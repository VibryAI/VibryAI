"""One-way, idempotent import helpers for legacy VibryServer data."""

from __future__ import annotations

import json

from cognition import store
from db.connection import get_conn


def import_legacy_recordings(*, user_id: str | None = None, limit: int = 1000, dry_run: bool = True) -> dict:
    """Queue historical transcripts as L0 Sources without changing legacy rows."""
    conn = get_conn()
    query = "SELECT * FROM recordings WHERE transcript != ''"
    args: list = []
    if user_id:
        query += " AND user_id=?"
        args.append(user_id)
    query += " ORDER BY created_at LIMIT ?"
    args.append(max(1, min(limit, 10000)))

    result = {"scanned": 0, "queued": 0, "duplicates": 0, "skipped": 0, "source_ids": []}
    for row in conn.execute(query, args).fetchall():
        result["scanned"] += 1
        transcript = (row["transcript"] or "").strip()
        if not transcript:
            result["skipped"] += 1
            continue
        if dry_run:
            continue
        source, _job, duplicate = store.create_source(
            user_id=row["user_id"],
            source_type="recording",
            content=transcript,
            origin="legacy_recordings",
            external_id=f"recording:{row['id']}",
            title=row["title"] or row["filename"] or "Recording",
            occurred_at=row["created_at"],
            derivation_type="transcript",
            metadata={
                "legacy_recording_id": row["id"],
                "legacy_category": row["category"] or "",
                "legacy_status": row["status"] or "",
                "duration_sec": row["duration_sec"] or 0,
            },
        )
        if duplicate:
            result["duplicates"] += 1
        else:
            result["queued"] += 1
            result["source_ids"].append(source["id"])
    return result


def import_legacy_recording_summaries(
    *, user_id: str | None = None, limit: int = 1000, dry_run: bool = True,
) -> dict:
    """Queue structured recording summaries as the durable personal-memory input."""
    conn = get_conn()
    query = "SELECT * FROM recordings WHERE TRIM(COALESCE(summary_json,'')) NOT IN ('','{}','[]')"
    args: list = []
    if user_id:
        query += " AND user_id=?"
        args.append(user_id)
    query += " ORDER BY created_at LIMIT ?"
    args.append(max(1, min(limit, 10000)))

    result = {"scanned": 0, "queued": 0, "duplicates": 0, "skipped": 0, "source_ids": []}
    for row in conn.execute(query, args).fetchall():
        result["scanned"] += 1
        try:
            summary = json.loads((row["summary_json"] or "").strip())
        except json.JSONDecodeError:
            summary = None
        if not isinstance(summary, dict):
            result["skipped"] += 1
            continue
        if dry_run:
            continue
        source, _job, duplicate = store.create_source(
            user_id=row["user_id"],
            source_type="recording_summary",
            content=json.dumps(summary, ensure_ascii=False, separators=(",", ":")),
            origin="legacy_recording_summaries",
            external_id=f"recording_summary:{row['id']}",
            title=row["title"] or row["filename"] or "Recording summary",
            occurred_at=row["created_at"],
            derivation_type="summary",
            metadata={
                "legacy_recording_id": row["id"],
                "legacy_category": row["category"] or "",
                "legacy_status": row["status"] or "",
                "duration_sec": row["duration_sec"] or 0,
                "structured_summary": True,
            },
        )
        if duplicate:
            result["duplicates"] += 1
        else:
            result["queued"] += 1
            result["source_ids"].append(source["id"])
    return result


def remove_legacy_recording_transcripts(*, user_id: str | None = None) -> dict:
    """Remove transcript-derived cognition rows while preserving legacy recordings."""
    conn = get_conn()
    query, args = "SELECT id FROM sources WHERE origin='legacy_recordings'", []
    if user_id:
        query += " AND user_id=?"
        args.append(user_id)
    source_ids = [row["id"] for row in conn.execute(query, args).fetchall()]
    result = {"sources": 0, "memberships": 0, "claims": 0, "jobs": 0}
    if not source_ids:
        return result

    placeholders = ",".join("?" for _ in source_ids)
    claim_ids = [row["claim_id"] for row in conn.execute(
        f"SELECT DISTINCT claim_id FROM claim_evidence WHERE source_id IN ({placeholders})", source_ids
    ).fetchall()]
    result["memberships"] = conn.execute(
        f"DELETE FROM project_memberships WHERE object_type='source' AND object_id IN ({placeholders})", source_ids
    ).rowcount
    conn.execute(f"DELETE FROM claim_evidence WHERE source_id IN ({placeholders})", source_ids)
    result["jobs"] = conn.execute(
        f"SELECT COUNT(*) FROM cognition_jobs WHERE source_id IN ({placeholders})", source_ids
    ).fetchone()[0]
    conn.execute(f"DELETE FROM cognition_jobs WHERE source_id IN ({placeholders})", source_ids)

    orphan_claim_ids = []
    if claim_ids:
        claim_placeholders = ",".join("?" for _ in claim_ids)
        orphan_claim_ids = [row["id"] for row in conn.execute(
            f"""SELECT c.id FROM claims_v2 c WHERE c.id IN ({claim_placeholders})
                AND NOT EXISTS (SELECT 1 FROM claim_evidence e WHERE e.claim_id=c.id)""",
            claim_ids,
        ).fetchall()]
    if orphan_claim_ids:
        orphan_placeholders = ",".join("?" for _ in orphan_claim_ids)
        conn.execute(
            f"DELETE FROM claim_relations WHERE source_claim_id IN ({orphan_placeholders}) OR target_claim_id IN ({orphan_placeholders})",
            [*orphan_claim_ids, *orphan_claim_ids],
        )
        conn.execute(f"DELETE FROM claim_entities WHERE claim_id IN ({orphan_placeholders})", orphan_claim_ids)
        conn.execute(
            f"DELETE FROM semantic_vectors WHERE object_type='claim' AND object_id IN ({orphan_placeholders})",
            orphan_claim_ids,
        )
        result["claims"] = conn.execute(
            f"DELETE FROM claims_v2 WHERE id IN ({orphan_placeholders})", orphan_claim_ids
        ).rowcount
    result["sources"] = conn.execute(f"DELETE FROM sources WHERE id IN ({placeholders})", source_ids).rowcount
    conn.commit()
    return result
