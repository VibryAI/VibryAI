"""Discover high-value multi-recording candidates without blocking recording jobs."""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timedelta

import db
from cognition import store


_FILENAME_TIME = re.compile(r"(20\d{6})[-_T]?(\d{6})")
_IGNORED_CATEGORIES = {"", "未分类", "uncategorized", "unknown"}


def _recorded_at(recording: dict) -> datetime | None:
    value = " ".join(
        str(recording.get(key) or "") for key in ("filename", "title")
    )
    match = _FILENAME_TIME.search(value)
    if match:
        try:
            return datetime.strptime("".join(match.groups()), "%Y%m%d%H%M%S")
        except ValueError:
            pass
    raw = str(recording.get("created_at") or "").replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(raw).replace(tzinfo=None)
    except ValueError:
        return None


def _normalized_tags(recording: dict) -> set[str]:
    result = {
        str(item).strip().lower()
        for item in (recording.get("tags") or [])
        if str(item).strip()
    }
    category = str(recording.get("category") or "").strip().lower()
    if category not in _IGNORED_CATEGORIES:
        result.add(category)
    return result


def _is_continuous(first: tuple[datetime, dict], second: tuple[datetime, dict]) -> bool:
    first_at, first_recording = first
    second_at, _ = second
    if first_at.date() != second_at.date():
        return False
    duration = max(0.0, float(first_recording.get("duration_sec") or 0))
    if duration >= 60:
        gap = second_at - (first_at + timedelta(seconds=duration))
        return timedelta(minutes=-3) <= gap <= timedelta(minutes=12)
    return timedelta(minutes=45) <= second_at - first_at <= timedelta(minutes=75)


def _continuous_groups(timed: list[tuple[datetime, dict]]) -> list[list[dict]]:
    groups: list[list[dict]] = []
    current: list[tuple[datetime, dict]] = []
    for item in timed:
        if current and not _is_continuous(current[-1], item):
            if len(current) >= 2:
                groups.append([entry[1] for entry in current])
            current = []
        current.append(item)
    if len(current) >= 2:
        groups.append([entry[1] for entry in current])
    return groups


def _same_topic_groups(
    timed: list[tuple[datetime, dict]], continuous: list[list[dict]],
) -> list[list[dict]]:
    continuous_sets = [{item["id"] for item in group} for group in continuous]
    by_day: dict[str, list[dict]] = {}
    for recorded_at, recording in timed:
        by_day.setdefault(recorded_at.date().isoformat(), []).append(recording)

    groups: list[list[dict]] = []
    for recordings in by_day.values():
        candidates = [item for item in recordings if _normalized_tags(item)]
        if len(candidates) < 2:
            continue
        connected: list[list[dict]] = []
        for recording in candidates:
            tags = _normalized_tags(recording)
            matches = [
                group for group in connected
                if any(tags & _normalized_tags(item) for item in group)
            ]
            if not matches:
                connected.append([recording])
                continue
            target = matches[0]
            target.append(recording)
            for extra in matches[1:]:
                target.extend(extra)
                connected.remove(extra)
        for group in connected:
            ids = {item["id"] for item in group}
            if len(ids) >= 2 and not any(ids <= existing for existing in continuous_sets):
                groups.append(group)
    return groups


def _dedupe_key(suggestion_type: str, recording_ids: list[str]) -> str:
    raw = f"{suggestion_type}:{','.join(sorted(recording_ids))}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _publish(
    *, user_id: str, suggestion_type: str, recordings: list[dict], reason: dict,
) -> dict | None:
    recording_ids = [item["id"] for item in recordings]
    date = _recorded_at(recordings[0])
    date_label = date.strftime("%m月%d日") if date else "近期"
    continuous = suggestion_type == "continuous"
    title = f"{date_label}{'连续录音' if continuous else '同主题录音'}"
    suggestion, duplicate = store.create_recording_group_suggestion(
        user_id=user_id,
        suggestion_type=suggestion_type,
        title=title,
        recording_ids=recording_ids,
        reason=reason,
        dedupe_key=_dedupe_key(suggestion_type, recording_ids),
    )
    if duplicate and suggestion.get("message_id"):
        return None

    thread = store.ensure_thread(
        user_id=user_id, scope_type="global", scope_id="", title="Memory Matrix",
    )
    names = [item.get("title") or item.get("filename") or item["id"] for item in recordings]
    description = (
        "这些文件的开始时间与上一段结束时间连续，可能来自同一次长录音。"
        if continuous else
        "这些录音发生在同一天，并共享项目分类或内容标签，可能属于同一主题。"
    )
    message = store.add_message(
        thread_id=thread["id"],
        user_id=user_id,
        role="assistant",
        content=(
            f"## {title}\n\n{description}\n\n" +
            "\n".join(f"- {name}" for name in names) +
            "\n\n是否合并生成一份完整纪要？"
        ),
        content_format="markdown",
        message_type="recording_group_suggestion",
        metadata={
            "suggestion_id": suggestion["id"],
            "suggestion_type": suggestion_type,
            "recording_ids": recording_ids,
            "status": "pending",
            "action": "accept_recording_group",
        },
    )
    return store.update_recording_group_suggestion(
        suggestion["id"], message_id=message["id"],
    )


def discover_recording_groups(user_id: str, limit: int = 100) -> list[dict]:
    recordings = db.list_recordings(status="completed", user_id=user_id, limit=limit)
    timed = sorted(
        (
            (recorded_at, item)
            for item in recordings
            if (recorded_at := _recorded_at(item)) is not None
            and (item.get("transcript") or "").strip()
        ),
        key=lambda entry: entry[0],
    )
    continuous = _continuous_groups(timed)
    same_topic = _same_topic_groups(timed, continuous)
    created = []
    for group in continuous:
        suggestion = _publish(
            user_id=user_id,
            suggestion_type="continuous",
            recordings=group,
            reason={"rule": "adjacent_recording_time", "confidence": 0.96},
        )
        if suggestion:
            created.append(suggestion)
    for group in same_topic:
        shared = set.intersection(*(_normalized_tags(item) for item in group))
        suggestion = _publish(
            user_id=user_id,
            suggestion_type="same_topic",
            recordings=group,
            reason={
                "rule": "same_day_shared_topic",
                "shared_topics": sorted(shared),
                "confidence": 0.72,
            },
        )
        if suggestion:
            created.append(suggestion)
    return created


def respond_to_group_suggestion(
    *, user_id: str, suggestion_id: str, action: str,
) -> tuple[dict, dict | None]:
    suggestion = store.get_recording_group_suggestion(suggestion_id, user_id)
    if not suggestion:
        raise ValueError("group suggestion not found")
    if action not in {"accept", "reject"}:
        raise ValueError("action must be accept or reject")
    if suggestion["status"] != "pending":
        aggregate = (
            store.get_recording_aggregate(suggestion.get("aggregate_id"), user_id)
            if suggestion.get("aggregate_id") else None
        )
        return suggestion, aggregate

    aggregate = None
    if action == "accept":
        from services.aggregate_pipeline import submit_aggregate

        aggregate, _, _ = submit_aggregate(
            user_id=user_id,
            recording_ids=suggestion["recording_ids"],
            title=suggestion["title"],
        )
        suggestion = store.update_recording_group_suggestion(
            suggestion_id, status="accepted", aggregate_id=aggregate["id"],
        ) or suggestion
    else:
        suggestion = store.update_recording_group_suggestion(
            suggestion_id, status="rejected",
        ) or suggestion

    if suggestion.get("message_id"):
        store.update_message_metadata(
            suggestion["message_id"], user_id,
            {
                "status": suggestion["status"],
                "aggregate_id": suggestion.get("aggregate_id"),
            },
        )
    return suggestion, aggregate
