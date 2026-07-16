"""Scoped project and global conversations backed by cognitive memory."""

from __future__ import annotations

import html
import re
import uuid
from typing import Any

import db
from app.config import config
from cognition import store
from cognition.context import compile_context
from services.proxy import build_upstream_payload, proxy_non_streaming


def _sanitize_html(value: str) -> str:
    value = re.sub(r"<\s*(script|style)[^>]*>.*?<\s*/\s*\1\s*>", "", value, flags=re.I | re.S)
    value = re.sub(r"\s+on\w+\s*=\s*(['\"]).*?\1", "", value, flags=re.I | re.S)
    value = re.sub(r"javascript\s*:", "", value, flags=re.I)
    return value.strip()


def _as_html(value: str) -> str:
    cleaned = value.strip()
    if re.search(r"</?(p|h[1-6]|ul|ol|li|blockquote|strong|em|table|div)\b", cleaned, re.I):
        return _sanitize_html(cleaned)
    paragraphs = [f"<p>{html.escape(part.strip())}</p>" for part in cleaned.split("\n\n") if part.strip()]
    return "".join(paragraphs) or "<p></p>"


def _system_prompt(*, scope_type: str, project: dict | None, memory_context: str) -> str:
    if scope_type == "project" and project:
        identity = (
            f"You are VibryAI working inside project '{project['name']}'. "
            f"The project goal is: {project.get('goal') or 'not set'}. "
            "Answer using project evidence, distinguish facts from inference, and point out conflicts. "
            "Treat user corrections, goals, decisions and constraints as important feedback."
        )
    else:
        identity = (
            "You are VibryAI in the user's global Memory Matrix. Help the user inspect and correct "
            "cross-project understanding. Distinguish confirmed knowledge, candidates and inference. "
            "Return concise safe HTML using only p, h3, ul, ol, li, strong, em and blockquote tags."
        )
    personality = db.get_personality()
    blocks = [identity]
    if personality:
        blocks.append(f"User-configured persona:\n{personality}")
    if memory_context:
        blocks.append(memory_context)
    return "\n\n".join(blocks)


async def send_message(
    *, user_id: str, scope_type: str, scope_id: str, message: str,
    reply_to_id: str | None = None, feedback_action: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict:
    project = None
    project_ids: list[str] = []
    title = "Memory Matrix"
    if scope_type == "project":
        project = store.get_project(scope_id, user_id)
        if not project:
            raise ValueError("project not found")
        project_ids = [scope_id]
        title = project["name"]
    elif scope_type != "global":
        raise ValueError("unsupported conversation scope")

    thread = store.ensure_thread(
        user_id=user_id, scope_type=scope_type, scope_id=scope_id, title=title,
    )
    source, _, _ = store.create_source(
        user_id=user_id,
        source_type="chat",
        content=message,
        origin="vibry_conversation",
        title=f"{title} conversation",
        external_id=f"{thread['id']}:{uuid.uuid4().hex}",
        metadata={
            "thread_id": thread["id"], "scope_type": scope_type,
            "scope_id": scope_id, **(metadata or {}),
        },
        project_ids=project_ids,
    )
    user_message = store.add_message(
        thread_id=thread["id"], user_id=user_id, role="user", content=message,
        content_format="plain", reply_to_id=reply_to_id, source_id=source["id"],
        metadata=metadata,
    )
    if feedback_action and reply_to_id:
        store.add_feedback(
            user_id=user_id, target_type="message", target_id=reply_to_id,
            action=feedback_action, correction={"message": message, "scope": scope_type},
        )

    context = compile_context(
        user_id=user_id, query=message, project_ids=project_ids, token_budget=2200,
    )["context"]
    history = store.list_messages(thread_id=thread["id"], user_id=user_id, limit=24)
    llm_messages = [{"role": "system", "content": _system_prompt(
        scope_type=scope_type, project=project, memory_context=context,
    )}]
    for item in history:
        if item["message_type"] == "chat" and item["role"] in {"user", "assistant"}:
            llm_messages.append({"role": item["role"], "content": item["content"]})

    result = await proxy_non_streaming(build_upstream_payload(llm_messages, {
        "model": config.chat.model, "temperature": 0.3, "max_tokens": 1600,
    }))
    if result.get("error"):
        raise RuntimeError(result["error"].get("message", "chat upstream failed"))
    assistant_text = result.get("choices", [{}])[0].get("message", {}).get("content", "").strip()
    content_format = "html" if scope_type == "global" else "markdown"
    if content_format == "html":
        assistant_text = _as_html(assistant_text)
    assistant_message = store.add_message(
        thread_id=thread["id"], user_id=user_id, role="assistant", content=assistant_text,
        content_format=content_format, reply_to_id=user_message["id"],
        metadata={"model": config.chat.model},
    )
    store.add_event(
        user_id=user_id, project_id=scope_id if scope_type == "project" else None,
        event_type="conversation_turn", actor="user", object_type="thread",
        object_id=thread["id"], payload={
            "user_message_id": user_message["id"],
            "assistant_message_id": assistant_message["id"],
            "feedback_action": feedback_action or "",
        },
    )
    return {
        "thread": thread, "user_message": user_message,
        "assistant_message": assistant_message, "source": source,
    }
