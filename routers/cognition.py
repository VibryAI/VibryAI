"""Vibry.AI Cognitive Core v2 API.

These endpoints are the shared contract for the Dashboard, VibryCard and
future Agent connectors. They intentionally avoid client-specific state.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

import db
from cognition import store
from utils.auth import resolve_user_id

router = APIRouter(tags=["cognition"])


class SourceInput(BaseModel):
    content: str = Field(min_length=1, max_length=2_000_000)
    source_type: str = Field(default="manual", max_length=40)
    origin: str = Field(default="api", max_length=80)
    title: str = Field(default="", max_length=300)
    mime_type: str = Field(default="text/plain", max_length=120)
    external_id: str = Field(default="", max_length=300)
    occurred_at: str | None = None
    parent_source_id: str | None = None
    derivation_type: str = Field(default="original", max_length=40)
    metadata: dict[str, Any] = Field(default_factory=dict)
    project_ids: list[str] = Field(default_factory=list)


class SourceProjectsInput(BaseModel):
    project_ids: list[str] = Field(default_factory=list)


class ProjectInput(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    description: str = Field(default="", max_length=8_000)
    goal: str = Field(default="", max_length=4_000)
    stage: str = Field(default="active", max_length=80)
    tags: list[str] = Field(default_factory=list)
    constraints: dict[str, Any] = Field(default_factory=dict)
    metrics: dict[str, Any] = Field(default_factory=dict)
    background_html: str = Field(default="", max_length=40_000)
    start_at: str | None = None
    target_at: str | None = None


class ProjectUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=160)
    description: str | None = Field(default=None, max_length=8_000)
    goal: str | None = Field(default=None, max_length=4_000)
    stage: str | None = Field(default=None, max_length=80)
    status: str | None = Field(default=None, max_length=40)
    tags: list[str] | None = None
    constraints: dict[str, Any] | None = None
    metrics: dict[str, Any] | None = None
    background_html: str | None = Field(default=None, max_length=40_000)
    start_at: str | None = None
    target_at: str | None = None


class ConversationInput(BaseModel):
    message: str = Field(min_length=1, max_length=40_000)
    reply_to_id: str | None = None
    feedback_action: str | None = Field(default=None, max_length=80)
    metadata: dict[str, Any] = Field(default_factory=dict)


class TaskInput(BaseModel):
    title: str = Field(min_length=1, max_length=300)
    description: str = Field(default="", max_length=8_000)
    due_at: str | None = None
    priority: str = Field(default="normal", max_length=40)
    source_message_id: str | None = None
    reminder: dict[str, Any] = Field(default_factory=dict)


class TaskUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=300)
    description: str | None = Field(default=None, max_length=8_000)
    status: str | None = Field(default=None, max_length=40)
    priority: str | None = Field(default=None, max_length=40)
    due_at: str | None = None


class L4Input(BaseModel):
    title: str = Field(min_length=1, max_length=300)
    content_html: str = Field(min_length=1, max_length=40_000)
    category: str = Field(default="general", max_length=80)
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    evidence: list[dict[str, Any]] = Field(default_factory=list)
    supersedes_id: str | None = None


class L4StatusInput(BaseModel):
    status: str = Field(pattern="^(suggested|confirmed|rejected|superseded)$")
    message: str = Field(default="", max_length=8_000)


class MembershipInput(BaseModel):
    object_type: str = Field(pattern="^(source|claim|scenario)$")
    object_id: str = Field(min_length=1, max_length=120)
    status: str = Field(default="confirmed", pattern="^(suggested|confirmed|rejected)$")
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    reason: dict[str, Any] = Field(default_factory=dict)


class ContextInput(BaseModel):
    query: str = Field(min_length=1, max_length=10_000)
    project_ids: list[str] = Field(default_factory=list)
    limit: int = Field(default=12, ge=1, le=50)
    token_budget: int = Field(default=1200, ge=200, le=8000)


class FeedbackInput(BaseModel):
    target_type: str = Field(min_length=1, max_length=80)
    target_id: str = Field(min_length=1, max_length=120)
    action: str = Field(min_length=1, max_length=80)
    correction: dict[str, Any] = Field(default_factory=dict)


class InsightRunInput(BaseModel):
    project_id: str | None = None
    trigger: str = Field(default="manual", max_length=40)


class LegacyMigrationInput(BaseModel):
    user_id: str | None = None
    limit: int = Field(default=1000, ge=1, le=10000)
    dry_run: bool = True


class SemanticReindexInput(BaseModel):
    user_id: str | None = None
    batch_size: int = Field(default=64, ge=1, le=256)


@router.post("/api/v2/sources", status_code=202)
async def create_source(request: Request, payload: SourceInput):
    user_id = resolve_user_id(request)
    source, job, duplicate = store.create_source(
        user_id=user_id,
        source_type=payload.source_type,
        content=payload.content,
        origin=payload.origin,
        title=payload.title,
        mime_type=payload.mime_type,
        external_id=payload.external_id,
        occurred_at=payload.occurred_at,
        parent_source_id=payload.parent_source_id,
        derivation_type=payload.derivation_type,
        metadata=payload.metadata,
        project_ids=payload.project_ids,
    )
    return JSONResponse({"source": source, "job": job, "duplicate": duplicate}, status_code=200 if duplicate else 202)


@router.get("/api/v2/sources")
async def list_sources(request: Request, limit: int = 50, status: str = ""):
    user_id = resolve_user_id(request)
    items = store.list_sources(user_id, limit=limit, status=status or None)
    return {"count": len(items), "sources": items}


@router.get("/api/v2/sources/{source_id}")
async def get_source(request: Request, source_id: str):
    source = store.get_source(source_id, resolve_user_id(request))
    if not source:
        raise HTTPException(status_code=404, detail="source not found")
    return {"source": source, "claims": store.list_claims_for_source(source_id)}


@router.put("/api/v2/sources/{source_id}/projects")
async def set_source_projects(request: Request, source_id: str, payload: SourceProjectsInput):
    try:
        projects = store.set_source_projects(
            user_id=resolve_user_id(request), source_id=source_id, project_ids=payload.project_ids,
        )
    except ValueError as exc:
        raise HTTPException(status_code=404 if str(exc) == "source not found" else 422, detail=str(exc)) from exc
    return {"source_id": source_id, "projects": projects}


@router.get("/api/v2/dashboard")
async def dashboard_snapshot(request: Request):
    return store.dashboard_snapshot(resolve_user_id(request))


@router.get("/api/v2/jobs/{job_id}")
async def get_job(request: Request, job_id: str):
    job = store.get_job(job_id, resolve_user_id(request))
    if not job:
        raise HTTPException(status_code=404, detail="job not found")
    return {"job": job}


@router.post("/api/v2/projects", status_code=201)
async def create_project(request: Request, payload: ProjectInput):
    project = store.create_project(
        user_id=resolve_user_id(request), name=payload.name, description=payload.description,
        goal=payload.goal, stage=payload.stage, tags=payload.tags,
        constraints=payload.constraints, metrics=payload.metrics,
        background_html=payload.background_html, start_at=payload.start_at, target_at=payload.target_at,
    )
    return {"project": project}


@router.get("/api/v2/projects")
async def list_projects(request: Request, limit: int = 100):
    projects = store.list_projects(resolve_user_id(request), limit=limit)
    return {"count": len(projects), "projects": projects}


@router.patch("/api/v2/projects/{project_id}")
async def update_project(request: Request, project_id: str, payload: ProjectUpdate):
    project = store.update_project(
        project_id=project_id, user_id=resolve_user_id(request),
        changes=payload.model_dump(exclude_unset=True),
    )
    if not project:
        raise HTTPException(status_code=404, detail="project not found")
    return {"project": project}


@router.delete("/api/v2/projects/{project_id}")
async def delete_project(request: Request, project_id: str):
    deleted = store.delete_project(project_id=project_id, user_id=resolve_user_id(request))
    if not deleted:
        raise HTTPException(status_code=404, detail="project not found")
    return {"deleted": True, **deleted}


@router.get("/api/v2/plugins")
async def list_plugins():
    from cognition.plugins import list_plugins as discover_plugins
    plugins = discover_plugins()
    return {"count": len(plugins), "plugins": plugins}


@router.get("/api/v2/operations")
async def get_operations(request: Request, limit: int = 100):
    """Control-plane state shared by Dashboard and trusted local clients."""
    from pathlib import Path
    import sys
    from cognition.plugins import list_plugins as discover_plugins
    from cognition.semantic import status as semantic_status
    return {
        "jobs": store.list_jobs(resolve_user_id(request), limit=limit),
        "plugins": discover_plugins(),
        "semantic": semantic_status(),
        "mcp": {
            "transport": "stdio",
            "entrypoint": str(Path(__file__).resolve().parents[1] / "mcp_server.py"),
            "client_config": {
                "mcpServers": {
                    "vibry-ai": {
                        "command": sys.executable,
                        "args": [str(Path(__file__).resolve().parents[1] / "mcp_server.py"), "--user-id", "<your-user-id>"],
                    }
                }
            },
            "tools": ["vibry_search_context", "vibry_list_projects", "vibry_project_brief", "vibry_capture_source"],
            "scopes": ["context:read", "project:read", "source:write"],
        },
    }


@router.post("/api/v2/jobs/{job_id}/retry", status_code=202)
async def retry_job(request: Request, job_id: str):
    job = store.retry_job(job_id, resolve_user_id(request))
    if not job:
        raise HTTPException(status_code=404, detail="job not found")
    return {"job": job}


@router.get("/api/v2/projects/{project_id}")
async def get_project(request: Request, project_id: str):
    project = store.get_project(project_id, resolve_user_id(request))
    if not project:
        raise HTTPException(status_code=404, detail="project not found")
    return {"project": project, "memberships": store.list_memberships(project_id)}


@router.get("/api/v2/projects/{project_id}/workspace")
async def get_project_workspace(request: Request, project_id: str):
    workspace = store.project_workspace(project_id, resolve_user_id(request))
    if not workspace:
        raise HTTPException(status_code=404, detail="project not found")
    return workspace


@router.post("/api/v2/projects/{project_id}/chat")
async def project_chat(request: Request, project_id: str, payload: ConversationInput):
    from cognition.conversation import send_message
    try:
        return await send_message(
            user_id=resolve_user_id(request), scope_type="project", scope_id=project_id,
            message=payload.message, reply_to_id=payload.reply_to_id,
            feedback_action=payload.feedback_action, metadata=payload.metadata,
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.post("/api/v2/projects/{project_id}/tasks", status_code=201)
async def create_project_task(request: Request, project_id: str, payload: TaskInput):
    try:
        task = store.create_task(
            user_id=resolve_user_id(request), project_id=project_id, title=payload.title,
            description=payload.description, due_at=payload.due_at, priority=payload.priority,
            source_message_id=payload.source_message_id, reminder=payload.reminder,
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"task": task}


@router.patch("/api/v2/tasks/{task_id}")
async def update_task(request: Request, task_id: str, payload: TaskUpdate):
    task = store.update_task(
        task_id=task_id, user_id=resolve_user_id(request),
        changes=payload.model_dump(exclude_unset=True),
    )
    if not task:
        raise HTTPException(status_code=404, detail="task not found")
    return {"task": task}


@router.put("/api/v2/recordings/{recording_id}/projects")
async def set_recording_projects(request: Request, recording_id: str, payload: SourceProjectsInput):
    user_id = resolve_user_id(request)
    source = store.get_source_by_external_id(user_id=user_id, external_id=recording_id)
    if not source:
        source = store.get_source_by_external_id(user_id=user_id, external_id=f"recording:{recording_id}")
    if not source:
        raise HTTPException(status_code=404, detail="recording source not found")
    try:
        projects = store.set_source_projects(
            user_id=user_id, source_id=source["id"], project_ids=payload.project_ids,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    store.add_event(
        user_id=user_id, event_type="recording_projects_updated", actor="user",
        object_type="source", object_id=source["id"], payload={"project_ids": payload.project_ids},
    )
    return {"recording_id": recording_id, "source_id": source["id"], "projects": projects}


@router.get("/api/v2/recordings/{recording_id}/content")
async def get_recording_content(request: Request, recording_id: str):
    """Return one linkable meeting view with transcript, minutes and evidence-bound insights."""
    user_id = resolve_user_id(request)
    recording = db.get_recording(recording_id)
    if not recording or recording.get("user_id") != user_id:
        raise HTTPException(status_code=404, detail="recording not found")

    source = None
    for external_id in (
        f"recording_summary:{recording_id}",
        f"recording:{recording_id}",
        recording_id,
    ):
        source = store.get_source_by_external_id(
            user_id=user_id, external_id=external_id,
        )
        if source:
            break

    related_insights = []
    if source:
        source_id = source["id"]
        for insight in store.list_insights(user_id, limit=100):
            if any(
                evidence.get("source_id") == source_id
                for claim in insight.get("evidence", [])
                for evidence in claim.get("sources", [])
            ):
                related_insights.append(insight)

    return {
        "recording": recording,
        "source": source,
        "insights": related_insights,
        "admin_path": f"/admin/?recording={recording_id}",
    }


@router.get("/api/v2/memory-matrix")
async def get_memory_matrix(request: Request):
    return store.memory_matrix(resolve_user_id(request))


@router.post("/api/v2/memory-matrix/chat")
async def memory_matrix_chat(request: Request, payload: ConversationInput):
    from cognition.conversation import send_message
    try:
        return await send_message(
            user_id=resolve_user_id(request), scope_type="global", scope_id="",
            message=payload.message, reply_to_id=payload.reply_to_id,
            feedback_action=payload.feedback_action, metadata=payload.metadata,
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.post("/api/v2/memory-matrix/items", status_code=201)
async def create_l4_item(request: Request, payload: L4Input):
    item = store.create_l4_item(
        user_id=resolve_user_id(request), title=payload.title,
        content_html=payload.content_html, category=payload.category,
        confidence=payload.confidence, evidence=payload.evidence,
        supersedes_id=payload.supersedes_id,
    )
    return {"item": item}


@router.patch("/api/v2/memory-matrix/items/{item_id}")
async def update_l4_item(request: Request, item_id: str, payload: L4StatusInput):
    user_id = resolve_user_id(request)
    try:
        item = store.update_l4_item(item_id=item_id, user_id=user_id, status=payload.status)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if not item:
        raise HTTPException(status_code=404, detail="L4 item not found")
    store.add_feedback(
        user_id=user_id, target_type="l4", target_id=item_id, action=payload.status,
        correction={"message": payload.message},
    )
    matrix = store.memory_matrix(user_id)
    store.add_message(
        thread_id=matrix["thread"]["id"], user_id=user_id, role="user",
        content=payload.message or f"L4: {payload.status}", content_format="plain",
        message_type="feedback", metadata={"item_id": item_id, "status": payload.status},
    )
    return {"item": item}


@router.post("/api/v2/projects/{project_id}/memberships")
async def add_project_membership(request: Request, project_id: str, payload: MembershipInput):
    project = store.get_project(project_id, resolve_user_id(request))
    if not project:
        raise HTTPException(status_code=404, detail="project not found")
    membership = store.add_membership(
        project_id=project_id, object_type=payload.object_type, object_id=payload.object_id,
        assignment_source="user", confidence=payload.confidence, status=payload.status,
        reason=payload.reason,
    )
    return {"membership": membership}


@router.get("/api/v2/projects/{project_id}/brief")
async def get_project_brief(request: Request, project_id: str):
    brief = store.project_brief(project_id, resolve_user_id(request))
    if not brief:
        raise HTTPException(status_code=404, detail="project not found")
    return brief


@router.get("/api/v2/insights")
async def list_insights(request: Request, project_id: str = "", limit: int = 30):
    return {"insights": store.list_insights(resolve_user_id(request), project_id or None, limit)}


@router.post("/api/v2/insights/run", status_code=202)
async def run_insights(request: Request, payload: InsightRunInput):
    user_id = resolve_user_id(request)
    projects = [store.get_project(payload.project_id, user_id)] if payload.project_id else store.dirty_projects()
    jobs = []
    for project in projects:
        if not project:
            continue
        jobs.append(store.enqueue_job(
            user_id=user_id, job_type="project_insight",
            payload={"project_id": project["id"], "trigger": payload.trigger},
        ))
    if payload.project_id and not jobs:
        raise HTTPException(status_code=404, detail="project not found")
    return JSONResponse({"count": len(jobs), "jobs": jobs}, status_code=202)


@router.post("/api/v2/context/build")
async def build_context(request: Request, payload: ContextInput):
    user_id = resolve_user_id(request)
    from cognition.context import compile_context
    result = compile_context(
        user_id=user_id, query=payload.query, project_ids=payload.project_ids,
        token_budget=payload.token_budget,
    )
    return {"mode": "evidence_project_insight", **result}


@router.post("/api/v2/feedback", status_code=201)
async def submit_feedback(request: Request, payload: FeedbackInput):
    feedback = store.add_feedback(
        user_id=resolve_user_id(request), target_type=payload.target_type,
        target_id=payload.target_id, action=payload.action, correction=payload.correction,
    )
    return {"feedback": feedback}


@router.post("/admin/api/v2/migrations/recordings")
async def migrate_legacy_recordings(request: Request, payload: LegacyMigrationInput):
    from utils.auth import check_admin
    if not check_admin(request):
        raise HTTPException(status_code=401, detail="Admin required")
    from cognition.migration import import_legacy_recordings
    return import_legacy_recordings(
        user_id=payload.user_id, limit=payload.limit, dry_run=payload.dry_run,
    )


@router.post("/admin/api/v2/migrations/recording-summaries")
async def migrate_legacy_recording_summaries(request: Request, payload: LegacyMigrationInput):
    from utils.auth import check_admin
    if not check_admin(request):
        raise HTTPException(status_code=401, detail="Admin required")
    from cognition.migration import import_legacy_recording_summaries
    return import_legacy_recording_summaries(
        user_id=payload.user_id, limit=payload.limit, dry_run=payload.dry_run,
    )


@router.post("/admin/api/v2/semantic/reindex")
async def rebuild_semantic_index(request: Request, payload: SemanticReindexInput):
    from utils.auth import check_admin
    if not check_admin(request):
        raise HTTPException(status_code=401, detail="Admin required")
    from cognition.semantic import rebuild_all_vectors
    return rebuild_all_vectors(user_id=payload.user_id, batch_size=payload.batch_size)
