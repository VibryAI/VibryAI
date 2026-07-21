"""Refresh recording minutes, cognition sources, and project insights."""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import db  # noqa: E402
from db.connection import get_conn  # noqa: E402
from cognition import store  # noqa: E402
from services.recording_pipeline import refresh_completed_recording_content  # noqa: E402


def eligible_recordings(user_id: str | None, limit: int) -> list[dict]:
    query = (
        "SELECT id,user_id,title,updated_at FROM recordings "
        "WHERE core_status='completed' AND TRIM(COALESCE(transcript,''))!=''"
    )
    args: list[object] = []
    if user_id:
        query += " AND user_id=?"
        args.append(user_id)
    query += " ORDER BY updated_at DESC LIMIT ?"
    args.append(max(1, min(limit, 1000)))
    return [dict(row) for row in get_conn().execute(query, args).fetchall()]


def wait_for_recordings(recording_ids: list[str], timeout: int) -> dict:
    if not recording_ids:
        return {"completed": 0, "failed": []}
    deadline = time.monotonic() + timeout
    placeholders = ",".join("?" for _ in recording_ids)
    while time.monotonic() < deadline:
        rows = get_conn().execute(
            f"SELECT id,core_status,memory_insight_status,processing_error FROM recordings WHERE id IN ({placeholders})",
            recording_ids,
        ).fetchall()
        failed = [
            dict(row) for row in rows
            if row["core_status"] == "failed" or row["memory_insight_status"] == "failed"
        ]
        pending = [
            row for row in rows
            if row["core_status"] != "completed" or row["memory_insight_status"] != "ingested"
        ]
        if not pending:
            return {"completed": len(rows), "failed": failed}
        time.sleep(3)
    raise TimeoutError("recording refresh did not finish before timeout")


def queue_project_insights(user_id: str | None, refresh_id: str) -> list[dict]:
    jobs = []
    for project in store.dirty_projects():
        if user_id and project["user_id"] != user_id:
            continue
        jobs.append(store.enqueue_job(
            user_id=project["user_id"],
            job_type="project_insight",
            payload={"project_id": project["id"], "trigger": "content_refresh"},
            dedupe_key=f"project:{project['id']}:insight:refresh:{refresh_id}",
        ))
    return jobs


def wait_for_jobs(job_ids: list[str], timeout: int) -> dict:
    if not job_ids:
        return {"completed": 0, "failed": []}
    deadline = time.monotonic() + timeout
    placeholders = ",".join("?" for _ in job_ids)
    while time.monotonic() < deadline:
        rows = get_conn().execute(
            f"SELECT id,status,error_text FROM cognition_jobs WHERE id IN ({placeholders})",
            job_ids,
        ).fetchall()
        pending = [row for row in rows if row["status"] in {"queued", "running"}]
        if not pending:
            failed = [dict(row) for row in rows if row["status"] == "failed"]
            return {"completed": len(rows) - len(failed), "failed": failed}
        time.sleep(3)
    raise TimeoutError("project insight refresh did not finish before timeout")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Re-run minutes, refresh cognition sources, then optionally rebuild project insights.",
    )
    parser.add_argument("--user-id", help="Only refresh one user, for example admin or VibryCard")
    parser.add_argument("--limit", type=int, default=200, help="Maximum recordings, default 200")
    parser.add_argument("--refresh-id", help="Idempotent batch id; generated from current time when omitted")
    parser.add_argument("--apply", action="store_true", help="Queue jobs; without this flag only preview")
    parser.add_argument("--wait", action="store_true", help="Wait for refreshed minutes to enter cognition")
    parser.add_argument("--project-insights", action="store_true", help="Queue dirty project insights after minutes finish")
    parser.add_argument("--timeout", type=int, default=1800, help="Wait timeout in seconds, default 1800")
    args = parser.parse_args()

    db.init_db()
    rows = eligible_recordings(args.user_id, args.limit)
    if not args.apply:
        print(json.dumps({"mode": "preview", "count": len(rows), "recordings": rows}, ensure_ascii=False, indent=2))
        return 0

    result = refresh_completed_recording_content(
        user_id=args.user_id,
        limit=args.limit,
        refresh_id=args.refresh_id,
    )
    output = {"mode": "apply", **result}
    refresh_id = str(result["refresh_id"])
    if args.wait or args.project_insights:
        output["recordings"] = wait_for_recordings(
            [row["id"] for row in rows], args.timeout,
        )
    if args.project_insights:
        jobs = queue_project_insights(args.user_id, refresh_id)
        output["project_insights_queued"] = len(jobs)
        output["project_insights"] = wait_for_jobs(
            [job["id"] for job in jobs], args.timeout,
        )
    print(json.dumps(output, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
