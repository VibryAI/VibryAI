"""Turn explicit Markdown action items into user-confirmed task cards."""

from __future__ import annotations

import hashlib

from cognition import store
from services.markdown_content import parse_summary_markdown


def _dedupe_key(source_id: str, title: str) -> str:
    return hashlib.sha256(
        f"{source_id}:{title.strip().lower()}".encode("utf-8")
    ).hexdigest()


def publish_action_item_suggestions(source: dict) -> list[dict]:
    metadata = source.get("metadata") or {}
    if source.get("source_type") not in {"recording", "topic_summary"} and metadata.get("kind") not in {
        "minutes", "topic_summary", "aggregate",
    }:
        return []
    parsed = parse_summary_markdown(source.get("content_text") or "")
    actions = [str(item).strip() for item in parsed.get("action_items") or []]
    if not actions:
        return []

    project_ids = store.source_project_ids(
        source_id=source["id"], user_id=source["user_id"],
    )
    project_id = project_ids[0] if len(project_ids) == 1 else None
    thread = store.ensure_thread(
        user_id=source["user_id"], scope_type="global", scope_id="",
        title="Memory Matrix",
    )
    created = []
    for title in actions[:20]:
        suggestion, duplicate = store.create_task_suggestion(
            user_id=source["user_id"],
            source_id=source["id"],
            project_id=project_id,
            title=title,
            dedupe_key=_dedupe_key(source["id"], title),
        )
        if duplicate and suggestion.get("message_id"):
            continue
        message = store.add_message(
            thread_id=thread["id"],
            user_id=source["user_id"],
            role="assistant",
            content=(
                f"## 是否加入待办？\n\n{title}\n\n"
                f"来自《{source.get('title') or '录音纪要'}》中的明确行动项。"
            ),
            content_format="markdown",
            message_type="task_suggestion",
            source_id=source["id"],
            metadata={
                "suggestion_id": suggestion["id"],
                "source_id": source["id"],
                "project_id": project_id,
                "status": "pending",
                "action": "accept_task_suggestion",
            },
        )
        updated = store.update_task_suggestion(
            suggestion["id"], message_id=message["id"],
        )
        if updated:
            created.append(updated)
    return created


def respond_to_task_suggestion(
    *, user_id: str, suggestion_id: str, action: str,
) -> tuple[dict, dict | None]:
    suggestion = store.get_task_suggestion(suggestion_id, user_id)
    if not suggestion:
        raise ValueError("task suggestion not found")
    if action not in {"accept", "reject"}:
        raise ValueError("action must be accept or reject")
    if suggestion["status"] != "pending":
        task = (
            next((item for item in store.list_tasks(user_id=user_id) if item["id"] == suggestion.get("task_id")), None)
            if suggestion.get("task_id") else None
        )
        return suggestion, task

    task = None
    if action == "accept":
        task = store.create_task(
            user_id=user_id,
            project_id=suggestion.get("project_id"),
            title=suggestion["title"],
            source_message_id=suggestion.get("message_id"),
        )
        suggestion = store.update_task_suggestion(
            suggestion_id, status="accepted", task_id=task["id"],
        ) or suggestion
    else:
        suggestion = store.update_task_suggestion(
            suggestion_id, status="rejected",
        ) or suggestion

    if suggestion.get("message_id"):
        store.update_message_metadata(
            suggestion["message_id"], user_id,
            {"status": suggestion["status"], "task_id": suggestion.get("task_id")},
        )
    return suggestion, task
