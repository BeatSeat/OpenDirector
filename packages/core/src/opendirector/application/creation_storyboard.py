"""Project-level storyboard continuity auditing and lineage-aware shot revision."""
from __future__ import annotations

import json
import re
from uuid import UUID, uuid4, uuid5

from opendirector.application.creation_canvas import current_asset
from opendirector.domain.creation.editors import SubjectDocument, TimelineDocument
from opendirector.domain.creation.generation import FollowingReference
from opendirector.domain.creation.project import ProjectSnapshot
from opendirector.domain.creation.validation import ContractError
from opendirector.domain.h3_cinematography import (
    CAMERA_MOTIONS,
    SHOT_SIZES,
    h3_reference_ordinals,
    lint_h3_prompt,
    lint_shot_plan,
)
from opendirector.domain.projects import ProjectError


_SORTED_SHOT_SIZES = tuple(sorted(SHOT_SIZES, key=len, reverse=True))
_SORTED_CAMERA_MOTIONS = tuple(sorted(CAMERA_MOTIONS, key=len, reverse=True))


def extract_prompt_shot_features(prompt: str) -> dict[str, str | None]:
    """Extract primary canonical H3 shot size and camera motion from prompt text."""
    text = prompt or ""
    found_size = None
    for candidate in _SORTED_SHOT_SIZES:
        if re.search(rf"(?<![A-Za-z]){re.escape(candidate)}(?![A-Za-z])", text, flags=re.IGNORECASE):
            found_size = candidate
            break
    found_motion = None
    for candidate in _SORTED_CAMERA_MOTIONS:
        if re.search(rf"(?<![A-Za-z]){re.escape(candidate)}(?![A-Za-z])", text, flags=re.IGNORECASE):
            found_motion = candidate
            break
    return {"shotSize": found_size, "cameraMotion": found_motion}


def _infer_h3_mode(video_mode: str | None, roles: list[str]) -> str:
    if video_mode == "continuation":
        return "continuation"
    if video_mode == "reference":
        return "ref2va"
    if "first_frame" in roles and "last_frame" in roles:
        return "fl2va"
    if "first_frame" in roles:
        return "i2va"
    if "last_frame" in roles:
        return "l2va"
    return "t2va"


def _continuation_depth(snapshot: ProjectSnapshot, spec_id: UUID) -> int:
    items_by_id = {item.id: item for item in snapshot.canvasItems}
    depth = 0
    seen: set[UUID] = set()
    current_spec_id: UUID | None = spec_id
    while current_spec_id is not None and current_spec_id not in seen:
        seen.add(current_spec_id)
        cont_ref = next(
            (r for r in snapshot.references
             if r.active and r.targetSpecId == current_spec_id and r.role == "continuation"),
            None,
        )
        if cont_ref is None:
            break
        depth += 1
        if isinstance(cont_ref, FollowingReference):
            src_item = items_by_id.get(cont_ref.source.id)
            if src_item and src_item.binding.role == "generation":
                current_spec_id = src_item.binding.objectRef.id
                continue
        break
    return depth


def audit_project_storyboard(
    snapshot: ProjectSnapshot,
    *,
    timeline_editor_id: UUID | None = None,
    group_id: UUID | None = None,
    spec_ids: tuple[UUID, ...] | None = None,
) -> dict:
    """Audit multi-shot continuity, H3 prompt/ordinal alignment, and pilot-gate readiness."""
    assets_by_id = {asset.id: asset for asset in snapshot.assets}
    items_by_id = {item.id: item for item in snapshot.canvasItems}
    specs_by_id = {spec.id: spec for spec in snapshot.generationSpecs}
    docs_by_id = {doc.id: doc for doc in snapshot.editorDocuments}
    jobs_by_id = {job.id: job for job in snapshot.jobs}
    results_by_spec: dict[UUID, list] = {}
    for res in snapshot.results:
        results_by_spec.setdefault(res.specId, []).append(res)

    item_by_spec: dict[UUID, object] = {}
    for item in snapshot.canvasItems:
        if item.binding.role == "generation":
            item_by_spec.setdefault(item.binding.objectRef.id, item)

    unlinked_timeline_clips: list[str] = []
    ordered_specs = []

    if spec_ids is not None:
        for sid in spec_ids:
            spec = specs_by_id.get(sid)
            if spec is None or spec.kind != "video":
                raise ContractError("missing_object", "未找到指定的视频生成配置。", sid)
            ordered_specs.append(spec)
    elif timeline_editor_id is not None:
        doc = docs_by_id.get(timeline_editor_id)
        if not isinstance(doc, TimelineDocument):
            raise ContractError("missing_object", "未找到指定的时间线文档。", timeline_editor_id)
        conn_by_slot = {conn.slotId: conn for conn in doc.inputConnections}
        visual_track = next((track for track in doc.data.tracks if track.kind == "visual"), None)
        seen_specs: set[UUID] = set()
        for clip in visual_track.clips if visual_track else ():
            resolved_spec = None
            conn = conn_by_slot.get(clip.id)
            if conn is not None:
                source_item = items_by_id.get(conn.sourceId)
                if source_item is not None and source_item.binding.role == "generation":
                    candidate = specs_by_id.get(source_item.binding.objectRef.id)
                    if candidate is not None and candidate.kind == "video":
                        resolved_spec = candidate
            else:
                unlinked_timeline_clips.append(clip.id)
                asset_obj = assets_by_id.get(clip.resource.asset.assetId)
                if asset_obj and asset_obj.provenance.jobId:
                    job = jobs_by_id.get(asset_obj.provenance.jobId)
                    if job is not None:
                        candidate = specs_by_id.get(job.inputSnapshot.spec.id)
                        if candidate is not None and candidate.kind == "video":
                            resolved_spec = candidate
            if resolved_spec is not None and resolved_spec.id not in seen_specs:
                seen_specs.add(resolved_spec.id)
                ordered_specs.append(resolved_spec)
    else:
        if group_id is not None:
            group_item = items_by_id.get(group_id)
            if group_item is None or group_item.kind != "group":
                raise ContractError("missing_object", "未找到指定的画布分组节点。", group_id)
            candidate_items = [
                item for item in snapshot.canvasItems
                if item.parentId == group_id and item.kind == "video" and item.binding.role == "generation"
            ]
        else:
            candidate_items = [
                item for item in snapshot.canvasItems
                if item.visible and item.kind == "video" and item.binding.role == "generation"
            ]
        candidate_items.sort(key=lambda item: (item.position.y, item.position.x, str(item.id)))
        for item in candidate_items:
            spec = specs_by_id.get(item.binding.objectRef.id)
            if spec is not None and spec.kind == "video":
                ordered_specs.append(spec)

    project_subject_ids = [item.id for item in snapshot.canvasItems if item.kind == "subject"]
    shot_reports: list[dict] = []
    sequence_plan_inputs: list[dict] = []
    anchored_count = 0

    for index, spec in enumerate(ordered_specs, start=1):
        canvas_item = item_by_spec.get(spec.id)
        active_refs = sorted(
            (ref for ref in snapshot.references if ref.active and ref.targetSpecId == spec.id),
            key=lambda ref: (ref.order, str(ref.id)),
        )
        roles: list[str] = []
        ref_sources: list[dict] = []
        subject_item_ids: list[str] = []
        missing_upstream: list[dict] = []
        frame_fingerprints: dict[str, str] = {}

        for ref in active_refs:
            roles.append(ref.role)
            if ref.binding == "pinned":
                asset_obj = assets_by_id.get(ref.source.assetId)
                if asset_obj is None:
                    missing_upstream.append({"referenceId": str(ref.id), "role": ref.role, "reason": "missing_asset"})
                elif asset_obj.kind in {"image", "video", "audio"}:
                    ref_sources.append({
                        "id": str(asset_obj.id),
                        "name": asset_obj.name,
                        "kind": asset_obj.kind,
                        "role": ref.role,
                        "hasAudio": bool(asset_obj.metadata.hasAudio),
                    })
                    if ref.role in {"first_frame", "last_frame"}:
                        frame_fingerprints[ref.role] = asset_obj.fingerprint
            else:
                src_item = items_by_id.get(ref.source.id)
                if src_item is None:
                    missing_upstream.append({"referenceId": str(ref.id), "role": ref.role, "reason": "missing_item"})
                    continue
                if src_item.kind == "subject":
                    subject_item_ids.append(str(src_item.id))
                    sub_doc = docs_by_id.get(src_item.binding.objectRef.id)
                    if not isinstance(sub_doc, SubjectDocument) or sub_doc.data.main is None:
                        missing_upstream.append({
                            "referenceId": str(ref.id),
                            "sourceItemId": str(src_item.id),
                            "role": ref.role,
                            "reason": "empty_subject",
                        })
                        continue
                    slot_refs = [r for r in (sub_doc.data.main, *sub_doc.data.auxiliaries, sub_doc.data.voice) if r]
                    for slot in slot_refs:
                        asset_obj = assets_by_id.get(slot.assetId)
                        if asset_obj and asset_obj.kind in {"image", "video", "audio"}:
                            ref_sources.append({
                                "id": str(asset_obj.id),
                                "name": asset_obj.name,
                                "kind": asset_obj.kind,
                                "role": ref.role,
                                "hasAudio": bool(asset_obj.metadata.hasAudio),
                            })
                else:
                    asset_obj = current_asset(snapshot, src_item)
                    if asset_obj is None:
                        missing_upstream.append({
                            "referenceId": str(ref.id),
                            "sourceItemId": str(src_item.id),
                            "role": ref.role,
                            "reason": "unrendered_upstream_node",
                        })
                    elif asset_obj.kind in {"image", "video", "audio"}:
                        ref_sources.append({
                            "id": str(asset_obj.id),
                            "name": asset_obj.name,
                            "kind": asset_obj.kind,
                            "role": ref.role,
                            "hasAudio": bool(asset_obj.metadata.hasAudio),
                        })
                        if ref.role in {"first_frame", "last_frame"}:
                            frame_fingerprints[ref.role] = asset_obj.fingerprint

        if subject_item_ids or ref_sources:
            anchored_count += 1

        video_mode = getattr(spec.parameters, "videoMode", None)
        duration = float(getattr(spec.parameters, "durationSeconds", None) or 5.0)
        generate_audio = bool(getattr(spec.parameters, "generateAudio", True))
        h3_mode = _infer_h3_mode(video_mode, roles)
        continuity_issues: list[str] = []
        continuity_warnings: list[str] = []

        if missing_upstream:
            continuity_issues.append(
                f"Shot {index} has {len(missing_upstream)} unrendered or missing upstream reference source(s); "
                "render/adopt upstream keyframes or media before submitting video generation."
            )
        if (
            "first_frame" in frame_fingerprints
            and "last_frame" in frame_fingerprints
            and frame_fingerprints["first_frame"] == frame_fingerprints["last_frame"]
        ):
            continuity_warnings.append(
                f"Shot {index} FL2VA first_frame and last_frame point to identical image bytes; "
                "expect near-zero bridge progression unless changed."
            )

        cont_depth = _continuation_depth(snapshot, spec.id) if h3_mode == "continuation" else 0
        if cont_depth > 3:
            continuity_warnings.append(
                f"Shot {index} is at continuation hop depth {cont_depth} (>3); "
                "re-anchor with a fresh keyframe to avoid latent accumulation drift."
            )

        lint_mode = "i2va" if h3_mode == "continuation" else h3_mode
        prompt_lint = lint_h3_prompt(
            spec.prompt or "",
            mode=lint_mode,
            duration_seconds=duration,
            reference_sources=ref_sources if h3_mode != "continuation" else None,
        )
        features = extract_prompt_shot_features(spec.prompt or "")
        candidates = results_by_spec.get(spec.id, [])
        adopted_id = str(spec.currentResultId) if spec.currentResultId else None

        sequence_plan_inputs.append({
            "durationSeconds": duration,
            "shotSize": features["shotSize"],
            "cameraMotion": features["cameraMotion"],
            "mode": "i2va" if h3_mode == "continuation" else h3_mode,
        })

        ready_for_video = not missing_upstream and prompt_lint["valid"]
        shot_reports.append({
            "shotIndex": index,
            "specId": str(spec.id),
            "specRevision": spec.revision,
            "canvasItemId": str(canvas_item.id) if canvas_item is not None else None,
            "title": getattr(canvas_item, "title", None) if canvas_item is not None else None,
            "h3Mode": h3_mode,
            "durationSeconds": duration,
            "shotSize": features["shotSize"],
            "cameraMotion": features["cameraMotion"],
            "subjectItemIds": subject_item_ids,
            "referenceOrdinals": h3_reference_ordinals(ref_sources) if h3_mode != "continuation" else [],
            "missingUpstream": missing_upstream,
            "continuationDepth": cont_depth,
            "candidateCount": len(candidates),
            "adoptedResultId": adopted_id,
            "readyForVideoGeneration": ready_for_video,
            "promptLint": prompt_lint,
            "continuityIssues": continuity_issues,
            "continuityWarnings": continuity_warnings,
        })

    # Second pass: cross-shot Subject / anchor consistency across the sequence
    sequence_continuity_warnings: list[str] = []
    if len(shot_reports) >= 2 and (project_subject_ids or anchored_count > 0):
        for shot in shot_reports:
            if not shot["subjectItemIds"] and not shot["referenceOrdinals"] and shot["h3Mode"] == "t2va":
                msg = (
                    f"Shot {shot['shotIndex']} ({shot['specId']}) runs bare T2VA with zero Subject or reference "
                    "anchors while project/sequence anchors exist; character or scene identity may drift."
                )
                shot["continuityWarnings"].append(msg)
                sequence_continuity_warnings.append(msg)

    if unlinked_timeline_clips:
        sequence_continuity_warnings.append(
            f"Timeline has {len(unlinked_timeline_clips)} visual clip(s) without inputConnections provenance: "
            f"{unlinked_timeline_clips}. Link sourceItemId for surgical shot revision."
        )

    sequence_lint = lint_shot_plan(sequence_plan_inputs) if shot_reports else {
        "valid": True,
        "shotCount": 0,
        "totalDurationSeconds": 0.0,
        "shotSizeDiversity": 0.0,
        "cameraMotionDiversity": 0.0,
        "errors": [],
        "warnings": [],
    }

    ready_ids = [s["specId"] for s in shot_reports if s["readyForVideoGeneration"]]
    blocked_ids = [s["specId"] for s in shot_reports if not s["readyForVideoGeneration"]]
    adopted_ids = [s["specId"] for s in shot_reports if s["adoptedResultId"] is not None]
    unreviewed_ids = [
        s["specId"] for s in shot_reports
        if s["candidateCount"] > 0 and s["adoptedResultId"] is None
    ]
    pending_seconds = sum(
        float(s["durationSeconds"]) for s in shot_reports if s["adoptedResultId"] is None
    )

    all_valid = (
        bool(sequence_lint["valid"])
        and not blocked_ids
        and all(not s["continuityIssues"] for s in shot_reports)
    )

    return {
        "projectId": str(snapshot.project.id),
        "valid": all_valid,
        "shots": shot_reports,
        "sequenceLint": sequence_lint,
        "continuity": {
            "projectSubjectCount": len(project_subject_ids),
            "unlinkedTimelineClipIds": unlinked_timeline_clips,
            "warnings": sequence_continuity_warnings,
        },
        "gate": {
            "shotCount": len(shot_reports),
            "readyShotSpecIds": ready_ids,
            "blockedShotSpecIds": blocked_ids,
            "adoptedShotSpecIds": adopted_ids,
            "unreviewedCandidateSpecIds": unreviewed_ids,
            "estimatedPendingVideoSeconds": round(pending_seconds, 3),
            "requiresUserApproval": bool(pending_seconds > 0 or blocked_ids or unreviewed_ids),
        },
    }


def _resolve_item_adopted_asset_id(snapshot: dict, item_id: str) -> str | None:
    items_by_id = {item["id"]: item for item in snapshot.get("canvasItems", ())}
    specs_by_id = {spec["id"]: spec for spec in snapshot.get("generationSpecs", ())}
    results_by_id = {res["id"]: res for res in snapshot.get("results", ())}
    item = items_by_id.get(item_id)
    if item is None:
        return None
    binding = item.get("binding", {})
    if binding.get("role") == "material":
        return binding.get("objectRef", {}).get("assetId")
    if binding.get("role") == "generation":
        if binding.get("materialOverride"):
            return binding["materialOverride"].get("assetId")
        spec = specs_by_id.get(binding.get("objectRef", {}).get("id"))
        if spec and spec.get("currentResultId"):
            res = results_by_id.get(spec["currentResultId"])
            if res:
                return res.get("asset", {}).get("assetId")
    return None


def prepare_timeline_shot_revision(
    snapshot: dict,
    *,
    timeline_editor_id: UUID,
    clip_id: str | None = None,
    asset_id: UUID | None = None,
    source_item_id: UUID | None = None,
    in_point: float | None = None,
    out_point: float | None = None,
    use_asset_duration: bool = False,
    title: str | None = None,
    operation_id: UUID | None = None,
) -> dict:
    """Prepare a surgical single-clip timeline save payload preserving all sibling clips."""
    doc = next(
        (d for d in snapshot.get("editorDocuments", ())
         if d["id"] == str(timeline_editor_id) and d["kind"] == "timeline"),
        None,
    )
    if doc is None:
        raise ProjectError(f"Timeline editor document {timeline_editor_id} is not in this project.")

    if source_item_id is not None and not any(
        item["id"] == str(source_item_id) for item in snapshot.get("canvasItems", ())
    ):
        raise ProjectError(f"Source canvas item {source_item_id} is not in this project.")

    resolved_clip_id = clip_id
    if resolved_clip_id is None:
        if source_item_id is None:
            raise ProjectError("Provide --clip-id or --source-item linked in timeline inputConnections.")
        matched = next(
            (c["slotId"] for c in (doc.get("inputConnections") or ()) if c["sourceId"] == str(source_item_id)),
            None,
        )
        if matched is None:
            raise ProjectError(
                f"Source canvas item {source_item_id} is not linked to any slot in timeline {timeline_editor_id}."
            )
        resolved_clip_id = matched

    resolved_asset_id = str(asset_id) if asset_id is not None else None
    if resolved_asset_id is None:
        if source_item_id is None:
            raise ProjectError("Provide --asset or a --source-item with an adopted/material asset.")
        resolved_asset_id = _resolve_item_adopted_asset_id(snapshot, str(source_item_id))
        if resolved_asset_id is None:
            raise ProjectError(
                f"Source canvas item {source_item_id} has no adopted result or material asset to sync."
            )

    asset_obj = next((a for a in snapshot.get("assets", ()) if a["id"] == resolved_asset_id), None)
    if asset_obj is None:
        raise ProjectError(f"Asset {resolved_asset_id} is not registered in this project.")

    updated_doc = json.loads(json.dumps(doc))
    target_track = None
    target_clip = None
    for track in updated_doc["data"]["tracks"]:
        for clip in track["clips"]:
            if clip["id"] == resolved_clip_id:
                target_track, target_clip = track, clip
                break
        if target_clip is not None:
            break
    if target_clip is None or target_track is None:
        raise ProjectError(f"Clip '{resolved_clip_id}' was not found in timeline {timeline_editor_id}.")

    media_kind = asset_obj["kind"]
    if target_track["kind"] == "visual" and media_kind not in {"image", "video"}:
        raise ProjectError("Visual timeline clips require an image or video asset.")
    if target_track["kind"] == "audio" and media_kind != "audio":
        raise ProjectError("Audio timeline clips require an audio asset.")

    old_asset_id = target_clip["resource"]["asset"]["assetId"]
    old_in = float(target_clip["inPoint"])
    old_out = float(target_clip["outPoint"])
    asset_duration = asset_obj.get("metadata", {}).get("durationSeconds")

    if media_kind == "image":
        new_in = 0.0
        new_out = float(out_point) if out_point is not None else (old_out - old_in)
    else:
        if asset_duration is None or float(asset_duration) <= 0:
            raise ProjectError("Video and audio timeline replacements require a probed positive durationSeconds.")
        total_dur = float(asset_duration)
        new_in = (
            float(in_point)
            if in_point is not None
            else (old_in if not use_asset_duration and old_in < total_dur else 0.0)
        )
        if out_point is not None:
            new_out = float(out_point)
        elif use_asset_duration:
            new_out = total_dur
        else:
            new_out = min(total_dur, new_in + (old_out - old_in))
            if new_out <= new_in:
                new_in, new_out = 0.0, total_dur
        if new_out <= new_in or new_out > total_dur + 1e-6:
            raise ProjectError(f"Invalid trim [{new_in}, {new_out}] for asset duration {total_dur}s.")

    new_asset_ref = {
        "assetId": asset_obj["id"],
        "revision": asset_obj["revision"],
        "fingerprint": asset_obj["fingerprint"],
        "kind": media_kind,
    }
    new_resource = {
        "asset": new_asset_ref,
        "title": title if title is not None else asset_obj["name"],
        "durationSeconds": asset_duration if media_kind != "image" else None,
    }
    target_clip["resource"] = new_resource
    target_clip["inPoint"] = new_in
    target_clip["outPoint"] = new_out

    imported = list(updated_doc["data"].get("importedAssets") or [])
    if not any(
        entry["asset"]["assetId"] == new_asset_ref["assetId"]
        and entry["asset"]["fingerprint"] == new_asset_ref["fingerprint"]
        for entry in imported
    ):
        imported.append(new_resource)
    updated_doc["data"]["importedAssets"] = imported

    operation = operation_id or uuid4()
    existing_conns = updated_doc.get("inputConnections") or []
    effective_source_id = str(source_item_id) if source_item_id is not None else next(
        (c["sourceId"] for c in existing_conns if c["slotId"] == resolved_clip_id),
        None,
    )
    connections = [c for c in existing_conns if c["slotId"] != resolved_clip_id]
    if effective_source_id is not None:
        if any(c["sourceId"] == effective_source_id for c in connections):
            raise ProjectError(
                f"Source canvas item {effective_source_id} is already connected to another timeline slot."
            )
        connections.append({
            "id": str(uuid5(operation, f"connection-{resolved_clip_id}")),
            "sourceId": effective_source_id,
            "slotId": resolved_clip_id,
            "asset": new_asset_ref,
        })
    updated_doc["inputConnections"] = connections

    return {
        "document": updated_doc,
        "expectedVersions": [
            {"objectType": "editor_document", "id": str(timeline_editor_id), "revision": doc["revision"]}
        ],
        "timelineEditorId": str(timeline_editor_id),
        "clipId": resolved_clip_id,
        "previousAssetId": old_asset_id,
        "newAssetId": asset_obj["id"],
        "inPoint": new_in,
        "outPoint": new_out,
    }
