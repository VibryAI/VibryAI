"""Durable multi-recording minutes generation."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone

import db
from cognition import store


_PROCESSING_VERSION = "aggregate-minutes-v1"
_CHUNK_CHAR_LIMIT = 45_000


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _dedupe_key(user_id: str, recording_ids: list[str], title: str) -> str:
    payload = json.dumps(
        {"user_id": user_id, "recording_ids": recording_ids, "title": title.strip()},
        ensure_ascii=False,
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _validated_recordings(user_id: str, recording_ids: list[str]) -> list[dict]:
    unique_ids = list(dict.fromkeys(recording_ids))
    if len(unique_ids) < 2:
        raise ValueError("at least two recordings are required")
    if len(unique_ids) > 20:
        raise ValueError("at most twenty recordings can be aggregated")

    recordings = []
    for recording_id in unique_ids:
        recording = db.get_recording(recording_id)
        if not recording or recording.get("user_id") != user_id:
            raise ValueError(f"recording not found: {recording_id}")
        if recording.get("core_status", recording.get("status")) != "completed":
            raise ValueError(f"recording is not completed: {recording_id}")
        if not (recording.get("transcript") or "").strip():
            raise ValueError(f"recording transcript is empty: {recording_id}")
        recordings.append(recording)
    return recordings


def submit_aggregate(
    *, user_id: str, recording_ids: list[str], title: str = "多记录专题总结",
    project_ids: list[str] | None = None,
) -> tuple[dict, dict, bool]:
    recordings = _validated_recordings(user_id, recording_ids)
    normalized_ids = [item["id"] for item in recordings]
    normalized_projects = list(dict.fromkeys(project_ids or []))
    for project_id in normalized_projects:
        if not store.get_project(project_id, user_id):
            raise ValueError(f"project not found: {project_id}")

    dedupe_key = _dedupe_key(user_id, normalized_ids, title)
    aggregate, duplicate = store.create_recording_aggregate(
        user_id=user_id,
        title=title or "多记录专题总结",
        recording_ids=normalized_ids,
        project_ids=normalized_projects,
        dedupe_key=dedupe_key,
    )
    job = store.enqueue_job(
        user_id=user_id,
        job_type="aggregate_minutes",
        payload={"aggregate_id": aggregate["id"]},
        dedupe_key=f"aggregate:{aggregate['id']}:{_PROCESSING_VERSION}",
    )
    if job.get("status") == "failed":
        job = store.retry_job(job["id"], user_id) or job
        aggregate = store.update_recording_aggregate(
            aggregate["id"], status="queued", error_text="",
        ) or aggregate
    return aggregate, job, duplicate


def _blocks(recordings: list[dict]) -> list[str]:
    return [
        f"## {item.get('title') or item['id']}\n\n{item['transcript'].strip()}"
        for item in recordings
    ]


def _chunks(blocks: list[str]) -> list[str]:
    chunks: list[str] = []
    current: list[str] = []
    current_size = 0
    for block in blocks:
        if current and current_size + len(block) > _CHUNK_CHAR_LIMIT:
            chunks.append("\n\n".join(current))
            current, current_size = [], 0
        current.append(block)
        current_size += len(block)
    if current:
        chunks.append("\n\n".join(current))
    return chunks


def _summary_markdown(result: dict) -> str:
    return str(
        result.get("markdown")
        or result.get("detailed_summary")
        or result.get("full_summary")
        or ""
    ).strip()


def _generate_markdown(aggregate: dict, recordings: list[dict]) -> str:
    from services.asr import summarize

    chunks = _chunks(_blocks(recordings))
    context = (
        f"这是由 {len(recordings)} 条相关录音生成的专题总结。"
        "请保留时间顺序、观点变化、明确决定和行动项，不得编造。"
    )
    if len(chunks) == 1:
        result = summarize(
            chunks[0], aggregate["title"], context, aggregate["user_id"],
            persist_recording=False,
        )
        markdown = _summary_markdown(result)
        if not markdown:
            raise RuntimeError(result.get("error") or "aggregate summary is empty")
        return markdown

    partials = []
    for index, chunk in enumerate(chunks, start=1):
        result = summarize(
            chunk,
            f"{aggregate['title']}（第 {index}/{len(chunks)} 部分）",
            "先整理这一部分的事实、决定、行动项和主题，不得跨部分推测。",
            aggregate["user_id"],
            persist_recording=False,
        )
        markdown = _summary_markdown(result)
        if not markdown:
            raise RuntimeError(result.get("error") or "aggregate partial summary is empty")
        partials.append(markdown)

    result = summarize(
        "\n\n---\n\n".join(partials),
        aggregate["title"],
        context + "以下内容是分段纪要，请合并去重并保持跨段时间关系。",
        aggregate["user_id"],
        persist_recording=False,
    )
    markdown = _summary_markdown(result)
    if not markdown:
        raise RuntimeError(result.get("error") or "aggregate final summary is empty")
    return markdown


def _source_ids(user_id: str, recording_ids: list[str]) -> list[str]:
    result = []
    for recording_id in recording_ids:
        for external_id in (
            f"recording_summary:{recording_id}", f"recording:{recording_id}", recording_id,
        ):
            source = store.get_source_by_external_id(user_id=user_id, external_id=external_id)
            if source:
                result.append(source["id"])
                break
    return result


def _publish(aggregate: dict) -> dict:
    thread = store.ensure_thread(
        user_id=aggregate["user_id"], scope_type="global", scope_id="",
        title="Memory Matrix",
    )
    message = store.add_message(
        thread_id=thread["id"],
        user_id=aggregate["user_id"],
        role="assistant",
        content=(
            f"## {aggregate['title']}\n\n"
            f"专题总结已生成，共关联 {len(aggregate['recording_ids'])} 条录音。"
        ),
        content_format="markdown",
        message_type="topic_summary",
        source_id=aggregate.get("source_id"),
        metadata={
            "aggregate_id": aggregate["id"],
            "source_id": aggregate.get("source_id"),
            "recording_ids": aggregate["recording_ids"],
            "project_ids": aggregate["project_ids"],
            "action": "open_topic_summary",
        },
    )
    return store.update_recording_aggregate(
        aggregate["id"], message_id=message["id"], status="completed",
        completed_at=_now(), error_text="",
    ) or aggregate


def process_aggregate_job(job: dict) -> None:
    payload = json.loads(job.get("payload_json") or "{}")
    aggregate_id = payload.get("aggregate_id")
    aggregate = store.get_recording_aggregate(aggregate_id, job.get("user_id"))
    if not aggregate:
        raise ValueError(f"aggregate not found: {aggregate_id}")
    if aggregate.get("status") == "completed" and aggregate.get("message_id"):
        return

    if not aggregate.get("summary_markdown") or not aggregate.get("source_id"):
        recordings = _validated_recordings(
            aggregate["user_id"], aggregate["recording_ids"],
        )
        store.update_recording_aggregate(
            aggregate["id"], status="summarizing", error_text="",
        )
        store.update_job_progress(job["id"], {"stage": "aggregate_summarizing"})
        markdown = _generate_markdown(aggregate, recordings)
        parent_source_ids = _source_ids(
            aggregate["user_id"], aggregate["recording_ids"],
        )
        source, _, duplicate = store.create_source(
            user_id=aggregate["user_id"],
            source_type="topic_summary",
            content=markdown,
            origin="vibry_aggregate",
            title=aggregate["title"],
            mime_type="text/markdown",
            external_id=f"aggregate:{aggregate['id']}",
            derivation_type="summary",
            metadata={
                "kind": "topic_summary",
                "aggregate_id": aggregate["id"],
                "recording_ids": aggregate["recording_ids"],
                "parent_source_ids": parent_source_ids,
                "processing_version": _PROCESSING_VERSION,
            },
            project_ids=aggregate["project_ids"],
        )
        if duplicate and source.get("content_text") != markdown:
            source, _, _ = store.refresh_source_content(
                source_id=source["id"], user_id=aggregate["user_id"],
                content=markdown, title=aggregate["title"],
                metadata={
                    "kind": "topic_summary",
                    "aggregate_id": aggregate["id"],
                    "recording_ids": aggregate["recording_ids"],
                    "parent_source_ids": parent_source_ids,
                    "processing_version": _PROCESSING_VERSION,
                },
                project_ids=aggregate["project_ids"],
            )
        aggregate = store.update_recording_aggregate(
            aggregate["id"], status="publishing", summary_markdown=markdown,
            source_id=source["id"], processing_version=_PROCESSING_VERSION,
        ) or aggregate

    store.update_job_progress(job["id"], {"stage": "aggregate_publishing"})
    if not aggregate.get("message_id"):
        _publish(aggregate)


def on_aggregate_job_failed(job: dict, error: str, final: bool) -> None:
    if not final:
        return
    payload = json.loads(job.get("payload_json") or "{}")
    aggregate_id = payload.get("aggregate_id")
    if aggregate_id:
        store.update_recording_aggregate(
            aggregate_id, status="failed", error_text=error[:1000],
        )
