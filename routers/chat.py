"""OpenAI-compatible gateway backed by the Vibry.AI cognitive core."""

from __future__ import annotations

import asyncio
import json
import logging
import time

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

import db
from app.config import config
from services.proxy import (
    build_upstream_payload,
    extract_user_message,
    inject_context_into_messages,
    proxy_non_streaming,
    stream_to_upstream,
)
from utils.auth import resolve_user_id


log = logging.getLogger("vibry")
router = APIRouter()


@router.get("/v1/models")
async def list_models():
    return {"object": "list", "data": [
        {"id": config.chat.model, "object": "model", "created": 0, "owned_by": "vibry-ai"},
        {"id": config.embedding.model, "object": "model", "created": 0, "owned_by": "vibry-ai"},
    ]}


@router.post("/v1/embeddings")
async def embeddings_proxy(request: Request):
    import httpx

    body = await request.json()
    model = body.get("model", config.embedding.model)
    raw_input = body.get("input", "")
    if isinstance(raw_input, str):
        multimodal_input = [{"type": "text", "text": raw_input}]
    elif isinstance(raw_input, list):
        multimodal_input = raw_input if raw_input and isinstance(raw_input[0], dict) else [
            {"type": "text", "text": item} for item in raw_input
        ]
    else:
        multimodal_input = [{"type": "text", "text": str(raw_input)}]
    upstream_payload = {"model": model, "input": multimodal_input}
    if "encoding_format" in body:
        upstream_payload["encoding_format"] = body["encoding_format"]
    if "dimensions" in body:
        upstream_payload["dimensions"] = body["dimensions"]
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {config.embedding.api_key}"}
    url = f"{config.embedding.base_url.rstrip('/')}/embeddings/multimodal"
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(url, json=upstream_payload, headers=headers)
    if response.status_code != 200:
        raise HTTPException(status_code=502, detail=f"Embedding upstream error: {response.status_code}")
    upstream_data = response.json()
    embedding_data = upstream_data.get("data", {})
    if isinstance(embedding_data, list):
        data = [{"object": "embedding", "embedding": item.get("embedding", []), "index": index}
                for index, item in enumerate(embedding_data)]
    else:
        data = [{"object": "embedding", "embedding": embedding_data.get("embedding", []), "index": 0}]
    return JSONResponse({"object": "list", "data": data, "model": model, "usage": upstream_data.get("usage", {})})


def _context_headers(request: Request) -> tuple[str, list[str], int]:
    mode = request.headers.get("X-Vibry-Context-Mode", "auto").lower()
    projects = [value.strip() for value in request.headers.get("X-Vibry-Project", "").split(",") if value.strip()]
    try:
        budget = int(request.headers.get("X-Vibry-Context-Budget", "1200"))
    except ValueError:
        budget = 1200
    return mode, projects, max(200, min(budget, 8000))


def _capture_source(*, user_id: str, content: str, external_id: str, derivation_type: str = "original", metadata: dict | None = None) -> None:
    try:
        from cognition.store import create_source
        create_source(
            user_id=user_id, source_type="chat", content=content, origin="gateway",
            title="Gateway conversation", external_id=external_id, derivation_type=derivation_type,
            metadata=metadata or {},
        )
    except Exception as exc:
        log.warning("Gateway source capture failed: %s", exc)


@router.post("/v1/chat/completions")
async def chat_completions(request: Request):
    started = time.time()
    user_id = resolve_user_id(request)
    try:
        body = await request.json()
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail="Invalid JSON body") from exc
    messages = body.get("messages", [])
    if not messages:
        raise HTTPException(status_code=400, detail="messages is required")

    user_message, _ = extract_user_message(messages)
    conversation_id = body.get("conversation_id", "default")
    context = ""
    if user_message:
        mode, project_ids, budget = _context_headers(request)
        if mode != "none":
            try:
                from cognition.context import compile_context
                context = compile_context(
                    user_id=user_id, query=user_message, project_ids=project_ids, token_budget=budget,
                )["context"]
            except Exception as exc:
                log.warning("Cognitive context failed: %s", exc)

    personality = db.get_personality()
    system_context = ""
    if personality:
        system_context += f"## Vibry.AI Persona\n{personality}\n\n---\n"
    if context:
        system_context += context
    modified_messages = inject_context_into_messages(messages, system_context)
    upstream_payload = build_upstream_payload(modified_messages, body)
    if user_message:
        db.save_chat_message(user_id, "user", user_message, conversation_id=conversation_id, model=upstream_payload.get("model", ""))
        _capture_source(
            user_id=user_id, content=user_message,
            external_id=f"{conversation_id}:user:{int(started * 1000)}",
            metadata={"conversation_id": conversation_id},
        )

    if body.get("stream", False):
        async def sse_gen():
            async for chunk in stream_to_upstream(upstream_payload):
                yield chunk
        return StreamingResponse(
            sse_gen(), media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no",
                     "X-Vibry-Context": str(len(context))},
        )

    result = await proxy_non_streaming(upstream_payload)
    assistant_text = result.get("choices", [{}])[0].get("message", {}).get("content", "")
    if assistant_text:
        usage = result.get("usage", {})
        db.save_chat_message(
            user_id, "assistant", assistant_text, conversation_id=conversation_id,
            model=upstream_payload.get("model", ""), tokens=usage.get("total_tokens", 0),
        )
        db.log_usage(
            user_id=user_id, endpoint="/v1/chat/completions", model=upstream_payload.get("model", ""),
            prompt_tokens=usage.get("prompt_tokens", 0), completion_tokens=usage.get("completion_tokens", 0),
            total_tokens=usage.get("total_tokens", 0),
        )
        _capture_source(
            user_id=user_id, content=f"User: {user_message}\nAssistant: {assistant_text[:4000]}",
            external_id=f"{conversation_id}:assistant:{int(started * 1000)}", derivation_type="agent_output",
            metadata={"conversation_id": conversation_id},
        )
    return JSONResponse(content=result)
