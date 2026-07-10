"""Vibry AI Core — OpenAI-compatible chat proxy endpoints"""
import json, time, logging
from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import StreamingResponse, JSONResponse
from app.config import config
from services.proxy import (
    extract_user_message, inject_memories_into_messages,
    build_upstream_payload, stream_to_upstream, proxy_non_streaming,
)
from services.memory import search_memories, format_memories_for_prompt
from services.wiki import is_wiki_initialized, search_wiki
import db

log = logging.getLogger("vibry")
router = APIRouter()

def _get_user_id(request: Request) -> str:
    auth = request.headers.get("Authorization", "")
    return auth[7:].strip() if auth.startswith("Bearer ") else "anonymous"

@router.get("/v1/models")
async def list_models():
    return {"object": "list", "data": [
        {"id": config.upstream.model, "object": "model", "created": 0, "owned_by": "vibry-ai"},
        {"id": config.upstream.embedding_model, "object": "model", "created": 0, "owned_by": "vibry-ai"},
    ]}

@router.post("/v1/embeddings")
async def embeddings_proxy(request: Request):
    import httpx
    body = await request.json()
    model = body.get("model", config.upstream.embedding_model)
    raw_input = body.get("input", "")
    if isinstance(raw_input, str):
        multimodal_input = [{"type": "text", "text": raw_input}]
    elif isinstance(raw_input, list):
        multimodal_input = raw_input if raw_input and isinstance(raw_input[0], dict) else [{"type": "text", "text": t} for t in raw_input]
    else:
        multimodal_input = [{"type": "text", "text": str(raw_input)}]
    upstream_payload = {"model": model, "input": multimodal_input}
    if "encoding_format" in body: upstream_payload["encoding_format"] = body["encoding_format"]
    if "dimensions" in body: upstream_payload["dimensions"] = body["dimensions"]
    multimodal_url = f"{config.upstream.base_url.rstrip('/')}/embeddings/multimodal"
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {config.upstream.api_key}"}
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(multimodal_url, json=upstream_payload, headers=headers)
    if resp.status_code != 200:
        raise HTTPException(status_code=502, detail=f"Embedding upstream error: {resp.status_code}")
    upstream_data = resp.json()
    emb_data = upstream_data.get("data", {})
    standard_data = [{"object": "embedding", "embedding": item.get("embedding", []), "index": i} for i, item in enumerate(emb_data)] if isinstance(emb_data, list) else [{"object": "embedding", "embedding": emb_data.get("embedding", []), "index": 0}]
    return JSONResponse({"object": "list", "data": standard_data, "model": model, "usage": upstream_data.get("usage", {})})

@router.post("/v1/chat/completions")
async def chat_completions(request: Request):
    t0 = time.time()
    user_id = _get_user_id(request)
    try:
        body = await request.json()
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid JSON body")
    messages = body.get("messages", [])
    if not messages: raise HTTPException(status_code=400, detail="messages is required")
    is_stream = body.get("stream", False)
    user_msg, _ = extract_user_message(messages)
    memory_text, wiki_text = "", ""
    if user_msg:
        try:
            memories_found = search_memories(user_msg, user_id=user_id)
            memory_text = format_memories_for_prompt(memories_found)
        except Exception as e: log.warning(f"Memory search failed: {e}")
        if is_wiki_initialized():
            try:
                wiki_results = search_wiki(user_msg, top_k=3)
                if wiki_results:
                    wiki_lines = ["\n## 📚 专业知识 (Wiki RAG)\n"]
                    for wr in wiki_results:
                        wiki_lines.append(f"### [{wr['title']}]({wr.get('path','')})\n{wr.get('snippet','')}\n")
                    wiki_text = "\n".join(wiki_lines)
            except Exception as e: log.warning(f"Wiki search failed: {e}")
    personality = db.get_personality()
    combined_context = ""
    if personality: combined_context = f"## 🎭 人格设定 (Vibry AI)\n{personality}\n\n---\n"
    if wiki_text: combined_context += wiki_text + "\n---\n"
    if memory_text: combined_context += memory_text
    modified_messages = inject_memories_into_messages(messages, combined_context)
    upstream_payload = build_upstream_payload(modified_messages, body)
    if user_msg:
        db.save_chat_message(user_id, "user", user_msg, conversation_id=user_id, model=upstream_payload.get("model",""))
    if is_stream:
        async def sse_gen():
            async for chunk in stream_to_upstream(upstream_payload): yield chunk
        return StreamingResponse(sse_gen(), media_type="text/event-stream", headers={"Cache-Control":"no-cache","Connection":"keep-alive","X-Accel-Buffering":"no","X-Vibry-Memories":str(len(memory_text))})
    else:
        result = await proxy_non_streaming(upstream_payload)
        assistant_text = result.get("choices",[{}])[0].get("message",{}).get("content","")
        if assistant_text:
            db.save_chat_message(user_id, "assistant", assistant_text, conversation_id=user_id, model=upstream_payload.get("model",""), tokens=result.get("usage",{}).get("total_tokens",0))
        return JSONResponse(content=result)
