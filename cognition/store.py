"""Durable storage primitives for the Vibry.AI cognitive core."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from db.connection import get_conn


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _json(value: Any) -> str:
    return json.dumps({} if value is None else value, ensure_ascii=False, separators=(",", ":"))


def _dict(value: str | None, fallback: Any) -> Any:
    try:
        return json.loads(value) if value else fallback
    except json.JSONDecodeError:
        return fallback


def _list(value: str | None) -> list[Any]:
    parsed = _dict(value, [])
    return parsed if isinstance(parsed, list) else []


def init_cognition_schema(conn: sqlite3.Connection) -> None:
    """Create the v2 tables in the existing primary SQLite database."""
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS sources (
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL DEFAULT 'anonymous',
            source_type TEXT NOT NULL,
            origin TEXT NOT NULL DEFAULT 'api',
            external_id TEXT NOT NULL DEFAULT '',
            title TEXT NOT NULL DEFAULT '',
            mime_type TEXT NOT NULL DEFAULT 'text/plain',
            content_text TEXT NOT NULL DEFAULT '',
            content_uri TEXT NOT NULL DEFAULT '',
            content_hash TEXT NOT NULL DEFAULT '',
            occurred_at TEXT,
            captured_at TEXT NOT NULL,
            parent_source_id TEXT,
            derivation_type TEXT NOT NULL DEFAULT 'original',
            metadata_json TEXT NOT NULL DEFAULT '{}',
            status TEXT NOT NULL DEFAULT 'queued',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(parent_source_id) REFERENCES sources(id)
        );
        CREATE INDEX IF NOT EXISTS idx_sources_user_captured ON sources(user_id, captured_at DESC);
        CREATE INDEX IF NOT EXISTS idx_sources_origin_external ON sources(user_id, origin, external_id);
        CREATE INDEX IF NOT EXISTS idx_sources_hash ON sources(user_id, content_hash);

        CREATE TABLE IF NOT EXISTS cognition_jobs (
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL DEFAULT 'anonymous',
            source_id TEXT,
            recording_id TEXT,
            job_type TEXT NOT NULL,
            payload_json TEXT NOT NULL DEFAULT '{}',
            progress_json TEXT NOT NULL DEFAULT '{}',
            dedupe_key TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'queued',
            attempts INTEGER NOT NULL DEFAULT 0,
            max_attempts INTEGER NOT NULL DEFAULT 3,
            lease_owner TEXT NOT NULL DEFAULT '',
            lease_until TEXT,
            run_after TEXT,
            error_text TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            started_at TEXT,
            finished_at TEXT,
            FOREIGN KEY(source_id) REFERENCES sources(id)
        );
        CREATE INDEX IF NOT EXISTS idx_jobs_status_created ON cognition_jobs(status, created_at);

        CREATE TABLE IF NOT EXISTS recording_aggregates (
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL DEFAULT 'anonymous',
            title TEXT NOT NULL DEFAULT '',
            recording_ids_json TEXT NOT NULL DEFAULT '[]',
            project_ids_json TEXT NOT NULL DEFAULT '[]',
            status TEXT NOT NULL DEFAULT 'queued',
            summary_markdown TEXT NOT NULL DEFAULT '',
            source_id TEXT,
            message_id TEXT,
            error_text TEXT NOT NULL DEFAULT '',
            dedupe_key TEXT NOT NULL,
            processing_version TEXT NOT NULL DEFAULT 'aggregate-minutes-v1',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            completed_at TEXT,
            UNIQUE(user_id, dedupe_key),
            FOREIGN KEY(source_id) REFERENCES sources(id),
            FOREIGN KEY(message_id) REFERENCES cognition_messages(id)
        );
        CREATE INDEX IF NOT EXISTS idx_recording_aggregates_user_created
            ON recording_aggregates(user_id, created_at DESC);

        CREATE TABLE IF NOT EXISTS recording_group_suggestions (
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL DEFAULT 'anonymous',
            suggestion_type TEXT NOT NULL,
            title TEXT NOT NULL DEFAULT '',
            recording_ids_json TEXT NOT NULL DEFAULT '[]',
            reason_json TEXT NOT NULL DEFAULT '{}',
            status TEXT NOT NULL DEFAULT 'pending',
            message_id TEXT,
            aggregate_id TEXT,
            dedupe_key TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(user_id, dedupe_key),
            FOREIGN KEY(message_id) REFERENCES cognition_messages(id),
            FOREIGN KEY(aggregate_id) REFERENCES recording_aggregates(id)
        );
        CREATE INDEX IF NOT EXISTS idx_recording_group_suggestions_user_created
            ON recording_group_suggestions(user_id, created_at DESC);

        CREATE TABLE IF NOT EXISTS task_suggestions (
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL DEFAULT 'anonymous',
            source_id TEXT NOT NULL,
            project_id TEXT,
            title TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            message_id TEXT,
            task_id TEXT,
            dedupe_key TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(user_id, dedupe_key),
            FOREIGN KEY(source_id) REFERENCES sources(id),
            FOREIGN KEY(project_id) REFERENCES projects_v2(id),
            FOREIGN KEY(message_id) REFERENCES cognition_messages(id),
            FOREIGN KEY(task_id) REFERENCES project_tasks(id)
        );
        CREATE INDEX IF NOT EXISTS idx_task_suggestions_user_created
            ON task_suggestions(user_id, created_at DESC);

        CREATE TABLE IF NOT EXISTS projects_v2 (
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL DEFAULT 'anonymous',
            name TEXT NOT NULL,
            description TEXT NOT NULL DEFAULT '',
            goal TEXT NOT NULL DEFAULT '',
            stage TEXT NOT NULL DEFAULT 'active',
            status TEXT NOT NULL DEFAULT 'active',
            constraints_json TEXT NOT NULL DEFAULT '{}',
            metrics_json TEXT NOT NULL DEFAULT '{}',
            tags_json TEXT NOT NULL DEFAULT '[]',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_projects_user_updated ON projects_v2(user_id, updated_at DESC);

        CREATE TABLE IF NOT EXISTS project_memberships (
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL,
            object_type TEXT NOT NULL,
            object_id TEXT NOT NULL,
            assignment_source TEXT NOT NULL DEFAULT 'auto',
            confidence REAL NOT NULL DEFAULT 0.0,
            status TEXT NOT NULL DEFAULT 'suggested',
            reason_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(project_id, object_type, object_id),
            FOREIGN KEY(project_id) REFERENCES projects_v2(id)
        );
        CREATE INDEX IF NOT EXISTS idx_memberships_object ON project_memberships(object_type, object_id);

        CREATE TABLE IF NOT EXISTS claims_v2 (
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL DEFAULT 'anonymous',
            network TEXT NOT NULL CHECK(network IN ('world','experience','observation','opinion')),
            content TEXT NOT NULL,
            entities_json TEXT NOT NULL DEFAULT '[]',
            occurred_at TEXT,
            confidence REAL NOT NULL DEFAULT 0.5,
            status TEXT NOT NULL DEFAULT 'active',
            extraction_version TEXT NOT NULL DEFAULT 'v2.0',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_claims_user_created ON claims_v2(user_id, created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_claims_network ON claims_v2(user_id, network);

        CREATE TABLE IF NOT EXISTS entities_v2 (
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL DEFAULT 'anonymous',
            canonical_name TEXT NOT NULL,
            entity_type TEXT NOT NULL DEFAULT 'unknown',
            aliases_json TEXT NOT NULL DEFAULT '[]',
            confidence REAL NOT NULL DEFAULT 0.5,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(user_id, canonical_name)
        );
        CREATE INDEX IF NOT EXISTS idx_entities_user_name ON entities_v2(user_id, canonical_name);

        CREATE TABLE IF NOT EXISTS claim_entities (
            claim_id TEXT NOT NULL,
            entity_id TEXT NOT NULL,
            role TEXT NOT NULL DEFAULT 'mentioned',
            confidence REAL NOT NULL DEFAULT 0.5,
            created_at TEXT NOT NULL,
            PRIMARY KEY(claim_id, entity_id),
            FOREIGN KEY(claim_id) REFERENCES claims_v2(id),
            FOREIGN KEY(entity_id) REFERENCES entities_v2(id)
        );

        CREATE TABLE IF NOT EXISTS claim_evidence (
            id TEXT PRIMARY KEY,
            claim_id TEXT NOT NULL,
            source_id TEXT NOT NULL,
            quote TEXT NOT NULL DEFAULT '',
            support_type TEXT NOT NULL DEFAULT 'supports',
            created_at TEXT NOT NULL,
            FOREIGN KEY(claim_id) REFERENCES claims_v2(id),
            FOREIGN KEY(source_id) REFERENCES sources(id)
        );
        CREATE INDEX IF NOT EXISTS idx_claim_evidence_source ON claim_evidence(source_id);

        CREATE TABLE IF NOT EXISTS semantic_vectors (
            object_type TEXT NOT NULL,
            object_id TEXT NOT NULL,
            user_id TEXT NOT NULL,
            model_id TEXT NOT NULL,
            text_hash TEXT NOT NULL,
            vector_json TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY(object_type, object_id, model_id)
        );
        CREATE INDEX IF NOT EXISTS idx_semantic_vectors_user ON semantic_vectors(user_id, object_type);

        CREATE TABLE IF NOT EXISTS claim_relations (
            id TEXT PRIMARY KEY,
            source_claim_id TEXT NOT NULL,
            target_claim_id TEXT NOT NULL,
            relation_type TEXT NOT NULL CHECK(relation_type IN ('similar','conflicts','supersedes')),
            confidence REAL NOT NULL,
            status TEXT NOT NULL DEFAULT 'suggested' CHECK(status IN ('suggested','confirmed','rejected')),
            reason_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            UNIQUE(source_claim_id, target_claim_id, relation_type),
            FOREIGN KEY(source_claim_id) REFERENCES claims_v2(id),
            FOREIGN KEY(target_claim_id) REFERENCES claims_v2(id)
        );

        CREATE TABLE IF NOT EXISTS insights_v2 (
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL DEFAULT 'anonymous',
            project_id TEXT,
            insight_type TEXT NOT NULL,
            title TEXT NOT NULL,
            content TEXT NOT NULL,
            confidence REAL NOT NULL DEFAULT 0.5,
            status TEXT NOT NULL DEFAULT 'active',
            evidence_json TEXT NOT NULL DEFAULT '[]',
            scope_type TEXT NOT NULL DEFAULT 'project',
            scope_id TEXT NOT NULL DEFAULT '',
            version INTEGER NOT NULL DEFAULT 1,
            supersedes_id TEXT,
            trigger_type TEXT NOT NULL DEFAULT 'scheduled',
            feedback_status TEXT NOT NULL DEFAULT '',
            expires_at TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(project_id) REFERENCES projects_v2(id),
            FOREIGN KEY(supersedes_id) REFERENCES insights_v2(id)
        );
        CREATE INDEX IF NOT EXISTS idx_insights_project_created ON insights_v2(project_id, created_at DESC);

        CREATE TABLE IF NOT EXISTS project_state (
            project_id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL DEFAULT 'anonymous',
            dirty INTEGER NOT NULL DEFAULT 1,
            last_source_at TEXT,
            last_insight_at TEXT,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(project_id) REFERENCES projects_v2(id)
        );
        CREATE INDEX IF NOT EXISTS idx_project_state_dirty ON project_state(dirty, updated_at);

        CREATE TABLE IF NOT EXISTS cognition_meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS cognition_feedback (
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL DEFAULT 'anonymous',
            target_type TEXT NOT NULL,
            target_id TEXT NOT NULL,
            action TEXT NOT NULL,
            correction_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS cognition_threads (
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL DEFAULT 'admin',
            scope_type TEXT NOT NULL CHECK(scope_type IN ('project','global','source')),
            scope_id TEXT NOT NULL DEFAULT '',
            title TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'active',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(user_id, scope_type, scope_id)
        );
        CREATE INDEX IF NOT EXISTS idx_cognition_threads_scope
            ON cognition_threads(user_id, scope_type, scope_id);

        CREATE TABLE IF NOT EXISTS cognition_messages (
            id TEXT PRIMARY KEY,
            thread_id TEXT NOT NULL,
            user_id TEXT NOT NULL DEFAULT 'admin',
            role TEXT NOT NULL CHECK(role IN ('user','assistant','system')),
            content TEXT NOT NULL DEFAULT '',
            content_format TEXT NOT NULL DEFAULT 'markdown'
                CHECK(content_format IN ('plain','markdown','html')),
            message_type TEXT NOT NULL DEFAULT 'chat',
            reply_to_id TEXT,
            source_id TEXT,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            FOREIGN KEY(thread_id) REFERENCES cognition_threads(id),
            FOREIGN KEY(reply_to_id) REFERENCES cognition_messages(id),
            FOREIGN KEY(source_id) REFERENCES sources(id)
        );
        CREATE INDEX IF NOT EXISTS idx_cognition_messages_thread
            ON cognition_messages(thread_id, created_at);

        CREATE TABLE IF NOT EXISTS project_tasks (
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL DEFAULT 'admin',
            project_id TEXT,
            title TEXT NOT NULL,
            description TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'pending',
            priority TEXT NOT NULL DEFAULT 'normal',
            due_at TEXT,
            source_message_id TEXT,
            reminder_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(project_id) REFERENCES projects_v2(id),
            FOREIGN KEY(source_message_id) REFERENCES cognition_messages(id)
        );
        CREATE INDEX IF NOT EXISTS idx_project_tasks_project
            ON project_tasks(project_id, status, due_at);

        CREATE TABLE IF NOT EXISTS cognition_events (
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL DEFAULT 'admin',
            project_id TEXT,
            event_type TEXT NOT NULL,
            actor TEXT NOT NULL DEFAULT 'system',
            object_type TEXT NOT NULL DEFAULT '',
            object_id TEXT NOT NULL DEFAULT '',
            payload_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            FOREIGN KEY(project_id) REFERENCES projects_v2(id)
        );
        CREATE INDEX IF NOT EXISTS idx_cognition_events_project
            ON cognition_events(project_id, created_at DESC);

        CREATE TABLE IF NOT EXISTS l4_profile_items (
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL DEFAULT 'admin',
            category TEXT NOT NULL DEFAULT 'general',
            title TEXT NOT NULL,
            content_html TEXT NOT NULL DEFAULT '',
            confidence REAL NOT NULL DEFAULT 0.5,
            status TEXT NOT NULL DEFAULT 'suggested'
                CHECK(status IN ('suggested','confirmed','rejected','superseded')),
            evidence_json TEXT NOT NULL DEFAULT '[]',
            version INTEGER NOT NULL DEFAULT 1,
            supersedes_id TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(supersedes_id) REFERENCES l4_profile_items(id)
        );
        CREATE INDEX IF NOT EXISTS idx_l4_profile_status
            ON l4_profile_items(user_id, status, updated_at DESC);
        """
    )

    project_columns = {row["name"] for row in conn.execute("PRAGMA table_info(projects_v2)").fetchall()}
    for column, declaration in [
        ("background_html", "TEXT NOT NULL DEFAULT ''"),
        ("start_at", "TEXT"),
        ("target_at", "TEXT"),
    ]:
        if column not in project_columns:
            conn.execute(f"ALTER TABLE projects_v2 ADD COLUMN {column} {declaration}")

    job_columns = {row["name"] for row in conn.execute("PRAGMA table_info(cognition_jobs)").fetchall()}
    for column, declaration in [
        ("recording_id", "TEXT"),
        ("progress_json", "TEXT NOT NULL DEFAULT '{}'"),
        ("dedupe_key", "TEXT NOT NULL DEFAULT ''"),
        ("lease_owner", "TEXT NOT NULL DEFAULT ''"),
        ("run_after", "TEXT"),
    ]:
        if column not in job_columns:
            conn.execute(f"ALTER TABLE cognition_jobs ADD COLUMN {column} {declaration}")
    insight_columns = {row["name"] for row in conn.execute("PRAGMA table_info(insights_v2)").fetchall()}
    for column, declaration in [
        ("scope_type", "TEXT NOT NULL DEFAULT 'project'"),
        ("scope_id", "TEXT NOT NULL DEFAULT ''"),
        ("version", "INTEGER NOT NULL DEFAULT 1"),
        ("supersedes_id", "TEXT"),
        ("trigger_type", "TEXT NOT NULL DEFAULT 'scheduled'"),
        ("feedback_status", "TEXT NOT NULL DEFAULT ''"),
        ("expires_at", "TEXT"),
    ]:
        if column not in insight_columns:
            conn.execute(f"ALTER TABLE insights_v2 ADD COLUMN {column} {declaration}")
    conn.execute(
        "UPDATE insights_v2 SET scope_id=COALESCE(project_id,'') WHERE scope_id=''"
    )
    conn.execute(
        "UPDATE l4_profile_items SET status='confirmed' "
        "WHERE status='suggested' AND confidence>=0.8"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_jobs_recording ON cognition_jobs(recording_id, created_at)"
    )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_jobs_dedupe "
        "ON cognition_jobs(dedupe_key) WHERE dedupe_key != ''"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_jobs_dispatch "
        "ON cognition_jobs(status,job_type,run_after,created_at)"
    )
    # Early v2 builds serialized empty lists as `{}`. Normalize those values
    # once so existing projects and insights remain safe for API clients.
    for table, column in [
        ("projects_v2", "tags_json"), ("claims_v2", "entities_json"),
        ("entities_v2", "aliases_json"), ("insights_v2", "evidence_json"),
    ]:
        conn.execute(f"UPDATE {table} SET {column}='[]' WHERE {column}='{{}}'")
    conn.execute("DELETE FROM semantic_vectors WHERE object_type='knowledge'")
    conn.execute("DROP TABLE IF EXISTS knowledge_evidence")
    conn.execute("DROP TABLE IF EXISTS knowledge_assets")
    conn.commit()


def _source_row(row: sqlite3.Row | None) -> dict | None:
    if not row:
        return None
    data = dict(row)
    data["metadata"] = _dict(data.pop("metadata_json", "{}"), {})
    return data


def _project_row(row: sqlite3.Row | None) -> dict | None:
    if not row:
        return None
    data = dict(row)
    data["constraints"] = _dict(data.pop("constraints_json", "{}"), {})
    data["metrics"] = _dict(data.pop("metrics_json", "{}"), {})
    data["tags"] = _list(data.pop("tags_json", "[]"))
    return data


def _message_row(row: sqlite3.Row | None) -> dict | None:
    if not row:
        return None
    data = dict(row)
    data["metadata"] = _dict(data.pop("metadata_json", "{}"), {})
    return data


def _task_row(row: sqlite3.Row | None) -> dict | None:
    if not row:
        return None
    data = dict(row)
    data["reminder"] = _dict(data.pop("reminder_json", "{}"), {})
    return data


def _l4_row(row: sqlite3.Row | None) -> dict | None:
    if not row:
        return None
    data = dict(row)
    data["evidence"] = _list(data.pop("evidence_json", "[]"))
    return data


def _aggregate_row(row: sqlite3.Row | None) -> dict | None:
    if not row:
        return None
    data = dict(row)
    data["recording_ids"] = _list(data.pop("recording_ids_json", "[]"))
    data["project_ids"] = _list(data.pop("project_ids_json", "[]"))
    return data


def _group_suggestion_row(row: sqlite3.Row | None) -> dict | None:
    if not row:
        return None
    data = dict(row)
    data["recording_ids"] = _list(data.pop("recording_ids_json", "[]"))
    data["reason"] = _dict(data.pop("reason_json", "{}"), {})
    return data


def create_recording_group_suggestion(
    *, user_id: str, suggestion_type: str, title: str,
    recording_ids: list[str], reason: dict, dedupe_key: str,
) -> tuple[dict, bool]:
    conn = get_conn()
    existing = conn.execute(
        "SELECT * FROM recording_group_suggestions WHERE user_id=? AND dedupe_key=?",
        (user_id, dedupe_key),
    ).fetchone()
    if existing:
        return _group_suggestion_row(existing) or {}, True

    suggestion_id = f"rgs_{uuid.uuid4().hex}"
    now = _now()
    try:
        conn.execute(
            """INSERT INTO recording_group_suggestions (
                   id,user_id,suggestion_type,title,recording_ids_json,reason_json,
                   status,dedupe_key,created_at,updated_at
               ) VALUES (?,?,?,?,?,?,'pending',?,?,?)""",
            (
                suggestion_id, user_id, suggestion_type, title.strip(),
                _json(recording_ids), _json(reason), dedupe_key, now, now,
            ),
        )
        conn.commit()
    except sqlite3.IntegrityError:
        conn.rollback()
        existing = conn.execute(
            "SELECT * FROM recording_group_suggestions WHERE user_id=? AND dedupe_key=?",
            (user_id, dedupe_key),
        ).fetchone()
        if existing:
            return _group_suggestion_row(existing) or {}, True
        raise
    return get_recording_group_suggestion(suggestion_id, user_id) or {}, False


def get_recording_group_suggestion(
    suggestion_id: str, user_id: str | None = None,
) -> dict | None:
    query = "SELECT * FROM recording_group_suggestions WHERE id=?"
    args: list[Any] = [suggestion_id]
    if user_id is not None:
        query += " AND user_id=?"
        args.append(user_id)
    return _group_suggestion_row(get_conn().execute(query, args).fetchone())


def update_recording_group_suggestion(
    suggestion_id: str, **changes: Any,
) -> dict | None:
    allowed = {"status", "message_id", "aggregate_id", "title"}
    assignments: list[str] = []
    values: list[Any] = []
    for key, value in changes.items():
        if key in allowed:
            assignments.append(f"{key}=?")
            values.append(value)
    if not assignments:
        return get_recording_group_suggestion(suggestion_id)
    assignments.append("updated_at=?")
    values.extend([_now(), suggestion_id])
    conn = get_conn()
    conn.execute(
        f"UPDATE recording_group_suggestions SET {','.join(assignments)} WHERE id=?",
        values,
    )
    conn.commit()
    return get_recording_group_suggestion(suggestion_id)


def update_message_metadata(
    message_id: str, user_id: str, changes: dict[str, Any],
) -> dict | None:
    conn = get_conn()
    row = conn.execute(
        "SELECT * FROM cognition_messages WHERE id=? AND user_id=?",
        (message_id, user_id),
    ).fetchone()
    message = _message_row(row)
    if not message:
        return None
    metadata = dict(message.get("metadata") or {})
    metadata.update(changes)
    conn.execute(
        "UPDATE cognition_messages SET metadata_json=? WHERE id=? AND user_id=?",
        (_json(metadata), message_id, user_id),
    )
    conn.commit()
    return _message_row(conn.execute(
        "SELECT * FROM cognition_messages WHERE id=?", (message_id,),
    ).fetchone())


def create_task_suggestion(
    *, user_id: str, source_id: str, title: str, dedupe_key: str,
    project_id: str | None = None,
) -> tuple[dict, bool]:
    conn = get_conn()
    existing = conn.execute(
        "SELECT * FROM task_suggestions WHERE user_id=? AND dedupe_key=?",
        (user_id, dedupe_key),
    ).fetchone()
    if existing:
        return dict(existing), True
    suggestion_id = f"tss_{uuid.uuid4().hex}"
    now = _now()
    try:
        conn.execute(
            """INSERT INTO task_suggestions (
                   id,user_id,source_id,project_id,title,status,dedupe_key,created_at,updated_at
               ) VALUES (?,?,?,?,?,'pending',?,?,?)""",
            (
                suggestion_id, user_id, source_id, project_id, title.strip(),
                dedupe_key, now, now,
            ),
        )
        conn.commit()
    except sqlite3.IntegrityError:
        conn.rollback()
        existing = conn.execute(
            "SELECT * FROM task_suggestions WHERE user_id=? AND dedupe_key=?",
            (user_id, dedupe_key),
        ).fetchone()
        if existing:
            return dict(existing), True
        raise
    return dict(conn.execute(
        "SELECT * FROM task_suggestions WHERE id=?", (suggestion_id,),
    ).fetchone()), False


def get_task_suggestion(
    suggestion_id: str, user_id: str | None = None,
) -> dict | None:
    query = "SELECT * FROM task_suggestions WHERE id=?"
    args: list[Any] = [suggestion_id]
    if user_id is not None:
        query += " AND user_id=?"
        args.append(user_id)
    row = get_conn().execute(query, args).fetchone()
    return dict(row) if row else None


def update_task_suggestion(suggestion_id: str, **changes: Any) -> dict | None:
    allowed = {"status", "message_id", "task_id"}
    assignments: list[str] = []
    values: list[Any] = []
    for key, value in changes.items():
        if key in allowed:
            assignments.append(f"{key}=?")
            values.append(value)
    if not assignments:
        return get_task_suggestion(suggestion_id)
    assignments.append("updated_at=?")
    values.extend([_now(), suggestion_id])
    conn = get_conn()
    conn.execute(
        f"UPDATE task_suggestions SET {','.join(assignments)} WHERE id=?", values,
    )
    conn.commit()
    return get_task_suggestion(suggestion_id)


def create_recording_aggregate(
    *, user_id: str, title: str, recording_ids: list[str],
    project_ids: list[str] | None = None, dedupe_key: str,
) -> tuple[dict, bool]:
    conn = get_conn()
    existing = conn.execute(
        "SELECT * FROM recording_aggregates WHERE user_id=? AND dedupe_key=?",
        (user_id, dedupe_key),
    ).fetchone()
    if existing:
        return _aggregate_row(existing) or {}, True

    aggregate_id = f"agg_{uuid.uuid4().hex}"
    now = _now()
    conn.execute(
        """INSERT INTO recording_aggregates (
               id,user_id,title,recording_ids_json,project_ids_json,status,
               dedupe_key,created_at,updated_at
           ) VALUES (?,?,?,?,?,'queued',?,?,?)""",
        (
            aggregate_id, user_id, title.strip(), _json(recording_ids),
            _json(project_ids or []), dedupe_key, now, now,
        ),
    )
    conn.commit()
    return get_recording_aggregate(aggregate_id, user_id) or {}, False


def get_recording_aggregate(aggregate_id: str, user_id: str | None = None) -> dict | None:
    conn = get_conn()
    query = "SELECT * FROM recording_aggregates WHERE id=?"
    args: list[Any] = [aggregate_id]
    if user_id is not None:
        query += " AND user_id=?"
        args.append(user_id)
    return _aggregate_row(conn.execute(query, args).fetchone())


def list_recording_aggregates(user_id: str, limit: int = 50) -> list[dict]:
    rows = get_conn().execute(
        "SELECT * FROM recording_aggregates WHERE user_id=? ORDER BY created_at DESC LIMIT ?",
        (user_id, max(1, min(limit, 200))),
    ).fetchall()
    return [_aggregate_row(row) or {} for row in rows]


def update_recording_aggregate(aggregate_id: str, **changes: Any) -> dict | None:
    allowed = {
        "title", "status", "summary_markdown", "source_id", "message_id",
        "error_text", "processing_version", "completed_at",
    }
    assignments: list[str] = []
    values: list[Any] = []
    for key, value in changes.items():
        if key in allowed:
            assignments.append(f"{key}=?")
            values.append(value)
    if not assignments:
        return get_recording_aggregate(aggregate_id)
    assignments.append("updated_at=?")
    values.extend([_now(), aggregate_id])
    conn = get_conn()
    conn.execute(
        f"UPDATE recording_aggregates SET {','.join(assignments)} WHERE id=?",
        values,
    )
    conn.commit()
    return get_recording_aggregate(aggregate_id)


def create_source(
    *,
    user_id: str,
    source_type: str,
    content: str,
    origin: str = "api",
    title: str = "",
    mime_type: str = "text/plain",
    external_id: str = "",
    occurred_at: str | None = None,
    parent_source_id: str | None = None,
    derivation_type: str = "original",
    metadata: dict | None = None,
    project_ids: list[str] | None = None,
) -> tuple[dict, dict, bool]:
    """Persist L0 evidence and enqueue processing atomically."""
    conn = get_conn()
    clean_content = (content or "").strip()
    if not clean_content:
        raise ValueError("content is required")
    now = _now()
    content_hash = hashlib.sha256(clean_content.encode("utf-8")).hexdigest()

    if external_id:
        existing = conn.execute(
            "SELECT * FROM sources WHERE user_id=? AND origin=? AND external_id=?",
            (user_id, origin, external_id),
        ).fetchone()
        if existing:
            source = _source_row(existing)
            job = conn.execute(
                "SELECT * FROM cognition_jobs WHERE source_id=? ORDER BY created_at DESC LIMIT 1",
                (source["id"],),
            ).fetchone()
            return source, dict(job) if job else {}, True

    source_id = f"src_{uuid.uuid4().hex}"
    job_id = f"job_{uuid.uuid4().hex}"
    metadata = dict(metadata or {})
    if project_ids:
        metadata["project_ids"] = list(dict.fromkeys(project_ids))
    conn.execute(
        """INSERT INTO sources (
            id,user_id,source_type,origin,external_id,title,mime_type,content_text,
            content_hash,occurred_at,captured_at,parent_source_id,derivation_type,
            metadata_json,status,created_at,updated_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            source_id, user_id, source_type, origin, external_id, title, mime_type,
            clean_content, content_hash, occurred_at, now, parent_source_id,
            derivation_type, _json(metadata), "queued", now, now,
        ),
    )
    conn.execute(
        """INSERT INTO cognition_jobs (
            id,user_id,source_id,job_type,payload_json,status,created_at
        ) VALUES (?,?,?,?,?,?,?)""",
        (job_id, user_id, source_id, "process_source", "{}", "queued", now),
    )
    conn.commit()
    source = get_source(source_id)
    job = get_job(job_id)
    return source or {}, job or {}, False


def get_source(source_id: str, user_id: str | None = None) -> dict | None:
    conn = get_conn()
    query = "SELECT * FROM sources WHERE id=?"
    args: list[Any] = [source_id]
    if user_id is not None:
        query += " AND user_id=?"
        args.append(user_id)
    return _source_row(conn.execute(query, args).fetchone())


def get_source_by_external_id(*, user_id: str, external_id: str, origin: str | None = None) -> dict | None:
    conn = get_conn()
    query = "SELECT * FROM sources WHERE user_id=? AND external_id=?"
    args: list[Any] = [user_id, external_id]
    if origin:
        query += " AND origin=?"
        args.append(origin)
    query += " ORDER BY captured_at DESC LIMIT 1"
    return _source_row(conn.execute(query, args).fetchone())


def source_project_ids(*, source_id: str, user_id: str) -> list[str]:
    conn = get_conn()
    rows = conn.execute(
        """SELECT m.project_id FROM project_memberships m
           JOIN projects_v2 p ON p.id=m.project_id
           WHERE m.object_type='source' AND m.object_id=?
             AND m.status!='rejected' AND p.user_id=?
           ORDER BY m.updated_at DESC""",
        (source_id, user_id),
    ).fetchall()
    return [row["project_id"] for row in rows]


def refresh_source_content(
    *, source_id: str, user_id: str, content: str,
    title: str | None = None, metadata: dict | None = None,
    project_ids: list[str] | None = None,
) -> tuple[dict, dict, bool]:
    """Replace changed evidence in place and enqueue a clean extraction pass."""
    conn = get_conn()
    source = get_source(source_id, user_id)
    if not source:
        raise ValueError("source not found")
    clean_content = (content or "").strip()
    if not clean_content:
        raise ValueError("content is required")
    content_hash = hashlib.sha256(clean_content.encode("utf-8")).hexdigest()
    if content_hash == source.get("content_hash"):
        return source, {}, False

    claim_rows = conn.execute(
        "SELECT DISTINCT claim_id FROM claim_evidence WHERE source_id=?",
        (source_id,),
    ).fetchall()
    claim_ids = [row["claim_id"] for row in claim_rows]
    conn.execute("DELETE FROM claim_evidence WHERE source_id=?", (source_id,))
    orphan_ids = [
        claim_id for claim_id in claim_ids
        if not conn.execute(
            "SELECT 1 FROM claim_evidence WHERE claim_id=? LIMIT 1", (claim_id,),
        ).fetchone()
    ]
    if orphan_ids:
        placeholders = ",".join("?" for _ in orphan_ids)
        conn.execute(
            f"DELETE FROM claim_relations WHERE source_claim_id IN ({placeholders}) OR target_claim_id IN ({placeholders})",
            [*orphan_ids, *orphan_ids],
        )
        conn.execute(
            f"DELETE FROM claim_entities WHERE claim_id IN ({placeholders})", orphan_ids,
        )
        conn.execute(
            f"DELETE FROM semantic_vectors WHERE object_type='claim' AND object_id IN ({placeholders})",
            orphan_ids,
        )
        conn.execute(f"DELETE FROM claims_v2 WHERE id IN ({placeholders})", orphan_ids)

    merged_metadata = dict(source.get("metadata") or {})
    merged_metadata.update(metadata or {})
    effective_project_ids = list(dict.fromkeys(
        project_ids or source_project_ids(source_id=source_id, user_id=user_id)
    ))
    if effective_project_ids:
        merged_metadata["project_ids"] = effective_project_ids
    now = _now()
    conn.execute(
        """UPDATE sources SET content_text=?,content_hash=?,title=?,metadata_json=?,
           status='queued',updated_at=? WHERE id=? AND user_id=?""",
        (
            clean_content, content_hash, title if title is not None else source.get("title", ""),
            _json(merged_metadata), now, source_id, user_id,
        ),
    )
    for project_id in effective_project_ids:
        conn.execute(
            "UPDATE project_state SET dirty=1,updated_at=? WHERE project_id=? AND user_id=?",
            (now, project_id, user_id),
        )
    conn.commit()
    job = enqueue_job(
        user_id=user_id,
        source_id=source_id,
        job_type="process_source",
        dedupe_key=f"source:{source_id}:refresh:{content_hash[:16]}",
    )
    return get_source(source_id, user_id) or {}, job, True


def count_sources(user_id: str, status: str | None = None) -> int:
    conn = get_conn()
    query = "SELECT COUNT(*) AS count FROM sources WHERE user_id=?"
    args: list[Any] = [user_id]
    if status:
        query += " AND status=?"
        args.append(status)
    return int(conn.execute(query, args).fetchone()["count"])


def list_sources(
    user_id: str, limit: int = 50, status: str | None = None, offset: int = 0,
) -> list[dict]:
    conn = get_conn()
    query = "SELECT * FROM sources WHERE user_id=?"
    args: list[Any] = [user_id]
    if status:
        query += " AND status=?"
        args.append(status)
    query += " ORDER BY captured_at DESC LIMIT ? OFFSET ?"
    args.extend([max(1, min(limit, 200)), max(0, offset)])
    sources = [_source_row(row) for row in conn.execute(query, args).fetchall()]
    if not sources:
        return sources
    source_ids = [item["id"] for item in sources]
    placeholders = ",".join("?" for _ in source_ids)
    memberships = conn.execute(
        f"""SELECT m.object_id,m.status,m.confidence,m.assignment_source,p.id AS project_id,p.name AS project_name
            FROM project_memberships m JOIN projects_v2 p ON p.id=m.project_id
            WHERE p.user_id=? AND m.object_type='source' AND m.object_id IN ({placeholders})
            ORDER BY p.name""",
        [user_id, *source_ids],
    ).fetchall()
    projects_by_source: dict[str, list[dict]] = {source_id: [] for source_id in source_ids}
    for row in memberships:
        projects_by_source[row["object_id"]].append({
            "id": row["project_id"], "name": row["project_name"], "status": row["status"],
            "confidence": row["confidence"], "assignment_source": row["assignment_source"],
        })
    for source in sources:
        source["projects"] = projects_by_source[source["id"]]
    return sources


def dashboard_snapshot(user_id: str) -> dict:
    """Small, client-neutral dashboard read model for the second-brain workspace."""
    conn = get_conn()
    def count(table: str, where: str = "", args: tuple = ()) -> int:
        row = conn.execute(f"SELECT COUNT(*) AS count FROM {table} {where}", args).fetchone()
        return int(row["count"])
    return {
        "counts": {
            "sources": count("sources", "WHERE user_id=?", (user_id,)),
            "sources_pending": count("sources", "WHERE user_id=? AND status IN ('queued','processing')", (user_id,)),
            "projects": count("projects_v2", "WHERE user_id=? AND status='active'", (user_id,)),
            "claims": count("claims_v2", "WHERE user_id=? AND status='active'", (user_id,)),
            "insights": count("insights_v2", "WHERE user_id=? AND status='active'", (user_id,)),
            "jobs_queued": count("cognition_jobs", "WHERE user_id=? AND status IN ('queued','running')", (user_id,)),
        },
        "projects": list_projects(user_id, limit=100),
        "recent_sources": list_sources(user_id, limit=8),
        "recent_insights": list_insights(user_id, limit=6),
        "jobs": list_jobs(user_id, limit=12),
    }


def get_job(job_id: str, user_id: str | None = None) -> dict | None:
    conn = get_conn()
    query = "SELECT * FROM cognition_jobs WHERE id=?"
    args: list[Any] = [job_id]
    if user_id is not None:
        query += " AND user_id=?"
        args.append(user_id)
    row = conn.execute(query, args).fetchone()
    return dict(row) if row else None


def list_jobs(user_id: str, *, status: str | None = None, limit: int = 100) -> list[dict]:
    conn = get_conn()
    query, args = "SELECT * FROM cognition_jobs WHERE user_id=?", [user_id]
    if status:
        query += " AND status=?"
        args.append(status)
    query += " ORDER BY created_at DESC LIMIT ?"
    args.append(max(1, min(limit, 200)))
    result = []
    for row in conn.execute(query, args).fetchall():
        item = dict(row)
        item["payload"] = _dict(item.pop("payload_json", "{}"), {})
        result.append(item)
    return result


def list_recording_jobs(recording_id: str, user_id: str) -> list[dict]:
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM cognition_jobs WHERE recording_id=? AND user_id=? ORDER BY created_at",
        (recording_id, user_id),
    ).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        item["payload"] = _dict(item.pop("payload_json", "{}"), {})
        item["progress"] = _dict(item.pop("progress_json", "{}"), {})
        result.append(item)
    return result


def retry_job(job_id: str, user_id: str) -> dict | None:
    conn = get_conn()
    conn.execute(
        """UPDATE cognition_jobs SET status='queued', attempts=0, lease_until=NULL,run_after=NULL,
           error_text='', started_at=NULL, finished_at=NULL WHERE id=? AND user_id=?""",
        (job_id, user_id),
    )
    conn.commit()
    return get_job(job_id, user_id)


def claim_next_job(job_types: set[str] | None = None) -> dict | None:
    """Lease one queued job. The transaction makes multiple workers safe."""
    conn = get_conn()
    type_clause = ""
    params: list[Any] = [_now(), _now()]
    if job_types:
        placeholders = ",".join("?" for _ in job_types)
        type_clause = f" AND job_type IN ({placeholders})"
        params.extend(sorted(job_types))
    candidate_query = f"""SELECT * FROM cognition_jobs
       WHERE attempts < max_attempts AND (
            (status='queued' AND (run_after IS NULL OR run_after <= ?))
            OR (status='running' AND lease_until < ?)
       ){type_clause}
       ORDER BY created_at LIMIT 1"""
    if not conn.execute(candidate_query, params).fetchone():
        return None

    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute(candidate_query, params).fetchone()
        if not row:
            conn.commit()
            return None
        now = _now()
        lease = datetime.now(timezone.utc).timestamp() + 300
        lease_until = datetime.fromtimestamp(lease, timezone.utc).isoformat(timespec="seconds")
        lease_owner = uuid.uuid4().hex
        conn.execute(
            """UPDATE cognition_jobs SET status='running', attempts=attempts+1,
               lease_owner=?, lease_until=?, started_at=COALESCE(started_at,?) WHERE id=?""",
            (lease_owner, lease_until, now, row["id"]),
        )
        conn.commit()
        return get_job(row["id"])
    except Exception:
        conn.rollback()
        raise


def renew_job_lease(job_id: str, lease_owner: str, seconds: int = 300) -> bool:
    conn = get_conn()
    lease = datetime.now(timezone.utc).timestamp() + max(30, seconds)
    lease_until = datetime.fromtimestamp(lease, timezone.utc).isoformat(timespec="seconds")
    cur = conn.execute(
        "UPDATE cognition_jobs SET lease_until=? WHERE id=? AND status='running' AND lease_owner=?",
        (lease_until, job_id, lease_owner),
    )
    conn.commit()
    return cur.rowcount == 1


def update_job_progress(job_id: str, progress: dict) -> None:
    conn = get_conn()
    conn.execute(
        "UPDATE cognition_jobs SET progress_json=? WHERE id=?",
        (_json(progress), job_id),
    )
    conn.commit()


def update_job_payload(job_id: str, payload: dict, lease_owner: str | None = None) -> bool:
    """Persist durable upstream state while a worker owns a job."""
    query = "UPDATE cognition_jobs SET payload_json=? WHERE id=?"
    args: list[Any] = [_json(payload), job_id]
    if lease_owner:
        query += " AND status='running' AND lease_owner=?"
        args.append(lease_owner)
    conn = get_conn()
    cur = conn.execute(query, args)
    conn.commit()
    return cur.rowcount == 1


def reschedule_job(
    job_id: str, *, payload: dict, progress: dict, delay_seconds: int,
    lease_owner: str | None = None,
) -> bool:
    """Release a polling job without spending its retry budget.

    Pending upstream ASR is a normal state, not a failed worker attempt.
    """
    run_after = (
        datetime.now(timezone.utc) + timedelta(seconds=max(1, delay_seconds))
    ).isoformat(timespec="seconds")
    query = """UPDATE cognition_jobs
       SET status='queued', attempts=CASE WHEN attempts>0 THEN attempts-1 ELSE 0 END,
           lease_owner='', lease_until=NULL, run_after=?, payload_json=?,
           progress_json=?, error_text=''
       WHERE id=?"""
    args: list[Any] = [run_after, _json(payload), _json(progress), job_id]
    if lease_owner:
        query += " AND lease_owner=?"
        args.append(lease_owner)
    conn = get_conn()
    cur = conn.execute(query, args)
    conn.commit()
    return cur.rowcount == 1


def complete_job(job_id: str, lease_owner: str | None = None) -> None:
    conn = get_conn()
    query = (
        "UPDATE cognition_jobs SET status='completed', lease_owner='', lease_until=NULL,run_after=NULL, "
        "progress_json='{}', error_text='', finished_at=? WHERE id=?"
    )
    args: list[Any] = [_now(), job_id]
    if lease_owner:
        query += " AND lease_owner=?"
        args.append(lease_owner)
    conn.execute(query, args)
    conn.commit()


def enqueue_job(
    *, user_id: str, job_type: str, payload: dict | None = None,
    source_id: str | None = None, recording_id: str | None = None,
    dedupe_key: str = "", max_attempts: int = 3, run_after: str | None = None,
) -> dict:
    conn = get_conn()
    job_id = f"job_{uuid.uuid4().hex}"
    now = _now()
    try:
        conn.execute(
            """INSERT INTO cognition_jobs (
                id,user_id,source_id,recording_id,job_type,payload_json,dedupe_key,
                status,max_attempts,run_after,created_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (
                job_id, user_id, source_id, recording_id, job_type, _json(payload or {}),
                dedupe_key, "queued", max(1, max_attempts), run_after, now,
            ),
        )
        conn.commit()
    except sqlite3.IntegrityError:
        conn.rollback()
        if not dedupe_key:
            raise
        row = conn.execute(
            "SELECT * FROM cognition_jobs WHERE dedupe_key=?", (dedupe_key,)
        ).fetchone()
        return dict(row) if row else {}
    return get_job(job_id) or {}


def fail_job(job_id: str, error: str, lease_owner: str | None = None) -> str:
    conn = get_conn()
    row = conn.execute("SELECT attempts,max_attempts FROM cognition_jobs WHERE id=?", (job_id,)).fetchone()
    attempts = int(row["attempts"] or 0) if row else 0
    status = "failed" if row and attempts >= row["max_attempts"] else "queued"
    run_after = None
    if status == "queued":
        delay_seconds = min(120, 5 * (2 ** max(0, attempts - 1)))
        run_after = (
            datetime.now(timezone.utc) + timedelta(seconds=delay_seconds)
        ).isoformat(timespec="seconds")
    query = (
        "UPDATE cognition_jobs SET status=?, lease_owner='', lease_until=NULL, "
        "run_after=?,error_text=? WHERE id=?"
    )
    args: list[Any] = [status, run_after, error[:2000], job_id]
    if lease_owner:
        query += " AND lease_owner=?"
        args.append(lease_owner)
    conn.execute(query, args)
    conn.commit()
    return status


def recover_stuck_jobs() -> dict[str, int]:
    """Release expired leases and terminalize jobs that exhausted retries."""
    conn = get_conn()
    now = _now()
    exhausted = conn.execute(
        """UPDATE cognition_jobs SET status='failed',lease_owner='',lease_until=NULL,
           run_after=NULL,error_text=CASE WHEN error_text='' THEN 'retry budget exhausted' ELSE error_text END
           WHERE attempts>=max_attempts AND (
             status='queued' OR (status='running' AND lease_until<?)
           )""",
        (now,),
    ).rowcount
    released = conn.execute(
        """UPDATE cognition_jobs SET status='queued',lease_owner='',lease_until=NULL,
           run_after=NULL WHERE status='running' AND lease_until<? AND attempts<max_attempts""",
        (now,),
    ).rowcount
    conn.commit()
    return {"released": released, "failed": exhausted}


def queue_snapshot() -> dict:
    conn = get_conn()
    rows = conn.execute(
        """SELECT job_type,status,COUNT(*) AS count,MIN(created_at) AS oldest
           FROM cognition_jobs
           WHERE status IN ('queued','running','failed')
           GROUP BY job_type,status"""
    ).fetchall()
    by_type: dict[str, dict[str, int]] = {}
    totals = {"queued": 0, "running": 0, "failed": 0}
    oldest_queued_at = None
    for row in rows:
        job_type = row["job_type"]
        status = row["status"]
        count = int(row["count"])
        by_type.setdefault(job_type, {})[status] = count
        totals[status] += count
        if status == "queued" and row["oldest"]:
            if oldest_queued_at is None or row["oldest"] < oldest_queued_at:
                oldest_queued_at = row["oldest"]
    return {
        **totals,
        "oldest_queued_at": oldest_queued_at,
        "by_type": by_type,
    }


def set_source_status(source_id: str, status: str) -> None:
    conn = get_conn()
    conn.execute("UPDATE sources SET status=?, updated_at=? WHERE id=?", (status, _now(), source_id))
    conn.commit()


def create_claim(
    *, user_id: str, source_id: str, content: str, network: str,
    entities: list[str] | None = None, confidence: float = 0.5,
    occurred_at: str | None = None, quote: str = "",
) -> dict:
    conn = get_conn()
    now = _now()
    clean_content = content.strip()
    # Exact semantic-equivalent claims are merged at the write boundary. A
    # later vector/LLM pass can propose fuzzy merges, but must not silently
    # collapse two differently worded facts without review.
    existing = conn.execute(
        "SELECT id FROM claims_v2 WHERE user_id=? AND network=? AND content=? AND status='active'",
        (user_id, network, clean_content),
    ).fetchone()
    if existing:
        claim_id = existing["id"]
        evidence_exists = conn.execute(
            "SELECT 1 FROM claim_evidence WHERE claim_id=? AND source_id=? AND quote=?",
            (claim_id, source_id, quote or clean_content[:500]),
        ).fetchone()
        if not evidence_exists:
            conn.execute(
                "INSERT INTO claim_evidence (id,claim_id,source_id,quote,created_at) VALUES (?,?,?,?,?)",
                (f"evd_{uuid.uuid4().hex}", claim_id, source_id, quote or clean_content[:500], now),
            )
            conn.commit()
        return get_claim(claim_id) or {}
    claim_id = f"clm_{uuid.uuid4().hex}"
    conn.execute(
        """INSERT INTO claims_v2 (
            id,user_id,network,content,entities_json,occurred_at,confidence,created_at,updated_at
        ) VALUES (?,?,?,?,?,?,?,?,?)""",
        (claim_id, user_id, network, clean_content, _json(entities or []), occurred_at,
         max(0.0, min(float(confidence), 1.0)), now, now),
    )
    conn.execute(
        "INSERT INTO claim_evidence (id,claim_id,source_id,quote,created_at) VALUES (?,?,?,?,?)",
        (f"evd_{uuid.uuid4().hex}", claim_id, source_id, quote or clean_content[:500], now),
    )
    for name in dict.fromkeys(item.strip() for item in (entities or []) if item and item.strip()):
        entity = conn.execute(
            "SELECT id FROM entities_v2 WHERE user_id=? AND canonical_name=?", (user_id, name)
        ).fetchone()
        entity_id = entity["id"] if entity else f"ent_{uuid.uuid4().hex}"
        if not entity:
            conn.execute(
                """INSERT INTO entities_v2 (
                    id,user_id,canonical_name,aliases_json,created_at,updated_at
                ) VALUES (?,?,?,?,?,?)""",
                (entity_id, user_id, name, _json([name]), now, now),
            )
        conn.execute(
            "INSERT OR IGNORE INTO claim_entities (claim_id,entity_id,created_at) VALUES (?,?,?)",
            (claim_id, entity_id, now),
        )
    conn.commit()
    return get_claim(claim_id) or {}


def get_claim(claim_id: str) -> dict | None:
    conn = get_conn()
    row = conn.execute("SELECT * FROM claims_v2 WHERE id=?", (claim_id,)).fetchone()
    if not row:
        return None
    data = dict(row)
    data["entities"] = _list(data.pop("entities_json", "[]"))
    normalized = [row["canonical_name"] for row in conn.execute(
        """SELECT e.canonical_name FROM entities_v2 e JOIN claim_entities ce ON ce.entity_id=e.id
           WHERE ce.claim_id=? ORDER BY e.canonical_name""", (claim_id,)
    ).fetchall()]
    if normalized:
        data["entities"] = normalized
    data["evidence"] = [dict(item) for item in conn.execute(
        "SELECT source_id,quote,support_type FROM claim_evidence WHERE claim_id=?", (claim_id,)
    ).fetchall()]
    return data


def list_claims_for_source(source_id: str) -> list[dict]:
    conn = get_conn()
    rows = conn.execute(
        """SELECT c.* FROM claims_v2 c JOIN claim_evidence e ON e.claim_id=c.id
           WHERE e.source_id=? ORDER BY c.created_at""",
        (source_id,),
    ).fetchall()
    return [get_claim(row["id"]) for row in rows]


def create_project(
    *, user_id: str, name: str, description: str = "", goal: str = "",
    stage: str = "active", tags: list[str] | None = None,
    constraints: dict | None = None, metrics: dict | None = None,
    background_html: str = "", start_at: str | None = None, target_at: str | None = None,
) -> dict:
    conn = get_conn()
    project_id = f"prj_{uuid.uuid4().hex}"
    now = _now()
    conn.execute(
        """INSERT INTO projects_v2 (
            id,user_id,name,description,goal,stage,constraints_json,metrics_json,tags_json,
            background_html,start_at,target_at,created_at,updated_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (project_id, user_id, name.strip(), description.strip(), goal.strip(), stage,
         _json(constraints or {}), _json(metrics or {}), _json(tags or []),
         background_html.strip(), start_at, target_at, now, now),
    )
    conn.execute(
        "INSERT INTO project_state (project_id,user_id,dirty,updated_at) VALUES (?,?,1,?)",
        (project_id, user_id, now),
    )
    conn.commit()
    ensure_thread(user_id=user_id, scope_type="project", scope_id=project_id, title=name.strip())
    add_event(
        user_id=user_id, project_id=project_id, event_type="project_created",
        actor="user", object_type="project", object_id=project_id,
        payload={"name": name.strip(), "goal": goal.strip()},
    )
    project = get_project(project_id, user_id) or {}
    _capture_project_profile(project)
    return project


def update_project(*, project_id: str, user_id: str, changes: dict[str, Any]) -> dict | None:
    project = get_project(project_id, user_id)
    if not project:
        return None
    scalar_fields = {
        "name", "description", "goal", "stage", "status", "background_html", "start_at", "target_at",
    }
    json_fields = {"constraints": "constraints_json", "metrics": "metrics_json", "tags": "tags_json"}
    assignments: list[str] = []
    values: list[Any] = []
    changed: dict[str, Any] = {}
    for key in scalar_fields:
        if key not in changes:
            continue
        value = changes[key]
        if key in {"name", "description", "goal", "stage", "status", "background_html"}:
            value = str(value or "").strip()
        assignments.append(f"{key}=?")
        values.append(value)
        changed[key] = value
    for key, column in json_fields.items():
        if key not in changes:
            continue
        assignments.append(f"{column}=?")
        values.append(_json(changes[key]))
        changed[key] = changes[key]
    if not assignments:
        return project
    now = _now()
    assignments.append("updated_at=?")
    values.extend([now, project_id, user_id])
    conn = get_conn()
    conn.execute(
        f"UPDATE projects_v2 SET {','.join(assignments)} WHERE id=? AND user_id=?",
        values,
    )
    conn.execute(
        "UPDATE project_state SET dirty=1,updated_at=? WHERE project_id=? AND user_id=?",
        (now, project_id, user_id),
    )
    conn.commit()
    if "name" in changed:
        thread = ensure_thread(user_id=user_id, scope_type="project", scope_id=project_id, title=changed["name"])
        conn.execute("UPDATE cognition_threads SET title=?,updated_at=? WHERE id=?", (changed["name"], now, thread["id"]))
        conn.commit()
    add_event(
        user_id=user_id, project_id=project_id, event_type="project_updated",
        actor="user", object_type="project", object_id=project_id, payload=changed,
    )
    updated = get_project(project_id, user_id)
    if updated and {"goal", "description", "background_html"}.intersection(changed):
        _capture_project_profile(updated)
    return updated


def _capture_project_profile(project: dict) -> None:
    """Persist project background and goal as evidence-backed cognitive input."""
    background = project.get("background_html", "") or project.get("description", "")
    goal = project.get("goal", "")
    if not background and not goal:
        return
    source, job, duplicate = create_source(
        user_id=project["user_id"], source_type="project_profile",
        content=json.dumps({
            "current_intent": goal,
            "detailed_summary": background or f"Project goal: {goal}",
            "tags": project.get("tags", []),
        }, ensure_ascii=False),
        origin="project_workspace", title=f"{project['name']} background and goal",
        external_id=f"{project['id']}:profile:{project['updated_at']}",
        metadata={"project_id": project["id"], "kind": "project_profile"},
        project_ids=[project["id"]],
    )
    if duplicate or not job:
        return
    try:
        from cognition.pipeline import process_source
        process_source(source["id"])
        complete_job(job["id"])
    except Exception as exc:
        conn = get_conn()
        conn.execute(
            "UPDATE cognition_jobs SET status='failed',error_text=?,finished_at=? WHERE id=?",
            (str(exc)[:2000], _now(), job["id"]),
        )
        conn.execute("UPDATE sources SET status='failed',updated_at=? WHERE id=?", (_now(), source["id"]))
        conn.commit()


def get_project(project_id: str, user_id: str | None = None) -> dict | None:
    conn = get_conn()
    query = "SELECT * FROM projects_v2 WHERE id=?"
    args: list[Any] = [project_id]
    if user_id is not None:
        query += " AND user_id=?"
        args.append(user_id)
    return _project_row(conn.execute(query, args).fetchone())


def count_projects(user_id: str) -> int:
    conn = get_conn()
    return int(conn.execute(
        "SELECT COUNT(*) AS count FROM projects_v2 WHERE user_id=?", (user_id,),
    ).fetchone()["count"])


def list_projects(user_id: str, limit: int = 100, offset: int = 0) -> list[dict]:
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM projects_v2 WHERE user_id=? ORDER BY updated_at DESC LIMIT ? OFFSET ?",
        (user_id, max(1, min(limit, 200)), max(0, offset)),
    ).fetchall()
    return [_project_row(row) for row in rows]


def delete_project(*, project_id: str, user_id: str) -> dict | None:
    """Delete one project without deleting its source evidence."""
    conn = get_conn()
    project = conn.execute(
        "SELECT id FROM projects_v2 WHERE id=? AND user_id=?", (project_id, user_id)
    ).fetchone()
    if not project:
        return None

    # Source metadata mirrors manual labels for exports and integrations. Remove
    # the deleted id there as well, while keeping the source itself intact.
    source_rows = conn.execute(
        "SELECT id,metadata_json FROM sources WHERE user_id=?", (user_id,)
    ).fetchall()
    sources_updated = 0
    for source in source_rows:
        metadata = _dict(source["metadata_json"], {})
        project_ids = metadata.get("project_ids") if isinstance(metadata, dict) else None
        if not isinstance(project_ids, list) or project_id not in project_ids:
            continue
        metadata["project_ids"] = [item for item in project_ids if item != project_id]
        conn.execute(
            "UPDATE sources SET metadata_json=?,updated_at=? WHERE id=?",
            (_json(metadata), _now(), source["id"]),
        )
        sources_updated += 1

    # A queued nightly/manual insight job would otherwise retry against a
    # missing project after deletion. Match payload structurally, not by text.
    job_ids = []
    for job in conn.execute(
        "SELECT id,payload_json FROM cognition_jobs WHERE user_id=? AND job_type='project_insight'",
        (user_id,),
    ).fetchall():
        if _dict(job["payload_json"], {}).get("project_id") == project_id:
            job_ids.append(job["id"])
    if job_ids:
        placeholders = ",".join("?" for _ in job_ids)
        conn.execute(f"DELETE FROM cognition_jobs WHERE id IN ({placeholders})", job_ids)

    removed = {
        "sources_updated": sources_updated,
        "memberships": conn.execute(
            "DELETE FROM project_memberships WHERE project_id=?", (project_id,)
        ).rowcount,
        "insights": conn.execute(
            "DELETE FROM insights_v2 WHERE project_id=? AND user_id=?", (project_id, user_id)
        ).rowcount,
        "jobs": len(job_ids),
        "feedback": conn.execute(
            "DELETE FROM cognition_feedback WHERE user_id=? AND target_type='project' AND target_id=?",
            (user_id, project_id),
        ).rowcount,
        "tasks": conn.execute(
            "DELETE FROM project_tasks WHERE user_id=? AND project_id=?", (user_id, project_id)
        ).rowcount,
        "events": conn.execute(
            "DELETE FROM cognition_events WHERE user_id=? AND project_id=?", (user_id, project_id)
        ).rowcount,
    }
    thread_rows = conn.execute(
        "SELECT id FROM cognition_threads WHERE user_id=? AND scope_type='project' AND scope_id=?",
        (user_id, project_id),
    ).fetchall()
    for thread in thread_rows:
        conn.execute("DELETE FROM cognition_messages WHERE thread_id=?", (thread["id"],))
    removed["threads"] = conn.execute(
        "DELETE FROM cognition_threads WHERE user_id=? AND scope_type='project' AND scope_id=?",
        (user_id, project_id),
    ).rowcount
    conn.execute("DELETE FROM project_state WHERE project_id=? AND user_id=?", (project_id, user_id))
    conn.execute("DELETE FROM projects_v2 WHERE id=? AND user_id=?", (project_id, user_id))
    conn.commit()
    return {"project_id": project_id, "removed": removed}


def add_membership(
    *, project_id: str, object_type: str, object_id: str,
    assignment_source: str, confidence: float, status: str, reason: dict | None = None,
) -> dict:
    conn = get_conn()
    now = _now()
    existing = conn.execute(
        "SELECT id FROM project_memberships WHERE project_id=? AND object_type=? AND object_id=?",
        (project_id, object_type, object_id),
    ).fetchone()
    if existing:
        membership_id = existing["id"]
        conn.execute(
            """UPDATE project_memberships SET assignment_source=?, confidence=?, status=?,
               reason_json=?, updated_at=? WHERE id=?""",
            (assignment_source, confidence, status, _json(reason or {}), now, membership_id),
        )
    else:
        membership_id = f"mbr_{uuid.uuid4().hex}"
        conn.execute(
            """INSERT INTO project_memberships (
                id,project_id,object_type,object_id,assignment_source,confidence,status,reason_json,created_at,updated_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (membership_id, project_id, object_type, object_id, assignment_source,
             confidence, status, _json(reason or {}), now, now),
        )
    conn.execute("UPDATE projects_v2 SET updated_at=? WHERE id=?", (now, project_id))
    project = conn.execute("SELECT user_id FROM projects_v2 WHERE id=?", (project_id,)).fetchone()
    if project:
        conn.execute(
            """INSERT INTO project_state (project_id,user_id,dirty,last_source_at,updated_at)
               VALUES (?,?,1,?,?)
               ON CONFLICT(project_id) DO UPDATE SET dirty=1,last_source_at=excluded.last_source_at,updated_at=excluded.updated_at""",
            (project_id, project["user_id"], now, now),
        )
    conn.commit()
    row = conn.execute("SELECT * FROM project_memberships WHERE id=?", (membership_id,)).fetchone()
    data = dict(row)
    data["reason"] = _dict(data.pop("reason_json", "{}"), {})
    return data


def set_source_projects(*, user_id: str, source_id: str, project_ids: list[str]) -> list[dict]:
    """Apply a user's multi-project decision without deleting audit history."""
    source = get_source(source_id, user_id)
    if not source:
        raise ValueError("source not found")
    desired_ids = list(dict.fromkeys(project_ids))
    available_ids = {project["id"] for project in list_projects(user_id, limit=200)}
    invalid_ids = set(desired_ids) - available_ids
    if invalid_ids:
        raise ValueError("one or more projects do not belong to the current user")

    conn = get_conn()
    existing_rows = conn.execute(
        """SELECT m.project_id,m.status FROM project_memberships m JOIN projects_v2 p ON p.id=m.project_id
           WHERE p.user_id=? AND m.object_type='source' AND m.object_id=?""",
        (user_id, source_id),
    ).fetchall()
    existing_ids = {row["project_id"] for row in existing_rows}
    for project_id in available_ids:
        if project_id not in desired_ids and project_id not in existing_ids:
            continue
        add_membership(
            project_id=project_id, object_type="source", object_id=source_id,
            assignment_source="user", confidence=1.0,
            status="confirmed" if project_id in desired_ids else "rejected",
            reason={"kind": "dashboard_project_labels"},
        )

    metadata = dict(source.get("metadata") or {})
    metadata["project_ids"] = desired_ids
    conn.execute("UPDATE sources SET metadata_json=?,updated_at=? WHERE id=?", (_json(metadata), _now(), source_id))
    conn.commit()
    return next((item["projects"] for item in list_sources(user_id, limit=200) if item["id"] == source_id), [])


def create_insight(
    *, user_id: str, project_id: str | None, insight_type: str, title: str,
    content: str, confidence: float, evidence: list[dict] | None = None,
    trigger_type: str = "scheduled",
) -> dict:
    conn = get_conn()
    now = _now()
    insight_id = f"ins_{uuid.uuid4().hex}"
    previous = conn.execute(
        """SELECT id,version FROM insights_v2
           WHERE user_id=? AND project_id IS ? AND insight_type=? AND status='active'
           ORDER BY created_at DESC LIMIT 1""",
        (user_id, project_id, insight_type),
    ).fetchone()
    version = int(previous["version"] or 1) + 1 if previous else 1
    if previous:
        conn.execute(
            "UPDATE insights_v2 SET status='superseded',updated_at=? WHERE id=?",
            (now, previous["id"]),
        )
    conn.execute(
        """INSERT INTO insights_v2 (
            id,user_id,project_id,insight_type,title,content,confidence,evidence_json,
            scope_type,scope_id,version,supersedes_id,trigger_type,created_at,updated_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (insight_id, user_id, project_id, insight_type, title, content,
         max(0.0, min(float(confidence), 1.0)), _json(evidence or []),
         "project" if project_id else "global", project_id or "", version,
         previous["id"] if previous else None, trigger_type, now, now),
    )
    conn.commit()
    row = conn.execute("SELECT * FROM insights_v2 WHERE id=?", (insight_id,)).fetchone()
    data = dict(row)
    data["evidence"] = _list(data.pop("evidence_json", "[]"))
    return data


def count_insights(user_id: str, project_id: str | None = None) -> int:
    conn = get_conn()
    query = "SELECT COUNT(*) AS count FROM insights_v2 WHERE user_id=?"
    args: list[Any] = [user_id]
    if project_id:
        query += " AND project_id=?"
        args.append(project_id)
    return int(conn.execute(query, args).fetchone()["count"])


def list_insights(
    user_id: str, project_id: str | None = None, limit: int = 30, offset: int = 0,
) -> list[dict]:
    conn = get_conn()
    query = "SELECT * FROM insights_v2 WHERE user_id=?"
    args: list[Any] = [user_id]
    if project_id:
        query += " AND project_id=?"
        args.append(project_id)
    query += " ORDER BY created_at DESC LIMIT ? OFFSET ?"
    args.extend([max(1, min(limit, 100)), max(0, offset)])
    result = []
    for row in conn.execute(query, args).fetchall():
        data = dict(row)
        data["evidence"] = _list(data.pop("evidence_json", "[]"))
        result.append(data)
    return result


def dirty_projects() -> list[dict]:
    conn = get_conn()
    rows = conn.execute(
        """SELECT p.* FROM projects_v2 p JOIN project_state s ON s.project_id=p.id
           WHERE s.dirty=1 AND p.status='active' ORDER BY s.updated_at"""
    ).fetchall()
    return [_project_row(row) for row in rows]


def mark_project_insighted(project_id: str) -> None:
    conn = get_conn()
    now = _now()
    conn.execute(
        "UPDATE project_state SET dirty=0,last_insight_at=?,updated_at=? WHERE project_id=?",
        (now, now, project_id),
    )
    conn.commit()


def get_meta(key: str) -> str:
    conn = get_conn()
    row = conn.execute("SELECT value FROM cognition_meta WHERE key=?", (key,)).fetchone()
    return row["value"] if row else ""


def set_meta(key: str, value: str) -> None:
    conn = get_conn()
    conn.execute(
        """INSERT INTO cognition_meta (key,value,updated_at) VALUES (?,?,?)
           ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at""",
        (key, value, _now()),
    )
    conn.commit()


def list_memberships(project_id: str, object_type: str | None = None) -> list[dict]:
    conn = get_conn()
    query = "SELECT * FROM project_memberships WHERE project_id=?"
    args: list[Any] = [project_id]
    if object_type:
        query += " AND object_type=?"
        args.append(object_type)
    query += " ORDER BY updated_at DESC"
    rows = conn.execute(query, args).fetchall()
    result = []
    for row in rows:
        data = dict(row)
        data["reason"] = _dict(data.pop("reason_json", "{}"), {})
        result.append(data)
    return result


def project_brief(project_id: str, user_id: str) -> dict | None:
    project = get_project(project_id, user_id)
    if not project:
        return None
    conn = get_conn()
    memberships = list_memberships(project_id, "source")
    source_ids = [item["object_id"] for item in memberships if item["status"] != "rejected"]
    claims: list[dict] = []
    if source_ids:
        placeholders = ",".join("?" for _ in source_ids)
        rows = conn.execute(
            f"""SELECT DISTINCT c.* FROM claims_v2 c JOIN claim_evidence e ON e.claim_id=c.id
                WHERE e.source_id IN ({placeholders}) ORDER BY c.created_at DESC LIMIT 30""",
            source_ids,
        ).fetchall()
        claims = [get_claim(row["id"]) for row in rows]
    insights = [dict(row) for row in conn.execute(
        "SELECT * FROM insights_v2 WHERE project_id=? AND status='active' ORDER BY created_at DESC LIMIT 10",
        (project_id,),
    ).fetchall()]
    return {"project": project, "memberships": memberships, "claims": claims, "insights": insights}


def search_context(user_id: str, query: str, project_ids: list[str] | None = None, limit: int = 12) -> dict:
    """Hybrid lexical and persisted-vector retrieval over evidence-bound objects."""
    conn = get_conn()
    from cognition.semantic import dot, encode
    vector_model_id, query_vector = encode(query)
    words = [word for word in query.lower().split() if len(word) > 1][:8]
    cjk = "".join(char for char in query.lower() if '\u4e00' <= char <= '\u9fff')
    words.extend(cjk[index:index + 2] for index in range(max(0, len(cjk) - 1)))
    words = list(dict.fromkeys(words))[:16]
    rows = conn.execute(
        "SELECT * FROM claims_v2 WHERE user_id=? AND status='active' ORDER BY created_at DESC LIMIT 500",
        (user_id,),
    ).fetchall()
    project_source_ids: set[str] | None = None
    if project_ids:
        placeholders = ",".join("?" for _ in project_ids)
        mrows = conn.execute(
            f"SELECT object_id FROM project_memberships WHERE project_id IN ({placeholders}) AND object_type='source' AND status != 'rejected'",
            project_ids,
        ).fetchall()
        project_source_ids = {row["object_id"] for row in mrows}

    scored: list[tuple[float, dict]] = []
    claim_vector_rows = conn.execute(
        "SELECT object_id,vector_json FROM semantic_vectors WHERE user_id=? AND object_type='claim' AND model_id=?",
        (user_id, vector_model_id),
    ).fetchall()
    claim_vectors = {row["object_id"]: _dict(row["vector_json"], []) for row in claim_vector_rows}
    for row in rows:
        claim = get_claim(row["id"])
        if project_source_ids is not None:
            evidence_ids = {item["source_id"] for item in claim["evidence"]}
            if not evidence_ids.intersection(project_source_ids):
                continue
        text = claim["content"].lower()
        lexical_score = sum(1 for word in words if word in text)
        semantic_score = dot(query_vector, claim_vectors.get(claim["id"], []))
        if not words:
            lexical_score = 1
        if lexical_score or semantic_score >= 0.18:
            scored.append((lexical_score + semantic_score * 1.5 + claim["confidence"] * 0.1, claim))
    scored.sort(key=lambda item: item[0], reverse=True)
    cap = max(1, min(limit, 50))
    return {
        "query": query,
        "claims": [item[1] for item in scored[:cap]],
    }


def add_feedback(*, user_id: str, target_type: str, target_id: str, action: str, correction: dict | None = None) -> dict:
    conn = get_conn()
    feedback_id = f"fbk_{uuid.uuid4().hex}"
    conn.execute(
        "INSERT INTO cognition_feedback (id,user_id,target_type,target_id,action,correction_json,created_at) VALUES (?,?,?,?,?,?,?)",
        (feedback_id, user_id, target_type, target_id, action, _json(correction or {}), _now()),
    )
    conn.commit()
    return {"id": feedback_id, "target_type": target_type, "target_id": target_id, "action": action}


def ensure_thread(*, user_id: str, scope_type: str, scope_id: str = "", title: str = "") -> dict:
    conn = get_conn()
    row = conn.execute(
        "SELECT * FROM cognition_threads WHERE user_id=? AND scope_type=? AND scope_id=?",
        (user_id, scope_type, scope_id),
    ).fetchone()
    if row:
        return dict(row)
    thread_id = f"thr_{uuid.uuid4().hex}"
    now = _now()
    conn.execute(
        """INSERT INTO cognition_threads
           (id,user_id,scope_type,scope_id,title,status,created_at,updated_at)
           VALUES (?,?,?,?,?,'active',?,?)""",
        (thread_id, user_id, scope_type, scope_id, title.strip(), now, now),
    )
    conn.commit()
    return dict(conn.execute("SELECT * FROM cognition_threads WHERE id=?", (thread_id,)).fetchone())


def get_thread(thread_id: str, user_id: str) -> dict | None:
    row = get_conn().execute(
        "SELECT * FROM cognition_threads WHERE id=? AND user_id=?", (thread_id, user_id)
    ).fetchone()
    return dict(row) if row else None


def add_message(
    *, thread_id: str, user_id: str, role: str, content: str,
    content_format: str = "markdown", message_type: str = "chat",
    reply_to_id: str | None = None, source_id: str | None = None,
    metadata: dict | None = None,
) -> dict:
    if not get_thread(thread_id, user_id):
        raise ValueError("thread not found")
    conn = get_conn()
    message_id = f"msg_{uuid.uuid4().hex}"
    now = _now()
    conn.execute(
        """INSERT INTO cognition_messages
           (id,thread_id,user_id,role,content,content_format,message_type,reply_to_id,source_id,metadata_json,created_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        (message_id, thread_id, user_id, role, content.strip(), content_format, message_type,
         reply_to_id, source_id, _json(metadata or {}), now),
    )
    conn.execute("UPDATE cognition_threads SET updated_at=? WHERE id=?", (now, thread_id))
    conn.commit()
    return _message_row(conn.execute("SELECT * FROM cognition_messages WHERE id=?", (message_id,)).fetchone()) or {}


def list_messages(*, thread_id: str, user_id: str, limit: int = 100) -> list[dict]:
    if not get_thread(thread_id, user_id):
        return []
    rows = get_conn().execute(
        """SELECT * FROM (
               SELECT * FROM cognition_messages WHERE thread_id=? AND user_id=?
               ORDER BY created_at DESC LIMIT ?
           ) ORDER BY created_at""",
        (thread_id, user_id, max(1, min(limit, 500))),
    ).fetchall()
    return [_message_row(row) for row in rows]


def add_event(
    *, user_id: str, event_type: str, actor: str = "system",
    project_id: str | None = None, object_type: str = "", object_id: str = "",
    payload: dict | None = None,
) -> dict:
    conn = get_conn()
    event_id = f"evt_{uuid.uuid4().hex}"
    now = _now()
    conn.execute(
        """INSERT INTO cognition_events
           (id,user_id,project_id,event_type,actor,object_type,object_id,payload_json,created_at)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        (event_id, user_id, project_id, event_type, actor, object_type, object_id,
         _json(payload or {}), now),
    )
    conn.commit()
    return {
        "id": event_id, "project_id": project_id, "event_type": event_type,
        "actor": actor, "object_type": object_type, "object_id": object_id,
        "payload": payload or {}, "created_at": now,
    }


def list_events(*, user_id: str, project_id: str | None = None, limit: int = 100) -> list[dict]:
    conn = get_conn()
    query = "SELECT * FROM cognition_events WHERE user_id=?"
    args: list[Any] = [user_id]
    if project_id is not None:
        query += " AND project_id=?"
        args.append(project_id)
    query += " ORDER BY created_at DESC LIMIT ?"
    args.append(max(1, min(limit, 500)))
    result = []
    for row in conn.execute(query, args).fetchall():
        item = dict(row)
        item["payload"] = _dict(item.pop("payload_json", "{}"), {})
        result.append(item)
    return result


def create_task(
    *, user_id: str, title: str, project_id: str | None = None,
    description: str = "", due_at: str | None = None, priority: str = "normal",
    source_message_id: str | None = None, reminder: dict | None = None,
) -> dict:
    if project_id and not get_project(project_id, user_id):
        raise ValueError("project not found")
    conn = get_conn()
    task_id = f"tsk_{uuid.uuid4().hex}"
    now = _now()
    conn.execute(
        """INSERT INTO project_tasks
           (id,user_id,project_id,title,description,status,priority,due_at,source_message_id,reminder_json,created_at,updated_at)
           VALUES (?,?,?,?,?,'pending',?,?,?,?,?,?)""",
        (task_id, user_id, project_id, title.strip(), description.strip(), priority,
         due_at, source_message_id, _json(reminder or {}), now, now),
    )
    conn.commit()
    add_event(
        user_id=user_id, project_id=project_id, event_type="task_created", actor="user",
        object_type="task", object_id=task_id, payload={"title": title.strip(), "due_at": due_at},
    )
    return _task_row(conn.execute("SELECT * FROM project_tasks WHERE id=?", (task_id,)).fetchone()) or {}


def list_tasks(*, user_id: str, project_id: str | None = None, limit: int = 100) -> list[dict]:
    conn = get_conn()
    query = "SELECT * FROM project_tasks WHERE user_id=?"
    args: list[Any] = [user_id]
    if project_id is not None:
        query += " AND project_id=?"
        args.append(project_id)
    query += " ORDER BY CASE status WHEN 'pending' THEN 0 ELSE 1 END,due_at,created_at DESC LIMIT ?"
    args.append(max(1, min(limit, 500)))
    return [_task_row(row) for row in conn.execute(query, args).fetchall()]


def update_task(*, task_id: str, user_id: str, changes: dict[str, Any]) -> dict | None:
    conn = get_conn()
    row = conn.execute("SELECT * FROM project_tasks WHERE id=? AND user_id=?", (task_id, user_id)).fetchone()
    if not row:
        return None
    allowed = {"title", "description", "status", "priority", "due_at"}
    assignments, values = [], []
    for key in allowed:
        if key in changes:
            assignments.append(f"{key}=?")
            values.append(changes[key])
    if not assignments:
        return _task_row(row)
    assignments.append("updated_at=?")
    values.extend([_now(), task_id, user_id])
    conn.execute(f"UPDATE project_tasks SET {','.join(assignments)} WHERE id=? AND user_id=?", values)
    conn.commit()
    return _task_row(conn.execute("SELECT * FROM project_tasks WHERE id=?", (task_id,)).fetchone())


def list_l4_items(*, user_id: str, status: str | None = None, limit: int = 100) -> list[dict]:
    conn = get_conn()
    query = "SELECT * FROM l4_profile_items WHERE user_id=?"
    args: list[Any] = [user_id]
    if status:
        query += " AND status=?"
        args.append(status)
    query += " ORDER BY updated_at DESC LIMIT ?"
    args.append(max(1, min(limit, 500)))
    return [_l4_row(row) for row in conn.execute(query, args).fetchall()]


def create_l4_item(
    *, user_id: str, title: str, content_html: str, category: str = "general",
    confidence: float = 0.5, status: str = "suggested", evidence: list | None = None,
    supersedes_id: str | None = None,
) -> dict:
    conn = get_conn()
    item_id = f"l4_{uuid.uuid4().hex}"
    now = _now()
    normalized_confidence = max(0.0, min(float(confidence), 1.0))
    if status == "suggested" and normalized_confidence >= 0.8:
        status = "confirmed"
    version = 1
    if supersedes_id:
        previous = conn.execute(
            "SELECT version FROM l4_profile_items WHERE id=? AND user_id=?", (supersedes_id, user_id)
        ).fetchone()
        if previous:
            version = int(previous["version"]) + 1
            conn.execute(
                "UPDATE l4_profile_items SET status='superseded',updated_at=? WHERE id=?",
                (now, supersedes_id),
            )
    conn.execute(
        """INSERT INTO l4_profile_items
           (id,user_id,category,title,content_html,confidence,status,evidence_json,version,supersedes_id,created_at,updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
        (item_id, user_id, category, title.strip(), content_html.strip(),
         normalized_confidence, status, _json(evidence or []),
         version, supersedes_id, now, now),
    )
    conn.commit()
    return _l4_row(conn.execute("SELECT * FROM l4_profile_items WHERE id=?", (item_id,)).fetchone()) or {}


def update_l4_item(*, item_id: str, user_id: str, status: str) -> dict | None:
    conn = get_conn()
    if status not in {"suggested", "confirmed", "rejected", "superseded"}:
        raise ValueError("invalid L4 status")
    conn.execute(
        "UPDATE l4_profile_items SET status=?,updated_at=? WHERE id=? AND user_id=?",
        (status, _now(), item_id, user_id),
    )
    conn.commit()
    return _l4_row(conn.execute("SELECT * FROM l4_profile_items WHERE id=? AND user_id=?", (item_id, user_id)).fetchone())


def project_workspace(project_id: str, user_id: str) -> dict | None:
    project = get_project(project_id, user_id)
    if not project:
        return None
    thread = ensure_thread(user_id=user_id, scope_type="project", scope_id=project_id, title=project["name"])
    memberships = list_memberships(project_id, "source")
    source_ids = [item["object_id"] for item in memberships if item["status"] != "rejected"]
    sources = []
    if source_ids:
        conn = get_conn()
        placeholders = ",".join("?" for _ in source_ids)
        sources = [_source_row(row) for row in conn.execute(
            f"SELECT * FROM sources WHERE user_id=? AND id IN ({placeholders}) ORDER BY captured_at DESC",
            [user_id, *source_ids],
        ).fetchall()]
    return {
        "project": project,
        "thread": thread,
        "messages": list_messages(thread_id=thread["id"], user_id=user_id, limit=100),
        "sources": sources,
        "tasks": list_tasks(user_id=user_id, project_id=project_id, limit=100),
        "insights": list_insights(user_id, project_id, limit=30),
        "logs": list_events(user_id=user_id, project_id=project_id, limit=100),
    }


def memory_matrix(user_id: str) -> dict:
    thread = ensure_thread(user_id=user_id, scope_type="global", scope_id="", title="Memory Matrix")
    insights = [
        item for item in list_insights(user_id, limit=100)
        if item.get("status") == "active"
    ]
    project_names = {
        item["id"]: item["name"] for item in list_projects(user_id, limit=200)
    }
    for item in insights:
        item["project_name"] = project_names.get(item.get("project_id"), "")
    profile = list_l4_items(user_id=user_id, limit=100)
    visible_profile = [
        item for item in profile
        if item.get("status") == "confirmed"
        or (
            item.get("status") == "suggested"
            and float(item.get("confidence") or 0) < 0.55
        )
    ]
    pending_confirmations = sum(
        1 for item in visible_profile if item.get("status") == "suggested"
    )
    return {
        "thread": thread,
        "messages": list_messages(thread_id=thread["id"], user_id=user_id, limit=200),
        "profile": visible_profile,
        "insights": insights,
        "pending_confirmations": pending_confirmations,
    }
