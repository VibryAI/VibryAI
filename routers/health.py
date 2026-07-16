"""Vibry AI Core — Health check endpoint"""
from fastapi import APIRouter
from fastapi.responses import JSONResponse
from app.config import config

router = APIRouter()

@router.get("/api/health")
async def health():
    from services.asr_providers import supported_provider_modes
    try:
        import db
        db.init_db()
        cognition_status = "ok"
    except Exception as e:
        cognition_status = f"unavailable: {e}"
    return JSONResponse({
        "status": "ok", "version": "1.0.0",
        "server": f"http://{config.server.host}:{config.server.port}",
        "chat_model": config.chat.model,
        "embedding_model": config.embedding.model,
        "asr_mode": config.asr.mode,
        "asr_providers": supported_provider_modes(),
        "cognition": cognition_status,
        "queue": {"asr": 0, "summary": 0},
    })
