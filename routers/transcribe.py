"""Vibry AI Core — ASR + Summarization + Insight endpoints"""
import asyncio, base64, time, logging
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

@router.get("/api/asr-mode")
async def get_asr_mode():
    from services.asr_providers import supported_provider_modes
    return JSONResponse({"asr_mode": config.asr.mode, "providers": supported_provider_modes()})

@router.post("/api/asr-mode")
async def set_asr_mode(request: Request):
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
    return JSONResponse({
        "text": result["text"],
        "audio_url": result.get("audio_url"),
        "audio_token": result.get("audio_token"),
        "recording_id": result.get("recording_id"),
    })

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
        result = await asyncio.to_thread(summarize, transcript, title, context, user_id)
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
    import re
    insight_prompt = config.prompt.insight_prompt if hasattr(config,'prompt') and config.prompt.insight_prompt else ""
    if not insight_prompt: insight_prompt = config.summary.system_prompt
    insight_prompt = insight_prompt.replace("{name}",config.summary.user_name).replace("{role}",config.summary.user_role).replace("{context}",config.summary.user_context)
    async with _summary_lock:
        _summary_queue -= 1
        messages = [{"role":"system","content":insight_prompt},{"role":"user","content":f"Recording: {title}\n\nTranscript:\n{transcript}\n\nContext: {context}"}]
        from services.asr import call_llm
        model = config.summary.effective_model
        result = await asyncio.to_thread(call_llm, model, messages, 180)
        if "error" in result: return JSONResponse({"error": str(result["error"])}, status_code=500)
        # ★ 计费：LLM 按 token 计费
        usage = result.get("usage", {})
        db.log_usage(
            user_id=user_id, endpoint="/api/insight", model=model,
            prompt_tokens=usage.get("prompt_tokens",0),
            completion_tokens=usage.get("completion_tokens",0),
            total_tokens=usage.get("total_tokens",0),
        )
        raw = result.get("choices",[{}])[0].get("message",{}).get("content","")
        match = re.search(r'\{[\s\S]*\}', raw)
        try: parsed = __import__('json').loads(match.group() if match else raw)
        except: parsed = {"core_insight":"","analysis":{"opportunity":"","risk":""},"action_suggestions":[]}
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
    return JSONResponse({
        "text": result["text"],
        "recording_id": result.get("recording_id"),
        "audio_url": result.get("audio_url"),
        "audio_token": result.get("audio_token"),
        "provider": result.get("provider"),
    })
