#!/usr/bin/env python3
"""VibryCard -> VibryServer API compatibility checks.

This test intentionally avoids real ASR/LLM calls. It verifies that the
VibryCard Flask API surface is present on VibryServer and smoke-tests the
response shapes consumed by the Flutter app.
"""

from __future__ import annotations

import base64
import json
import re
import sys
from pathlib import Path

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
WORKSPACE = ROOT.parent
sys.path.insert(0, str(ROOT))


def _flask_routes() -> dict[str, set[str]]:
    source = (WORKSPACE / "VibryCard" / "server" / "server.py").read_text(encoding="utf-8")
    routes: dict[str, set[str]] = {}
    pattern = re.compile(r'@app\.route\("([^"]+)"(?:,\s*methods=\[([^\]]+)\])?\)')
    for path, methods_raw in pattern.findall(source):
        fastapi_path = re.sub(r"<([^>]+)>", r"{\1}", path)
        if methods_raw:
            methods = {m.strip().strip('"\'') for m in methods_raw.split(",")}
        else:
            methods = {"GET"}
        routes.setdefault(fastapi_path, set()).update(methods)
    return routes


def _fastapi_routes() -> dict[str, set[str]]:
    from app.main import app

    routes: dict[str, set[str]] = {}
    for route in app.routes:
        methods = getattr(route, "methods", None)
        if methods:
            routes.setdefault(route.path, set()).update(methods - {"HEAD", "OPTIONS"})
    return routes


def assert_route_compatibility() -> tuple[int, int]:
    flask = _flask_routes()
    fastapi = _fastapi_routes()
    missing: list[str] = []
    for path, methods in flask.items():
        available = fastapi.get(path, set())
        for method in methods:
            if method not in available:
                missing.append(f"{method} {path}")
    if missing:
        raise AssertionError("Missing VibryCard API routes on VibryServer: " + ", ".join(missing))
    return sum(len(v) for v in flask.values()), len(fastapi)


def smoke_test_response_shapes() -> None:
    import db
    import routers.voiceprint as voiceprint_router
    import services.asr as asr_service
    import services.memory as memory_service
    from app.config import config
    from app.main import app

    def fake_transcribe(audio_bytes: bytes, title: str = "", user_id: str = "anonymous") -> dict:
        return {
            "text": "hello from fake asr",
            "audio_url": "/api/audio/rec_20260711_120000",
            "audio_token": "token123",
            "recording_id": "rec_20260711_120000",
            "error": None,
        }

    def fake_summarize(transcript: str, title: str = "", context: str = "", user_id: str = "anonymous") -> dict:
        return {
            "current_intent": "compat test",
            "key_decisions": [],
            "action_items": [],
            "tags": ["test"],
            "detailed_summary": transcript,
        }

    def fake_call_llm(model: str, messages: list[dict], max_time: int = 180) -> dict:
        content = json.dumps({
            "core_insight": "ok",
            "analysis": {"opportunity": "yes", "risk": "low"},
            "action_suggestions": [],
        })
        return {"choices": [{"message": {"content": content}}]}

    asr_service.transcribe = fake_transcribe
    asr_service.summarize = fake_summarize
    asr_service.call_llm = fake_call_llm
    memory_service.get_mem0 = lambda: object()

    fake_utterances = [
        {"text": "first speaker", "start_time": 0, "end_time": 800, "additions": {"speaker": "1"}},
        {"text": "second speaker", "start_time": 900, "end_time": 1800, "additions": {"speaker": "2"}},
    ]
    original_get_recording = db.get_recording
    db.get_recording = lambda rec_id: {
        "id": rec_id,
        "title": "compat",
        "status": "completed",
        "transcript_chars": 20,
        "summary_json": "{}",
        "insight_json": "",
        "utterances_json": json.dumps(fake_utterances),
        "raw_wav_path": "",
    } if rec_id == "rec_20260711_120000" else original_get_recording(rec_id)
    db.upsert_recording = lambda rec_id, **kwargs: {"id": rec_id, **kwargs}

    audio_dir = Path(config.audio.audio_dir)
    audio_dir.mkdir(parents=True, exist_ok=True)
    temp_audio = audio_dir / "rec_20260711_120000.wav"
    temp_audio.write_bytes(b"RIFF" + b"\0" * 2048)

    voiceprint_router.extract_voiceprint = lambda wav_bytes: [0.0] * 40
    voiceprint_router.save_voiceprint = lambda name, vec: None
    voiceprint_router.wav_slice = lambda wav_bytes, start_ms, end_ms: b"RIFF" + b"\0" * 2048
    voiceprint_router.apply_voiceprint_to_transcript = lambda text, utterances, wav_bytes: text.replace("[\u53d1\u8a00\u4eba1]", "[Alice]")

    try:
        with TestClient(app) as client:
            headers = {"Authorization": "Bearer compat_user"}

            health = client.get("/api/health")
            assert health.status_code == 200 and health.json()["status"] == "ok"

            mode = client.get("/api/asr-mode")
            assert mode.status_code == 200 and "asr_mode" in mode.json()

            payload = {"audio_base64": base64.b64encode(b"RIFF" + b"\0" * 2048).decode(), "title": "compat.wav"}
            transcribed = client.post("/api/transcribe", json=payload, headers=headers)
            assert transcribed.status_code == 200
            for key in ("text", "audio_url", "audio_token", "recording_id"):
                assert key in transcribed.json(), f"missing {key} in /api/transcribe"

            summarized = client.post("/api/summarize", json={"transcript": "hello", "record_title": "compat"}, headers=headers)
            assert summarized.status_code == 200 and "current_intent" in summarized.json()

            insight = client.post("/api/insight", json={"transcript": "hello", "record_title": "compat"}, headers=headers)
            assert insight.status_code == 200 and "core_insight" in insight.json()

            discovered = client.post("/api/voiceprint/discover", json={"recording_id": "rec_20260711_120000"})
            assert discovered.status_code == 200
            assert discovered.json()["speaker_count"] == 2

            enrolled = client.post(
                "/api/voiceprint/discover/enroll",
                json={"recording_id": "rec_20260711_120000", "names": {"1": "Alice"}},
            )
            assert enrolled.status_code == 200
            assert enrolled.json()["speaker_mapping"] == {"1": "Alice"}

            vp_list = client.get("/api/voiceprint/list")
            assert vp_list.status_code == 200 and "voiceprints" in vp_list.json()
    finally:
        temp_audio.unlink(missing_ok=True)


def main() -> None:
    vibrycard_count, vibryserver_count = assert_route_compatibility()
    smoke_test_response_shapes()
    print(f"API route compatibility: {vibrycard_count}/{vibrycard_count} VibryCard routes present")
    print(f"VibryServer total exposed HTTP methods: {vibryserver_count}")
    print("Response shape smoke tests: passed")


if __name__ == "__main__":
    main()
