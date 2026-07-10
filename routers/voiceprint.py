"""Vibry AI Core — Voiceprint API endpoints"""
import json, os, logging
from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import JSONResponse
from app.config import config
from services.voiceprint import (
    extract_voiceprint, load_voiceprints, save_voiceprint, delete_voiceprint_file,
    wav_slice, apply_voiceprint_to_transcript,
)
import db

log = logging.getLogger("vibry")
router = APIRouter()

@router.post("/api/voiceprint/enroll")
async def voiceprint_enroll(request: Request):
    form = await request.form()
    name = (form.get("name","") or "").strip()
    if not name: raise HTTPException(status_code=400, detail="name required")
    audio_file = form.get("audio")
    if audio_file is None: raise HTTPException(status_code=400, detail="audio required")
    audio_bytes = await audio_file.read()
    if len(audio_bytes) < 1000: raise HTTPException(status_code=400, detail="audio too short")
    from utils.audio import convert_to_wav
    wav_bytes = convert_to_wav(audio_bytes)
    try:
        vec = extract_voiceprint(wav_bytes)
        save_voiceprint(name, vec)
        return JSONResponse({"ok": True, "name": name})
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))

@router.get("/api/voiceprint/list")
async def voiceprint_list():
    return JSONResponse({"voiceprints": list(load_voiceprints().keys())})

@router.delete("/api/voiceprint/{name}")
async def voiceprint_delete(name: str):
    ok = delete_voiceprint_file(name)
    if not ok: raise HTTPException(status_code=404, detail="voiceprint not found")
    return JSONResponse({"ok": True})

@router.post("/api/voiceprint/discover")
async def voiceprint_discover(request: Request):
    data = await request.json()
    recording_id = (data.get("recording_id","") or "").strip()
    if not recording_id: raise HTTPException(status_code=400, detail="recording_id required")
    rec = db.get_recording(recording_id)
    if rec is None: raise HTTPException(status_code=404, detail="recording not found")
    utterances_json = rec.get("utterances_json","")
    if not utterances_json: raise HTTPException(status_code=422, detail="no speaker data")
    try: utterances = json.loads(utterances_json)
    except: raise HTTPException(status_code=500, detail="corrupted speaker data")
    speaker_utterances = {}
    for u in utterances:
        sid = str((u.get("additions",{}) or {}).get("speaker","?"))
        if sid != "?": speaker_utterances.setdefault(sid,[]).append(u)
    if not speaker_utterances: raise HTTPException(status_code=422, detail="no speakers detected")
    speakers = []
    for sid in sorted(speaker_utterances.keys(), key=int):
        us = speaker_utterances[sid]; best = max(us, key=lambda u: u.get("end_time",0)-u.get("start_time",0))
        speakers.append({"speaker_id":sid,"sample_text":best.get("text","").strip(),"start_ms":best.get("start_time",0),"end_ms":best.get("end_time",0),"duration_ms":best.get("end_time",0)-best.get("start_time",0),"utterance_count":len(us)})
    return JSONResponse({"recording_id":recording_id,"speaker_count":len(speakers),"speakers":speakers})

@router.post("/api/voiceprint/discover/enroll")
async def voiceprint_discover_enroll(request: Request):
    data = await request.json()
    recording_id = (data.get("recording_id","") or "").strip()
    names = data.get("names",{})
    if not recording_id: raise HTTPException(status_code=400, detail="recording_id required")
    if not names or not isinstance(names,dict): raise HTTPException(status_code=400, detail="names required")
    names = {k:v.strip() for k,v in names.items() if v and v.strip()}
    if not names: raise HTTPException(status_code=400, detail="at least one name required")
    rec = db.get_recording(recording_id)
    if rec is None: raise HTTPException(status_code=404, detail="recording not found")
    utterances_json = rec.get("utterances_json",""); raw_wav_path = rec.get("raw_wav_path","")
    if not utterances_json: raise HTTPException(status_code=422, detail="no speaker data")
    try: utterances = json.loads(utterances_json)
    except: raise HTTPException(status_code=500, detail="corrupted speaker data")
    wav_bytes = None
    base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if raw_wav_path:
        full = os.path.join(base_dir, raw_wav_path)
        if os.path.exists(full):
            with open(full,"rb") as f: wav_bytes = f.read()
    if wav_bytes is None:
        audio_dir = config.audio.audio_dir if hasattr(config,'audio') else "audio"
        ep = os.path.join(audio_dir, f"{recording_id}.wav")
        if os.path.exists(ep):
            with open(ep,"rb") as f: wav_bytes = f.read()
    if wav_bytes is None: raise HTTPException(status_code=422, detail="audio unavailable")
    enrolled, skipped = [], []
    for sid, name in names.items():
        speaker_us = [u for u in utterances if str((u.get("additions",{}) or {}).get("speaker","?"))==sid]
        if not speaker_us: skipped.append({"speaker_id":sid,"name":name,"reason":"no audio segment"}); continue
        best = max(speaker_us, key=lambda u: u.get("end_time",0)-u.get("start_time",0))
        try:
            seg = wav_slice(wav_bytes, best["start_time"], best["end_time"])
            vec = extract_voiceprint(seg)
            save_voiceprint(name, vec)
            enrolled.append(name)
        except Exception as e: skipped.append({"speaker_id":sid,"name":name,"reason":str(e)})
    if not enrolled: return JSONResponse({"error":"enrollment failed","skipped_speakers":skipped}, status_code=500)
    lines = []
    for u in utterances:
        sid = (u.get("additions",{}) or {}).get("speaker","?")
        if u.get("text","").strip(): lines.append(f"[Speaker {sid}] {u['text']}")
    updated_text = apply_voiceprint_to_transcript("\n".join(lines), utterances, wav_bytes)
    db.upsert_recording(recording_id, transcript=updated_text, transcript_chars=len(updated_text))
    return JSONResponse({"ok":True,"updated_transcript":updated_text,"voiceprints_enrolled":enrolled,"skipped_speakers":skipped if skipped else None})

@router.get("/api/recording-status/{rec_id}")
async def recording_status(rec_id: str):
    rec = db.get_recording(rec_id)
    if rec is None: raise HTTPException(status_code=404, detail="not found")
    return JSONResponse({"id":rec["id"],"title":rec["title"],"status":rec["status"],"transcript_chars":rec.get("transcript_chars",0),"has_summary":bool(rec.get("summary_json","")),"has_insight":bool(rec.get("insight_json",""))})
