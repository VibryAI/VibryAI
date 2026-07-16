"""Source processing pipeline: L0 evidence -> L1 claims -> project suggestions."""

from __future__ import annotations

import json
import re
from typing import Any

from cognition import store


def _entities(text: str) -> list[str]:
    entities = re.findall(r"@([\w\-]+)", text)
    entities.extend(re.findall(r"[\u4e00-\u9fff]{2,8}(?=[：:，,。；;、\s]|$)", text))
    return list(dict.fromkeys(item.strip() for item in entities if item.strip()))[:8]


def extract_candidates(source: dict) -> list[dict[str, Any]]:
    """Extract deterministic claims first; an LLM extractor can enrich this later."""
    text = source["content_text"].strip()
    candidates: list[dict[str, Any]] = []
    try:
        payload = json.loads(text)
    except (TypeError, json.JSONDecodeError):
        payload = None

    if isinstance(payload, dict):
        for decision in payload.get("key_decisions") or payload.get("decisions") or []:
            if isinstance(decision, str) and decision.strip():
                candidates.append({"content": f"[决策] {decision.strip()}", "network": "experience", "confidence": 0.9})
        for action in payload.get("action_items") or []:
            if isinstance(action, str) and action.strip():
                candidates.append({"content": f"[行动项] {action.strip()}", "network": "experience", "confidence": 0.85})
        intent = payload.get("current_intent") or ""
        if isinstance(intent, str) and intent.strip():
            candidates.append({"content": f"[目的] {intent.strip()}", "network": "experience", "confidence": 0.8})
        conflict = payload.get("memory_conflict") or ""
        if isinstance(conflict, str) and conflict.strip():
            candidates.append({"content": f"[观察] {conflict.strip()}", "network": "observation", "confidence": 0.6})
        for tag in payload.get("tags") or []:
            if isinstance(tag, str) and tag.strip():
                candidates.append({"content": f"[主题] {tag.strip()}", "network": "observation", "confidence": 0.55, "entities": [tag.strip()]})
        summary = payload.get("detailed_summary") or ""
        if isinstance(summary, str) and len(summary.strip()) >= 30:
            candidates.append({"content": summary.strip()[:1200], "network": "experience", "confidence": 0.7})

    if not candidates:
        network = "world" if source["source_type"] in {"document", "web"} else "experience"
        candidates.append({"content": text[:1600], "network": network, "confidence": 0.55})

    for candidate in candidates:
        candidate.setdefault("entities", _entities(candidate["content"]))
    return candidates


def _project_score(
    project: dict, source: dict, candidates: list[dict], semantic_score: float | None = None,
) -> tuple[float, list[str]]:
    # Compare the project profile only against incoming evidence. Including the
    # profile here would make every project appear semantically relevant.
    haystack = " ".join([
        source.get("title", ""), source.get("content_text", ""),
        " ".join(item["content"] for item in candidates),
    ]).lower()
    signals = [project["name"], *project.get("tags", [])]
    matched = [signal for signal in signals if signal and signal.lower() in haystack]
    profile = " ".join([
        project["name"], project.get("description", ""), project.get("background_html", ""),
        project.get("goal", ""), *project.get("tags", []),
    ])
    if semantic_score is None:
        from cognition.semantic import similarity
        semantic_score = similarity(profile, haystack)
    if not matched and semantic_score < 0.16:
        return 0.0, []
    name_weight = 0.7 if project["name"] in matched else 0.0
    tag_weight = min(0.3, 0.1 * len([item for item in matched if item != project["name"]]))
    return min(1.0, name_weight + tag_weight + semantic_score * 0.35), matched or ["semantic_similarity"]


def assign_projects(source: dict, candidates: list[dict]) -> list[dict]:
    metadata = source.get("metadata") or {}
    explicit_ids = metadata.get("project_ids") or []
    memberships: list[dict] = []
    for project_id in explicit_ids:
        if store.get_project(project_id, source["user_id"]):
            memberships.append(store.add_membership(
                project_id=project_id, object_type="source", object_id=source["id"],
                assignment_source="user", confidence=1.0, status="confirmed",
                reason={"kind": "explicit_source_hint"},
            ))

    projects = store.list_projects(source["user_id"])
    manual_memberships = {
        project["id"]: next((item for item in store.list_memberships(project["id"], "source")
                             if item["object_id"] == source["id"] and item["assignment_source"] == "user"), None)
        for project in projects
    }
    source_text = " ".join([
        source.get("title", ""), source.get("content_text", ""),
        " ".join(item["content"] for item in candidates),
    ])
    from cognition.semantic import similarities
    _model_id, scores = similarities(source_text, [
        " ".join([
            project["name"], project.get("description", ""), project.get("background_html", ""),
            project.get("goal", ""), *project.get("tags", []),
        ])
        for project in projects
    ])
    semantic_scores = {project["id"]: score for project, score in zip(projects, scores)}
    for project in projects:
        if project["id"] in explicit_ids:
            continue
        if manual_memberships.get(project["id"]):
            continue
        score, matched = _project_score(project, source, candidates, semantic_scores.get(project["id"]))
        if score < 0.1:
            continue
        memberships.append(store.add_membership(
            project_id=project["id"], object_type="source", object_id=source["id"],
            assignment_source="auto", confidence=score,
            status="confirmed" if score >= 0.7 else "suggested",
            reason={"kind": "keyword_baseline", "matched": matched},
        ))
    return memberships


def process_source(source_id: str) -> dict:
    source = store.get_source(source_id)
    if not source:
        raise ValueError(f"source not found: {source_id}")
    store.set_source_status(source_id, "processing")
    candidates = extract_candidates(source)
    claims = [store.create_claim(
        user_id=source["user_id"], source_id=source_id, content=item["content"],
        network=item["network"], entities=item.get("entities"),
        confidence=item["confidence"], occurred_at=source.get("occurred_at"), quote=item["content"][:500],
    ) for item in candidates if len(item["content"].strip()) >= 4]
    from cognition.semantic import persist_many, suggest_claim_relations
    persist_many([
        (source["user_id"], "claim", claim["id"], claim["content"])
        for claim in claims
    ])
    for claim in claims:
        suggest_claim_relations(claim)
    memberships = assign_projects(source, candidates)

    store.set_source_status(source_id, "processed")
    return {
        "source_id": source_id, "claim_count": len(claims), "memberships": memberships,
    }
