"""Standalone FunASR audio processing server.

Run:
    uvicorn audio_processing_server:app --host 0.0.0.0 --port 8008

This service can be deployed separately from VibryServer and used as a local
ASR + voiceprint backend. It exposes OpenAI-compatible, Vibry-standard, and
Doubao-compatible response shapes.
"""

from __future__ import annotations

import argparse
import base64
import os
from typing import Optional

import uvicorn
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse

from services.asr_providers import FUNASR_MODELS, FunAsrLocalProvider
from services.voiceprint import (
    delete_voiceprint_file,
    extract_voiceprint,
    identify_speaker,
    load_voiceprints,
    save_voiceprint,
)
from utils.audio import convert_to_wav, detect_audio_format

app = FastAPI(
    title="Vibry Audio Processing Server",
    description="Standalone FunASR transcription and voiceprint service.",
    version="1.0.0",
)

DEFAULT_MODEL = os.getenv("FUNASR_MODEL", "sensevoice")
DEFAULT_DEVICE = os.getenv("FUNASR_DEVICE", "cpu")


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "provider": "funasr_local",
        "default_model": DEFAULT_MODEL,
        "device": DEFAULT_DEVICE,
        "models_available": list(FUNASR_MODELS.keys()),
        "voiceprints": list(load_voiceprints().keys()),
    }


@app.get("/v1/models")
async def models():
    return {
        "object": "list",
        "data": [
            {"id": name, "object": "model", "created": 1700000000, "owned_by": "funasr"}
            for name in FUNASR_MODELS
        ],
    }


@app.post("/v1/audio/transcriptions")
async def openai_transcriptions(
    file: UploadFile = File(...),
    model: Optional[str] = Form(default=None),
    language: Optional[str] = Form(default=None),
    response_format: str = Form(default="json"),
):
    result = await _transcribe_upload(file, model=model or DEFAULT_MODEL, language=language)
    if response_format == "verbose_json":
        return JSONResponse(result.to_openai(verbose=True))
    if response_format == "doubao_json":
        return JSONResponse(result.to_doubao_compatible())
    if response_format == "vibry_json":
        return JSONResponse(result.to_dict())
    return JSONResponse({"text": result.text})


@app.post("/api/asr/transcribe")
async def vibry_transcribe(request: Request):
    audio_bytes, filename, model, language, response_format = await _read_audio_request(request)
    provider = FunAsrLocalProvider(model_alias=model or DEFAULT_MODEL, device=DEFAULT_DEVICE)
    result = provider.transcribe(audio_bytes, audio_fmt=detect_audio_format(audio_bytes), language=language)
    payload = result.to_dict()
    payload["filename"] = filename
    if response_format == "doubao_json":
        return JSONResponse(result.to_doubao_compatible())
    if response_format == "openai_json":
        return JSONResponse(result.to_openai(verbose=True))
    return JSONResponse(payload)


@app.post("/api/voiceprint/enroll")
async def voiceprint_enroll(name: str = Form(...), audio: UploadFile = File(...)):
    audio_bytes = await audio.read()
    wav_bytes = convert_to_wav(audio_bytes)
    try:
        vec = extract_voiceprint(wav_bytes)
        save_voiceprint(name.strip(), vec)
        return {"ok": True, "name": name.strip()}
    except Exception as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.post("/api/voiceprint/identify")
async def voiceprint_identify(audio: UploadFile = File(...), threshold: float = Form(default=0.85)):
    audio_bytes = await audio.read()
    wav_bytes = convert_to_wav(audio_bytes)
    try:
        name, confidence = identify_speaker(wav_bytes, threshold=threshold)
        return {"ok": True, "name": name, "confidence": confidence}
    except Exception as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.get("/api/voiceprint/list")
async def voiceprint_list():
    return {"voiceprints": list(load_voiceprints().keys())}


@app.delete("/api/voiceprint/{name}")
async def voiceprint_delete(name: str):
    if not delete_voiceprint_file(name):
        raise HTTPException(status_code=404, detail="voiceprint not found")
    return {"ok": True}


async def _transcribe_upload(file: UploadFile, model: str, language: str | None):
    if model not in FUNASR_MODELS:
        raise HTTPException(status_code=400, detail=f"Unsupported model: {model}")
    content = await file.read()
    provider = FunAsrLocalProvider(model_alias=model, device=DEFAULT_DEVICE)
    return provider.transcribe(content, audio_fmt=detect_audio_format(content), language=language)


async def _read_audio_request(request: Request) -> tuple[bytes, str, str, str | None, str]:
    content_type = request.headers.get("content-type", "")
    if "application/json" in content_type:
        data = await request.json()
        audio_b64 = data.get("audio_base64", "")
        if not audio_b64:
            raise HTTPException(status_code=400, detail="missing audio_base64")
        return (
            base64.b64decode(audio_b64),
            data.get("filename", ""),
            data.get("model", DEFAULT_MODEL),
            data.get("language"),
            data.get("response_format", "vibry_json"),
        )
    if "multipart" in content_type:
        form = await request.form()
        audio_file = form.get("audio") or form.get("file")
        if audio_file is None:
            raise HTTPException(status_code=400, detail="missing audio/file")
        content = await audio_file.read()
        return (
            content,
            getattr(audio_file, "filename", ""),
            str(form.get("model") or DEFAULT_MODEL),
            form.get("language"),
            str(form.get("response_format") or "vibry_json"),
        )
    raise HTTPException(status_code=400, detail="need JSON or multipart")


def main():
    global DEFAULT_MODEL, DEFAULT_DEVICE
    parser = argparse.ArgumentParser(description="Standalone Vibry FunASR audio server")
    parser.add_argument("--host", default=os.getenv("AUDIO_SERVER_HOST", "0.0.0.0"))
    parser.add_argument("--port", type=int, default=int(os.getenv("AUDIO_SERVER_PORT", "8008")))
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--device", default=DEFAULT_DEVICE)
    args = parser.parse_args()

    DEFAULT_MODEL = args.model
    DEFAULT_DEVICE = args.device
    FunAsrLocalProvider(model_alias=DEFAULT_MODEL, device=DEFAULT_DEVICE)._load_model()
    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
