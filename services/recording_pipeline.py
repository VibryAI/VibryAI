"""Durable asynchronous processing for VibryCard recordings."""

from __future__ import annotations

import json
import hashlib
import logging
import os
import re
from datetime import datetime
from pathlib import Path

import db
from app.config import config
from cognition import store
from db.connection import get_conn
from utils.audio import detect_audio_format
from services.markdown_content import (
    memory_insight_to_markdown,
    parse_memory_insight_markdown,
    parse_recording_insight_markdown,
    recording_insight_to_markdown,
    sanitize_summary_markdown,
    summary_to_markdown,
)

log = logging.getLogger("vibry.recording_pipeline")

_ROOT = Path(__file__).resolve().parents[1]
_UPLOAD_DIR = _ROOT / "data" / "recording_uploads"


def _payload(job: dict) -> dict:
    raw = job.get("payload_json") or "{}"
    if isinstance(raw, dict):
        return raw
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else {}
    except (TypeError, json.JSONDecodeError):
        return {}


def _emit(user_id: str, recording_id: str, event_type: str, **payload) -> None:
    store.add_event(
        user_id=user_id,
        event_type=event_type,
        object_type="recording",
        object_id=recording_id,
        payload=payload,
    )


def _sha256_bytes(audio_bytes: bytes) -> str:
    return hashlib.sha256(audio_bytes).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _reusable_by_hash(user_id: str, audio_sha256: str) -> tuple[dict, dict | None] | None:
    recording = db.find_recording_by_audio_hash(user_id, audio_sha256)
    if not recording:
        return None
    jobs = store.list_recording_jobs(recording["id"], user_id)
    active_job = next((job for job in jobs if job.get("status") in {"queued", "running"}), None)
    transcription_job = next(
        (job for job in jobs if job.get("job_type") in {"transcribe_recording", "poll_standard_asr"}),
        None,
    )
    return recording, active_job or transcription_job or (jobs[-1] if jobs else None)


def _recording_id_for_upload(title: str, user_id: str, audio_sha256: str) -> str:
    recording_id = db.generate_id(title)
    existing = db.get_recording(recording_id)
    if not existing or (
        existing.get("user_id") == user_id
        and existing.get("audio_sha256") == audio_sha256
    ):
        return recording_id
    return f"{recording_id}_{audio_sha256[:10]}"


def backfill_recording_audio_hashes(limit: int = 250) -> int:
    """Populate hashes for pre-migration uploads without touching cloud storage."""
    rows = get_conn().execute(
        """SELECT id, upload_path FROM recordings
           WHERE COALESCE(audio_sha256, '')='' AND COALESCE(upload_path, '')<>''
           LIMIT ?""",
        (max(1, limit),),
    ).fetchall()
    updates: list[tuple[str, str]] = []
    for row in rows:
        path = Path(row["upload_path"])
        if not path.is_file():
            continue
        try:
            updates.append((_sha256_file(path), row["id"]))
        except OSError:
            log.warning("unable to hash historical upload: %s", path)
    if updates:
        conn = get_conn()
        conn.executemany(
            "UPDATE recordings SET audio_sha256=? WHERE id=? AND COALESCE(audio_sha256, '')=''",
            updates,
        )
        conn.commit()
    return len(updates)


def submit_recording(
    *, audio_bytes: bytes, title: str, user_id: str, category: str = "",
) -> tuple[dict, dict | None, bool]:
    """Persist an upload and enqueue transcription without holding the request open."""
    title = (title or "recording.ogg").strip()
    if not audio_bytes:
        raise ValueError("audio is required")
    if len(audio_bytes) > 200 * 1024 * 1024:
        raise ValueError("audio exceeds 200 MB")

    audio_sha256 = _sha256_bytes(audio_bytes)
    reusable = _reusable_by_hash(user_id, audio_sha256)
    if reusable:
        return reusable[0], reusable[1], True
    recording_id = _recording_id_for_upload(title, user_id, audio_sha256)
    existing = db.get_recording(recording_id)
    if existing and existing.get("user_id") != user_id:
        raise ValueError("recording id belongs to another user")
    if (
        existing
        and existing.get("core_status", existing.get("status")) == "completed"
        and existing.get("transcript")
        and (existing.get("summary_markdown") or existing.get("summary_json"))
    ):
        jobs = store.list_recording_jobs(recording_id, user_id)
        return existing, jobs[0] if jobs else None, True

    _UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    suffix = Path(title).suffix.lower()
    if suffix not in {".ogg", ".opus", ".wav", ".mp3", ".m4a", ".aac", ".flac"}:
        suffix = ".ogg"
    upload_path = _UPLOAD_DIR / f"{recording_id}{suffix}"
    part_path = upload_path.with_suffix(upload_path.suffix + ".part")
    part_path.write_bytes(audio_bytes)
    os.replace(part_path, upload_path)

    db.upsert_recording(
        recording_id,
        user_id=user_id,
        title=title,
        filename=title,
        file_size=len(audio_bytes),
        category=category or "未分类",
        status="queued",
        core_status="queued",
        recording_insight_status="disabled",
        memory_insight_status="pending",
        processing_error="",
        client_recording_id=title,
        upload_path=str(upload_path),
        audio_sha256=audio_sha256,
    )
    job = store.enqueue_job(
        user_id=user_id,
        recording_id=recording_id,
        job_type="transcribe_recording",
        payload={"title": title, "category": category},
        dedupe_key=f"recording:{recording_id}:transcribe:v1",
    )
    if job.get("status") == "failed":
        job = store.retry_job(job["id"], user_id) or job
    _emit(user_id, recording_id, "recording_queued", core_status="queued")
    return db.get_recording(recording_id) or {}, job, False


def submit_recording_file(
    *, staged_path: Path, file_size: int, title: str, user_id: str,
    category: str = "",
) -> tuple[dict, dict | None, bool]:
    """Persist an already streamed upload and enqueue transcription."""
    title = (title or "recording.ogg").strip()
    if file_size <= 0 or not staged_path.is_file():
        raise ValueError("audio is required")
    if file_size > 200 * 1024 * 1024:
        raise ValueError("audio exceeds 200 MB")

    audio_sha256 = _sha256_file(staged_path)
    reusable = _reusable_by_hash(user_id, audio_sha256)
    if reusable:
        return reusable[0], reusable[1], True
    recording_id = _recording_id_for_upload(title, user_id, audio_sha256)
    existing = db.get_recording(recording_id)
    if existing and existing.get("user_id") != user_id:
        raise ValueError("recording id belongs to another user")
    if (
        existing
        and existing.get("core_status", existing.get("status")) == "completed"
        and existing.get("transcript")
        and (existing.get("summary_markdown") or existing.get("summary_json"))
    ):
        jobs = store.list_recording_jobs(recording_id, user_id)
        return existing, jobs[0] if jobs else None, True

    _UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    suffix = Path(title).suffix.lower()
    if suffix not in {".ogg", ".opus", ".wav", ".mp3", ".m4a", ".aac", ".flac"}:
        suffix = ".ogg"
    upload_path = _UPLOAD_DIR / f"{recording_id}{suffix}"
    os.replace(staged_path, upload_path)

    db.upsert_recording(
        recording_id,
        user_id=user_id,
        title=title,
        filename=title,
        file_size=file_size,
        category=category or "未分类",
        status="queued",
        core_status="queued",
        recording_insight_status="disabled",
        memory_insight_status="pending",
        processing_error="",
        client_recording_id=title,
        upload_path=str(upload_path),
        audio_sha256=audio_sha256,
    )
    job = store.enqueue_job(
        user_id=user_id,
        recording_id=recording_id,
        job_type="transcribe_recording",
        payload={"title": title, "category": category},
        dedupe_key=f"recording:{recording_id}:transcribe:v1",
    )
    if job.get("status") == "failed":
        job = store.retry_job(job["id"], user_id) or job
    _emit(user_id, recording_id, "recording_queued", core_status="queued")
    return db.get_recording(recording_id) or {}, job, False


def _recording(job: dict) -> dict:
    recording_id = job.get("recording_id") or _payload(job).get("recording_id")
    recording = db.get_recording(recording_id) if recording_id else None
    if not recording:
        raise ValueError(f"recording not found: {recording_id}")
    if recording.get("user_id") != job.get("user_id"):
        raise ValueError("recording user mismatch")
    return recording


def _enqueue_stage(
    *, recording: dict, job_type: str, source_id: str | None = None,
    force: bool = False,
) -> dict:
    versions = {"summarize_recording": 2, "recording_insight": 3}
    version = versions.get(job_type, 1)
    job = store.enqueue_job(
        user_id=recording["user_id"],
        recording_id=recording["id"],
        source_id=source_id,
        job_type=job_type,
        payload={"recording_id": recording["id"]},
        dedupe_key=f"recording:{recording['id']}:{job_type}:v{version}",
    )
    if force and job.get("status") in {"completed", "failed"}:
        job = store.retry_job(job["id"], recording["user_id"]) or job
    elif job.get("status") == "failed":
        job = store.retry_job(job["id"], recording["user_id"]) or job
    return job


def process_recording_job(job: dict) -> bool:
    handlers = {
        "transcribe_recording": _transcribe_recording,
        "poll_standard_asr": _poll_standard_asr,
        "summarize_recording": _summarize_recording,
        "recording_insight": _recording_insight,
        "memory_ingest": _memory_ingest,
        "memory_insight": _memory_insight,
    }
    handler = handlers.get(job.get("job_type"))
    if not handler:
        raise ValueError(f"unsupported recording job: {job.get('job_type')}")
    return handler(job) is not False


def _transcribe_recording(job: dict) -> bool:
    recording = _recording(job)
    upload_path = Path(recording.get("upload_path") or "")
    if not upload_path.is_file():
        raise FileNotFoundError(f"uploaded audio not found: {upload_path}")

    db.upsert_recording(
        recording["id"], status="transcribing", core_status="transcribing",
        processing_error="",
    )
    store.update_job_progress(job["id"], {"stage": "transcribing"})
    _emit(recording["user_id"], recording["id"], "recording_transcribing", core_status="transcribing")

    audio_bytes = upload_path.read_bytes()
    from services.asr_providers import DoubaoStandardProvider, get_asr_provider

    provider = get_asr_provider(config.asr.mode)
    if isinstance(provider, DoubaoStandardProvider):
        payload = _payload(job)
        tasks = payload.get("asr_tasks")
        if not isinstance(tasks, list) or not tasks:
            tasks = provider.submit_tasks(
                audio_bytes, audio_fmt=detect_audio_format(audio_bytes),
            )
            payload["asr_tasks"] = tasks
            if not store.update_job_payload(job["id"], payload, job.get("lease_owner")):
                raise RuntimeError("unable to persist submitted ASR task")
        poll_job = store.enqueue_job(
            user_id=recording["user_id"],
            recording_id=recording["id"],
            job_type="poll_standard_asr",
            payload={"recording_id": recording["id"], "asr_tasks": tasks, "completed_results": {}},
            dedupe_key=f"recording:{recording['id']}:poll_standard_asr:v1",
        )
        if poll_job.get("status") == "failed":
            store.retry_job(poll_job["id"], recording["user_id"])
        stage = {"stage": "waiting_asr", "submitted_chunks": len(tasks), "completed_chunks": 0}
        store.update_job_progress(job["id"], stage)
        _emit(
            recording["user_id"], recording["id"], "recording_asr_submitted",
            core_status="transcribing", chunks=len(tasks),
        )
        return True

    from services.asr import transcribe

    result = transcribe(
        audio_bytes, recording.get("title", ""), recording["user_id"],
        recording.get("category", ""),
    )
    if result.get("error") or not (result.get("text") or "").strip():
        raise RuntimeError(result.get("error") or "empty transcription")
    _after_transcription(recording)
    return True


def _poll_standard_asr(job: dict) -> bool:
    """Poll submitted Doubao tasks once, then release the worker until the next check."""
    recording = _recording(job)
    payload = _payload(job)
    tasks = payload.get("asr_tasks")
    if not isinstance(tasks, list) or not tasks:
        raise ValueError("submitted ASR task state is missing")

    from services.asr_providers import DoubaoStandardProvider, get_asr_provider

    provider = get_asr_provider(config.asr.mode)
    if not isinstance(provider, DoubaoStandardProvider):
        raise RuntimeError("standard ASR polling requires the standard ASR provider")
    completed = payload.get("completed_results")
    completed = completed if isinstance(completed, dict) else {}
    for task in tasks:
        task_id = str(task.get("task_id") or "")
        if not task_id or task_id in completed:
            continue
        result = provider.poll_task(task)
        if result is not None:
            completed[task_id] = result

    payload["completed_results"] = completed
    if len(completed) < len(tasks):
        try:
            interval = max(2, int(os.getenv("DOUBAO_ASR_POLL_INTERVAL", "5")))
        except ValueError:
            interval = 5
        progress = {
            "stage": "waiting_asr",
            "submitted_chunks": len(tasks),
            "completed_chunks": len(completed),
        }
        if not store.reschedule_job(
            job["id"], payload=payload, progress=progress, delay_seconds=interval,
            lease_owner=job.get("lease_owner"),
        ):
            raise RuntimeError("unable to reschedule pending ASR poll")
        _emit(
            recording["user_id"], recording["id"], "recording_asr_waiting",
            core_status="transcribing", completed_chunks=len(completed), total_chunks=len(tasks),
        )
        return False

    ordered_results = [completed[str(task["task_id"])] for task in tasks]
    asr_result = provider.merge_task_results(tasks, ordered_results)
    from services.asr import transcribe
    upload_path = Path(recording.get("upload_path") or "")
    if not upload_path.is_file():
        raise FileNotFoundError(f"uploaded audio not found: {upload_path}")

    persisted = transcribe(
        upload_path.read_bytes(),
        recording.get("title", ""), recording["user_id"], recording.get("category", ""),
        provider_result=asr_result,
    )
    if persisted.get("error") or not (persisted.get("text") or "").strip():
        raise RuntimeError(persisted.get("error") or "empty transcription")
    _after_transcription(recording)
    return True


def _after_transcription(recording: dict) -> None:

    db.upsert_recording(
        recording["id"], status="summarizing", core_status="summarizing",
        processing_error="",
    )
    _enqueue_stage(recording=db.get_recording(recording["id"]) or recording, job_type="summarize_recording")
    _emit(recording["user_id"], recording["id"], "recording_summarizing", core_status="summarizing")


def _summarize_recording(job: dict) -> None:
    recording = _recording(job)
    transcript = (recording.get("transcript") or "").strip()
    if not transcript:
        raise ValueError("transcript is empty")

    db.upsert_recording(recording["id"], status="summarizing", core_status="summarizing")
    store.update_job_progress(job["id"], {"stage": "summarizing"})

    from services.asr import summarize

    result = summarize(
        transcript,
        recording.get("title", ""),
        "",
        recording["user_id"],
        persist_recording=False,
    )
    if result.get("error"):
        raise RuntimeError(result["error"])

    db.upsert_recording(
        recording["id"],
        status="completed",
        core_status="completed",
        recording_insight_status="disabled",
        memory_insight_status="queued",
        processing_error="",
        processing_version=int(recording.get("processing_version") or 0) + 1,
    )
    completed = db.get_recording(recording["id"]) or recording
    is_refresh = ":summarize:refresh:" in (job.get("dedupe_key") or "")
    _enqueue_stage(
        recording=completed,
        job_type="memory_ingest",
        force=is_refresh,
    )
    _emit(
        recording["user_id"], recording["id"], "recording_core_completed",
        core_status="completed", recording_insight_status="disabled", memory_insight_status="queued",
    )


def generate_recording_insight(
    *, transcript: str, title: str, context: str, user_id: str,
) -> dict:
    insight_prompt = config.prompt.system_prompt
    insight_prompt = (
        insight_prompt.replace("{name}", config.summary.user_name)
        .replace("{role}", config.summary.user_role)
        .replace("{context}", config.summary.user_context)
    )
    output_contract = """\
无论前文是否提到 JSON，最终都只输出纯 Markdown，不要代码块，不要 JSON。
优先使用以下标题；某一部分没有可靠内容时省略整个章节：

# 录音洞察
## 核心洞察
## 机会分析
## 风险提示
## 行动建议

行动建议使用无序列表，最多 3 条。不得为了填满结构编造内容。"""
    messages = [
        {"role": "system", "content": f"{insight_prompt}\n\n{output_contract}"},
        {
            "role": "user",
            "content": f"Recording: {title}\n\nTranscript:\n{transcript}\n\nContext: {context}",
        },
    ]
    from services.asr import call_llm

    model = config.summary.effective_model
    result = call_llm(model, messages, 180)
    if result.get("error"):
        raise RuntimeError(str(result["error"]))
    usage = result.get("usage", {})
    db.log_usage(
        user_id=user_id, endpoint="/api/insight", model=model,
        prompt_tokens=usage.get("prompt_tokens", 0),
        completion_tokens=usage.get("completion_tokens", 0),
        total_tokens=usage.get("total_tokens", 0),
    )
    raw = result.get("choices", [{}])[0].get("message", {}).get("content", "")
    if not raw.strip():
        raise RuntimeError("recording insight returned empty content")
    return parse_recording_insight_markdown(raw)


def _normalize_recording_insight(parsed: object) -> dict:
    if not isinstance(parsed, dict):
        raise RuntimeError("recording insight returned invalid object")

    core = parsed.get("core_insight")
    if not isinstance(core, str) or not core.strip():
        core = parsed.get("current_intent") or parsed.get("detailed_summary") or ""

    raw_analysis = parsed.get("analysis")
    analysis = raw_analysis if isinstance(raw_analysis, dict) else {}
    opportunity = analysis.get("opportunity") or parsed.get("opportunity") or ""
    risk = analysis.get("risk") or parsed.get("risk") or ""

    actions = parsed.get("action_suggestions")
    if not isinstance(actions, list):
        actions = parsed.get("action_items")
    if not isinstance(actions, list):
        actions = [actions] if isinstance(actions, str) and actions.strip() else []

    normalized = {
        "core_insight": str(core).strip(),
        "analysis": {
            "opportunity": str(opportunity).strip(),
            "risk": str(risk).strip(),
        },
        "action_suggestions": [str(item).strip() for item in actions if str(item).strip()],
    }
    if not (
        normalized["core_insight"]
        or normalized["analysis"]["opportunity"]
        or normalized["analysis"]["risk"]
        or normalized["action_suggestions"]
    ):
        raise RuntimeError("recording insight returned empty content")
    return normalized


def _has_recording_insight(recording: dict) -> bool:
    if (recording.get("recording_insight_markdown") or "").strip():
        return True
    insight = recording.get("recording_insight_json") or recording.get("insight_json")
    return (
        isinstance(insight, dict)
        and isinstance(insight.get("core_insight"), str)
        and isinstance(insight.get("analysis"), dict)
        and isinstance(insight.get("action_suggestions"), list)
    )


def _has_memory_insight(recording: dict) -> bool:
    insight = recording.get("memory_insight_json")
    return (
        isinstance(insight, dict)
        and isinstance(insight.get("summary"), str)
        and isinstance(insight.get("connections"), list)
    )


def _memory_source_is_current(recording: dict) -> bool:
    summary = recording.get("summary_json") or {}
    expected = (
        recording.get("summary_markdown") or summary_to_markdown(summary)
    ).strip()
    if not expected:
        return False
    source = store.get_source_by_external_id(
        user_id=recording["user_id"],
        external_id=f"recording_summary:{recording['id']}",
    )
    return bool(
        source
        and source.get("status") == "processed"
        and (source.get("content_text") or "").strip() == expected
    )


def backfill_completed_recording_jobs(limit: int = 200) -> dict[str, int]:
    """Queue missing memory ingestion for recordings created before the async pipeline."""
    rows = get_conn().execute(
        """SELECT id FROM recordings
           WHERE status='completed' AND core_status='completed'
             AND TRIM(COALESCE(transcript,''))!=''
             AND (
               TRIM(COALESCE(summary_markdown,''))!=''
               OR TRIM(COALESCE(summary_json,'')) NOT IN ('','{}','[]')
             )
           ORDER BY updated_at DESC LIMIT ?""",
        (max(1, min(limit, 1000)),),
    ).fetchall()
    counts = {"memory_ingest": 0}
    for row in rows:
        recording = db.get_recording(row["id"])
        if not recording:
            continue
        updates = {}
        updates["recording_insight_status"] = "disabled"
        source = store.get_source_by_external_id(
            user_id=recording["user_id"],
            external_id=f"recording_summary:{recording['id']}",
        )
        if not _memory_source_is_current(recording):
            updates["memory_insight_status"] = "queued"
            _enqueue_stage(recording=recording, job_type="memory_ingest", force=True)
            counts["memory_ingest"] += 1
        elif source and source.get("status") == "processed":
            updates["memory_insight_status"] = "ingested"
        if updates:
            db.upsert_recording(recording["id"], **updates)
    return counts


def backfill_markdown_fields(limit: int = 1000) -> dict[str, int]:
    """Render existing structured rows into user-facing Markdown columns."""
    rows = get_conn().execute(
        "SELECT id FROM recordings ORDER BY updated_at DESC LIMIT ?",
        (max(1, min(limit, 10000)),),
    ).fetchall()
    counts = {"summary": 0, "recording_insight": 0, "memory_insight": 0}
    for row in rows:
        recording = db.get_recording(row["id"])
        if not recording:
            continue
        updates: dict[str, str] = {}
        existing_summary_markdown = (recording.get("summary_markdown") or "").strip()
        if existing_summary_markdown:
            markdown = sanitize_summary_markdown(existing_summary_markdown)
            if markdown != existing_summary_markdown:
                updates["summary_markdown"] = markdown
                counts["summary"] += 1
        else:
            markdown = sanitize_summary_markdown(
                summary_to_markdown(recording.get("summary_json") or {})
            )
            if markdown:
                updates["summary_markdown"] = markdown
                counts["summary"] += 1
        if not (recording.get("recording_insight_markdown") or "").strip():
            insight = recording.get("recording_insight_json") or recording.get("insight_json") or {}
            markdown = recording_insight_to_markdown(insight)
            if markdown:
                updates["recording_insight_markdown"] = markdown
                counts["recording_insight"] += 1
        if not (recording.get("memory_insight_markdown") or "").strip():
            markdown = memory_insight_to_markdown(recording.get("memory_insight_json") or {})
            if markdown:
                updates["memory_insight_markdown"] = markdown
                counts["memory_insight"] += 1
        if updates:
            db.upsert_recording(recording["id"], **updates)
    return counts


def refresh_completed_recording_content(
    *, user_id: str | None = None, limit: int = 200, refresh_id: str | None = None,
) -> dict[str, int | str]:
    """Re-run user-facing minutes after prompt changes."""
    query = (
        "SELECT id FROM recordings WHERE core_status='completed' "
        "AND TRIM(COALESCE(transcript,''))!=''"
    )
    args: list[object] = []
    if user_id:
        query += " AND user_id=?"
        args.append(user_id)
    query += " ORDER BY updated_at DESC LIMIT ?"
    args.append(max(1, min(limit, 1000)))
    generation = refresh_id or datetime.now().strftime("%Y%m%d%H%M%S")
    queued = 0
    skipped = 0
    memory_requeued = 0
    for row in get_conn().execute(query, args).fetchall():
        recording = db.get_recording(row["id"])
        if not recording:
            continue
        dedupe_key = f"recording:{recording['id']}:summarize:refresh:{generation}"
        existing = get_conn().execute(
            "SELECT id,status FROM cognition_jobs WHERE dedupe_key=?", (dedupe_key,),
        ).fetchone()
        if existing and existing["status"] == "completed":
            if not _memory_source_is_current(recording):
                _enqueue_stage(
                    recording=recording,
                    job_type="memory_ingest",
                    force=True,
                )
                db.upsert_recording(
                    recording["id"], memory_insight_status="queued",
                )
                memory_requeued += 1
            skipped += 1
            continue
        if existing:
            job = store.get_job(existing["id"], recording["user_id"]) or {}
            if job.get("status") == "failed":
                store.retry_job(job["id"], recording["user_id"])
        else:
            store.enqueue_job(
                user_id=recording["user_id"],
                recording_id=recording["id"],
                job_type="summarize_recording",
                payload={"recording_id": recording["id"]},
                dedupe_key=dedupe_key,
            )
        db.upsert_recording(
            recording["id"],
            status="summarizing",
            core_status="summarizing",
            recording_insight_status="disabled",
            summary_markdown="",
            processing_error="",
        )
        queued += 1
    return {
        "queued": queued,
        "skipped": skipped,
        "memory_requeued": memory_requeued,
        "refresh_id": generation,
    }


def _recording_insight(job: dict) -> None:
    recording = _recording(job)
    db.upsert_recording(recording["id"], recording_insight_status="disabled")
    store.update_job_progress(job["id"], {"stage": "recording_insight_disabled"})


def _memory_ingest(job: dict) -> None:
    recording = _recording(job)
    summary = recording.get("summary_json") or {}
    summary_markdown = (
        recording.get("summary_markdown") or summary_to_markdown(summary)
    ).strip()
    if not summary_markdown:
        raise ValueError("summary is empty")
    db.upsert_recording(recording["id"], memory_insight_status="ingesting")
    store.update_job_progress(job["id"], {"stage": "memory_ingest"})
    recording_source = store.get_source_by_external_id(
        user_id=recording["user_id"],
        external_id=f"recording:{recording['id']}",
    )
    project_ids = store.source_project_ids(
        source_id=recording_source["id"], user_id=recording["user_id"],
    ) if recording_source else []
    source, source_job, duplicate = store.create_source(
        user_id=recording["user_id"],
        source_type="recording",
        content=summary_markdown,
        origin="vibry_card",
        title=recording.get("title", ""),
        external_id=f"recording_summary:{recording['id']}",
        derivation_type="summary",
        mime_type="text/markdown",
        metadata={"recording_id": recording["id"], "kind": "minutes"},
        project_ids=project_ids,
    )
    if duplicate:
        source, refreshed_job, refreshed = store.refresh_source_content(
            source_id=source["id"],
            user_id=recording["user_id"],
            content=summary_markdown,
            title=recording.get("title", ""),
            metadata={"recording_id": recording["id"], "kind": "minutes"},
            project_ids=project_ids,
        )
        if refreshed:
            source_job = refreshed_job
        elif source.get("status") == "processed":
            db.upsert_recording(recording["id"], memory_insight_status="ingested")
        elif source_job.get("status") == "failed":
            source_job = store.retry_job(source_job["id"], recording["user_id"]) or source_job
    _emit(
        recording["user_id"], recording["id"], "recording_memory_ingesting",
        memory_insight_status="ingesting", source_id=source.get("id"),
        cognition_job_id=source_job.get("id"),
    )


def on_source_processed(source_id: str) -> None:
    source = store.get_source(source_id)
    if not source:
        return
    from services.task_suggestions import publish_action_item_suggestions
    try:
        publish_action_item_suggestions(source)
    except Exception:
        log.exception("task suggestion publishing failed for source %s", source_id)
    metadata = source.get("metadata") or {}
    recording_id = metadata.get("recording_id")
    if not recording_id:
        return
    recording = db.get_recording(recording_id)
    if not recording or recording.get("user_id") != source.get("user_id"):
        return
    db.upsert_recording(recording_id, memory_insight_status="ingested")
    _emit(
        recording["user_id"], recording_id, "recording_memory_ingested",
        memory_insight_status="ingested", source_id=source_id,
    )
    from services.recording_grouping import discover_recording_groups
    try:
        discover_recording_groups(recording["user_id"])
    except Exception:
        log.exception("recording group discovery failed for user %s", recording["user_id"])


def _memory_insight(job: dict) -> None:
    recording = _recording(job)
    db.upsert_recording(recording["id"], memory_insight_status="ingested")
    store.update_job_progress(job["id"], {"stage": "memory_insight_disabled"})


def on_recording_job_failed(job: dict, error: str, final: bool) -> None:
    if not final or not job.get("recording_id"):
        return
    fields: dict = {"processing_error": error[:500]}
    if job.get("job_type") in {"transcribe_recording", "poll_standard_asr", "summarize_recording"}:
        fields.update(status="failed", core_status="failed")
    elif job.get("job_type") == "recording_insight":
        fields["recording_insight_status"] = "failed"
    elif job.get("job_type") in {"memory_ingest", "memory_insight"}:
        fields["memory_insight_status"] = "failed"
    else:
        return
    db.upsert_recording(job["recording_id"], **fields)
    _emit(
        job["user_id"], job["recording_id"], "recording_processing_failed",
        job_type=job.get("job_type"), error=error[:500],
    )
