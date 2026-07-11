"""Vibry AI Core — Memory API endpoints"""
from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import JSONResponse
from services.memory import add_memory, search_memories
from utils.auth import resolve_user_id
import logging
log = logging.getLogger("vibry")

router = APIRouter()

@router.post("/api/memories")
async def api_add_memory(request: Request):
    user_id = resolve_user_id(request)
    body = await request.json()
    text = body.get("text", "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="text is required")
    metadata = body.get("metadata", {})
    try:
        result = add_memory(text, user_id=user_id, metadata=metadata)
    except Exception as e:
        log.error(f"Memory write failed: {e}")
        raise HTTPException(status_code=500, detail=f"Memory write failed: {e}")
    return JSONResponse({"ok": True, "user_id": user_id, "result": result})

@router.get("/api/memories")
async def api_search_memories(request: Request, q: str = "", top_k: int = 10):
    user_id = resolve_user_id(request)
    if not q.strip():
        raise HTTPException(status_code=400, detail="query parameter 'q' is required")
    try:
        results = search_memories(q, user_id=user_id, top_k=top_k, threshold=0.0)
    except Exception as e:
        log.error(f"Memory search failed: {e}")
        raise HTTPException(status_code=500, detail=f"Memory search failed: {e}")
    return JSONResponse({"user_id": user_id, "query": q, "count": len(results), "memories": results})
