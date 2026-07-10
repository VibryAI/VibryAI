"""Vibry AI Core — Health check endpoint"""
from fastapi import APIRouter
from fastapi.responses import JSONResponse
from app.config import config

router = APIRouter()

@router.get("/api/health")
async def health():
    from services.memory import get_mem0
    try:
        _ = get_mem0()
        mem0_status = "ok"
    except Exception as e:
        mem0_status = f"unavailable: {e}"
    return JSONResponse({
        "status": "ok", "version": "0.2.0",
        "server": f"http://{config.server.host}:{config.server.port}",
        "upstream": config.upstream.model,
        "asr_mode": config.asr.mode,
        "mem0": mem0_status,
        "memory_config": {
            "top_k": config.memory.top_k, "threshold": config.memory.threshold,
            "vector_store": config.memory.vector_store,
        },
        "queue": {"asr": 0, "summary": 0},
    })
