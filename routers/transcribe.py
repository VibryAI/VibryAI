"""Vibry AI Core — ASR + Summarization + Insight endpoints"""
import asyncio, base64, os, tempfile, time, logging
from pathlib import Path
from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import JSONResponse
from app.config import config
from utils.auth import resolve_user_id
import db

log = logging.getLogger("vibry")
router = APIRouter()

_asr_lock = asyncio.Lock()
_summary_lock = asyncio.Lock()
_asr_queue = 0
_summary_queue = 0

_UPLOAD_STAGE_DIR = Path(__file__).resolve().parents[1] / "data" / "recording_uploads"
_MAX_UPLOAD_BYTES = 200 * 1024 * 1024


async def _stage_upload(upload_file) -> tuple[Path, int]:
    """Stream multipart audio to disk so long recordings do not occupy request memory."""
    _UPLOAD_STAGE_DIR.mkdir(parents=True, exist_ok=True)
    suffix = Path(getattr(upload_file, "filename", "") or "recording.ogg").suffix.lower()
    if suffix not in {".ogg", ".opus", ".wav", ".mp3", ".m4a"}:
        suffix = ".ogg"
    fd, raw_path = tempfile.mkstemp(prefix="upload_", suffix=f"{suffix}.part", dir=_UPLOAD_STAGE_DIR)
    path = Path(raw_path)
    size = 0
    try:
        with os.fdopen(fd, "wb") as target:
            while True:
                chunk = await upload_file.read(1024 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                if size > _MAX_UPLOAD_BYTES:
                    raise ValueError("audio exceeds 200 MB")
                target.write(chunk)
        if size == 0:
            raise ValueError("audio is required")
        return path, size
    except Exception:
        path.unlink(missing_ok=True)
        raise


def _capture_transcript_source(result: dict, title: str, user_id: str, category: str = "") -> dict:
    """Bridge successful ASR output into the Cognitive Core."""
    transcript = (result.get("text") or "").strip()
    if not transcript:
        return result
    try:
        from cognition.store import create_source
        source, job, _ = create_source(
            user_id=user_id,
            source_type="recording",
            content=transcript,
            origin="vibry_card",
            title=title,
            external_id=result.get("recording_id") or "",
            derivation_type="transcript",
            metadata={
                "recording_id": result.get("recording_id", ""),
                "audio_url": result.get("audio_url", ""),
                "legacy_category": category,
            },
        )
        result["source_id"] = source["id"]
        result["cognition_job_id"] = job.get("id", "")
    except Exception as exc:
        log.warning("Cognitive source capture failed: %s", exc)
    return result

@router.get("/api/asr-mode")
async def get_asr_mode():
    from services.asr_providers import supported_provider_modes
    return JSONResponse({"asr_mode": config.asr.mode, "providers": supported_provider_modes()})

@router.post("/api/asr-mode")
async def set_asr_mode(request: Request):
    from utils.auth import check_admin
    if not check_admin(request):
        raise HTTPException(status_code=401, detail="Admin required")
    data = await request.json()
    mode = data.get("mode", config.asr.mode)
    from services.asr_providers import supported_provider_modes
    if mode in supported_provider_modes():
        config.asr.mode = mode
        log.info(f"ASR mode switched: {mode}")
    return JSONResponse({"asr_mode": config.asr.mode})

@router.post("/api/transcribe")
async def api_transcribe(request: Request):
    global _asr_queue; _asr_queue += 1
    qpos = _asr_queue; user_id = resolve_user_id(request); t0 = time.time()
    content_type = request.headers.get("content-type","")
    category = ""
    if "application/json" in content_type:
        data = await request.json()
        audio_b64 = data.get("audio_base64",""); title = data.get("title","")
        category = data.get("category","")
        if not audio_b64: _asr_queue -= 1; raise HTTPException(status_code=400, detail="missing audio_base64")
        audio_bytes = base64.b64decode(audio_b64)
    elif "multipart" in content_type:
        form = await request.form(); audio_file = form.get("audio")
        if audio_file is None: _asr_queue -= 1; raise HTTPException(status_code=400, detail="missing audio")
        audio_bytes = await audio_file.read(); title = form.get("title",""); category = form.get("category","")
    else: _asr_queue -= 1; raise HTTPException(status_code=400, detail="need JSON or multipart")
    size_kb = len(audio_bytes)/1024
    async with _asr_lock:
        _asr_queue -= 1
        from services.asr import transcribe
        result = await asyncio.to_thread(transcribe, audio_bytes, title, user_id, category)
    elapsed = (time.time()-t0)*1000
    if result.get("error"):
        status = 422 if "未识别" in str(result.get("error","")) else 500
        return JSONResponse(result, status_code=status)
    result = _capture_transcript_source(result, title, user_id, category)
    return JSONResponse({
        "text": result["text"],
        "audio_url": result.get("audio_url"),
        "audio_token": result.get("audio_token"),
        "recording_id": result.get("recording_id"),
        "source_id": result.get("source_id"),
        "cognition_job_id": result.get("cognition_job_id"),
    })


@router.post("/api/v2/recordings/process", status_code=202)
async def api_submit_recording(request: Request):
    """Persist an audio upload and return immediately with a durable job id."""
    user_id = resolve_user_id(request)
    content_type = request.headers.get("content-type", "")
    category = ""
    staged_path: Path | None = None
    audio_bytes: bytes | None = None
    if "application/json" in content_type:
        data = await request.json()
        audio_b64 = data.get("audio_base64", "")
        title = data.get("title", "")
        category = data.get("category", "")
        if not audio_b64:
            raise HTTPException(status_code=400, detail="missing audio_base64")
        try:
            audio_bytes = base64.b64decode(audio_b64, validate=True)
        except Exception as exc:
            raise HTTPException(status_code=400, detail="invalid audio_base64") from exc
    elif "multipart" in content_type:
        form = await request.form()
        audio_file = form.get("audio")
        if audio_file is None:
            raise HTTPException(status_code=400, detail="missing audio")
        try:
            staged_path, staged_size = await _stage_upload(audio_file)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        title = form.get("title", "") or getattr(audio_file, "filename", "")
        category = form.get("category", "")
    else:
        raise HTTPException(status_code=400, detail="need JSON or multipart")

    from services.recording_pipeline import submit_recording, submit_recording_file

    try:
        if staged_path is not None:
            recording, job, duplicate = await asyncio.to_thread(
                submit_recording_file,
                staged_path=staged_path,
                file_size=staged_size,
                title=str(title or "recording.ogg"),
                user_id=user_id,
                category=str(category or ""),
            )
        else:
            recording, job, duplicate = await asyncio.to_thread(
                submit_recording,
                audio_bytes=audio_bytes or b"",
                title=str(title or "recording.ogg"),
                user_id=user_id,
                category=str(category or ""),
            )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    finally:
        if staged_path is not None:
            staged_path.unlink(missing_ok=True)
    return JSONResponse(
        {
            "recording_id": recording.get("id"),
            "job_id": job.get("id") if job else None,
            "core_status": recording.get("core_status", recording.get("status", "queued")),
            "recording_insight_status": recording.get("recording_insight_status", "pending"),
            "memory_insight_status": recording.get("memory_insight_status", "pending"),
            "duplicate": duplicate,
        },
        status_code=200 if duplicate and recording.get("core_status") == "completed" else 202,
    )

@router.post("/api/transcribe/voice")
async def api_transcribe_voice(request: Request):
    global _asr_queue; _asr_queue += 1
    qpos = _asr_queue; user_id = resolve_user_id(request); t0 = time.time()
    content_type = request.headers.get("content-type","")
    if "application/json" in content_type:
        data = await request.json()
        audio_b64 = data.get("audio_base64",""); title = data.get("title","voice")
        if not audio_b64: _asr_queue -= 1; raise HTTPException(status_code=400, detail="missing audio_base64")
        audio_bytes = base64.b64decode(audio_b64)
    elif "multipart" in content_type:
        form = await request.form(); audio_file = form.get("audio")
        if audio_file is None: _asr_queue -= 1; raise HTTPException(status_code=400, detail="missing audio")
        audio_bytes = await audio_file.read(); title = form.get("title","voice")
    else: _asr_queue -= 1; raise HTTPException(status_code=400, detail="need JSON or multipart")
    async with _asr_lock:
        _asr_queue -= 1
        from services.asr import transcribe_voice
        result = await asyncio.to_thread(transcribe_voice, audio_bytes, title, user_id)
    elapsed = (time.time()-t0)*1000
    if result.get("error"):
        status = 422 if "未识别" in str(result.get("error","")) else 500
        return JSONResponse(result, status_code=status)
    return JSONResponse({"text": result["text"], "audio_url": result.get("audio_url"), "audio_token": result.get("audio_token")})

@router.post("/api/summarize")
async def api_summarize(request: Request):
    global _summary_queue; _summary_queue += 1
    qpos = _summary_queue; user_id = resolve_user_id(request); t0 = time.time()
    data = await request.json()
    transcript = data.get("transcript",""); title = data.get("record_title") or data.get("title","Recording"); context = data.get("context","")
    if not transcript: _summary_queue -= 1; raise HTTPException(status_code=400, detail="transcript required")
    async with _summary_lock:
        _summary_queue -= 1
        from services.asr import summarize
        persist_recording = data.get("persist", True) is not False
        if persist_recording:
            result = await asyncio.to_thread(summarize, transcript, title, context, user_id)
        else:
            result = await asyncio.to_thread(
                summarize, transcript, title, context, user_id,
                persist_recording=False,
            )
    elapsed = (time.time()-t0)*1000
    if "error" in result: return JSONResponse({"error": result["error"]}, status_code=500)
    return JSONResponse(result)

@router.post("/api/insight")
async def api_insight(request: Request):
    global _summary_queue; _summary_queue += 1
    qpos = _summary_queue; user_id = resolve_user_id(request); t0 = time.time()
    data = await request.json()
    transcript = data.get("transcript",""); title = data.get("record_title","Recording"); context = data.get("context","")
    if not transcript: _summary_queue -= 1; raise HTTPException(status_code=400, detail="transcript required")
    async with _summary_lock:
        _summary_queue -= 1
        from services.recording_pipeline import generate_recording_insight
        try:
            parsed = await asyncio.to_thread(
                generate_recording_insight,
                transcript=transcript,
                title=title,
                context=context,
                user_id=user_id,
            )
        except RuntimeError as exc:
            return JSONResponse({"error": str(exc)}, status_code=500)
    return JSONResponse(parsed)

@router.post("/admin/api/transcribe-upload")
async def admin_transcribe_upload(request: Request):
    """后台上传音频文件转写"""
    from utils.auth import check_admin
    if not check_admin(request): raise HTTPException(status_code=401, detail="Admin required")
    global _asr_queue; _asr_queue += 1
    form = await request.form()
    audio_file = form.get("audio")
    if audio_file is None: _asr_queue -= 1; raise HTTPException(status_code=400, detail="missing audio file")
    audio_bytes = await audio_file.read()
    title = form.get("title","") or getattr(audio_file, "filename", "") or "upload"
    category = form.get("category", "")
    async with _asr_lock:
        _asr_queue -= 1
        from services.asr import transcribe
        result = await asyncio.to_thread(transcribe, audio_bytes, title, "admin", category)
    if result.get("error"):
        return JSONResponse(result, status_code=500)
    result = _capture_transcript_source(result, title, "admin", category)
    return JSONResponse({
        "text": result["text"],
        "recording_id": result.get("recording_id"),
        "audio_url": result.get("audio_url"),
        "audio_token": result.get("audio_token"),
        "provider": result.get("provider"),
        "source_id": result.get("source_id"),
        "cognition_job_id": result.get("cognition_job_id"),
    })
