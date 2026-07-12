"""Vibry AI Core — Recording CRUD + Stats + Audio + Categories endpoints"""
import os, logging
from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import JSONResponse, FileResponse
import db
from app.config import config
from utils.auth import resolve_user_id, check_admin

log = logging.getLogger("vibry")
router = APIRouter()

@router.get("/api/recordings")
async def list_recordings(request: Request, status: str = None, category: str = None, limit: int = 50, offset: int = 0):
    user_id = resolve_user_id(request)
    recordings = db.list_recordings(status=status, user_id=user_id, category=category, limit=limit, offset=offset)
    stats = db.get_stats(user_id=user_id)
    return JSONResponse({"recordings": recordings, "stats": stats})

@router.get("/api/recordings/{rec_id}")
async def get_recording(request: Request, rec_id: str):
    rec = db.get_recording(rec_id)
    if rec is None: raise HTTPException(status_code=404, detail="Record not found")
    logs_data = db.get_analysis_log(rec_id)
    rec["analysis_log"] = logs_data
    return JSONResponse(rec)

@router.delete("/api/recordings/{rec_id}")
async def delete_recording(rec_id: str):
    db.delete_recording(rec_id)
    log.info(f"Deleted recording: {rec_id}")
    return JSONResponse({"ok": True})

@router.patch("/api/recordings/{rec_id}/tags")
async def update_recording_tags(request: Request, rec_id: str):
    data = await request.json()
    tags = data.get("tags", [])
    category = data.get("category")
    rec = db.update_tags(rec_id, tags, category)
    if rec is None: raise HTTPException(status_code=404, detail="Record not found")
    return JSONResponse(rec)

@router.get("/api/audio/{rec_id}")
async def serve_audio(request: Request, rec_id: str):
    token = request.query_params.get("token", "")
    info = db.get_audio_info(rec_id)
    if info is None: raise HTTPException(status_code=404, detail="Recording not found")
    if not info["audio_token"] or token != info["audio_token"]:
        raise HTTPException(status_code=403, detail="Invalid token")
    if not info["audio_path"]: raise HTTPException(status_code=404, detail="Audio not ready")
    audio_dir = config.audio.audio_dir if hasattr(config, 'audio') else "audio"
    filepath = os.path.join(audio_dir, info["audio_path"])
    if not os.path.exists(filepath): raise HTTPException(status_code=404, detail="Audio file missing")
    return FileResponse(filepath, media_type="audio/wav")

@router.get("/api/stats")
async def get_stats(request: Request):
    user_id = resolve_user_id(request)
    return JSONResponse(db.get_stats(user_id=user_id))

# ============================================================
# Categories
# ============================================================

@router.get("/api/categories")
async def list_categories():
    return JSONResponse({"categories": db.list_categories()})

@router.post("/admin/api/categories")
async def create_category(request: Request):
    if not check_admin(request): raise HTTPException(status_code=401, detail="Admin required")
    data = await request.json()
    name = data.get("name", "").strip()
    if not name: raise HTTPException(status_code=400, detail="name required")
    color = data.get("color", "#6366f1")
    sort_order = data.get("sort_order", 0)
    try:
        return JSONResponse(db.create_category(name, color, sort_order))
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Create failed: {e}")

@router.put("/admin/api/categories/{cat_id}")
async def update_category_api(request: Request, cat_id: int):
    if not check_admin(request): raise HTTPException(status_code=401, detail="Admin required")
    data = await request.json()
    ok = db.update_category(cat_id, name=data.get("name"), color=data.get("color"), sort_order=data.get("sort_order"))
    return JSONResponse({"ok": ok})

@router.delete("/admin/api/categories/{cat_id}")
async def delete_category_api(request: Request, cat_id: int):
    if not check_admin(request): raise HTTPException(status_code=401, detail="Admin required")
    ok = db.delete_category(cat_id)
    if not ok: raise HTTPException(status_code=400, detail="Delete failed (not found or '未分类' cannot be deleted)")
    return JSONResponse({"ok": True})
