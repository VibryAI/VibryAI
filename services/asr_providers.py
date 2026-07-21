"""ASR provider adapters for local and cloud engines."""

from __future__ import annotations

import base64
import json
import logging
import os
import tempfile
import threading
import time
import types
import urllib.error
import urllib.request
import uuid
from abc import ABC, abstractmethod
from typing import Any

from app.config import config
from services.asr_contract import AsrResult, AsrSegment, build_result_from_text
from utils.audio import compress_for_asr

log = logging.getLogger("vibry.asr.providers")

os.environ.setdefault("HF_ENDPOINT", getattr(config.asr, "hf_endpoint", "https://hf-mirror.com"))


def _network_timeout(name: str, default: int, *, minimum: int = 30) -> int:
    """Read a bounded network timeout without allowing a bad env value to fail ASR."""
    try:
        value = int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default
    return max(minimum, value)


FUNASR_MODELS: dict[str, dict[str, Any]] = {
    "sensevoice": {
        "model": "iic/SenseVoiceSmall",
        "vad_model": "fsmn-vad",
        "vad_kwargs": {"max_single_segment_time": 30000},
        "spk_model": "cam++",
    },
    "paraformer": {
        "model": "paraformer-zh",
        "vad_model": "fsmn-vad",
        "punc_model": "ct-punc",
        "spk_model": "cam++",
    },
    "paraformer-en": {
        "model": "paraformer-en",
        "vad_model": "fsmn-vad",
    },
    "fun-asr-nano": {
        "model": "FunAudioLLM/Fun-ASR-Nano-2512",
        "hub": "hf",
        "trust_remote_code": True,
        "vad_model": "fsmn-vad",
        "vad_kwargs": {"max_single_segment_time": 30000},
    },
}

PROVIDER_ALIASES = {
    # 云端豆包 ASR
    "cloud": "doubao_flash",
    "cloud_flash": "doubao_flash",
    "doubao": "doubao_flash",
    "doubao_flash": "doubao_flash",
    "cloud_standard": "doubao_standard",
    "doubao_standard": "doubao_standard",
    # 本地 FunASR 独立服务 (8008 端口)
    "funasr_server": "funasr_server",
}

_model_lock = threading.Lock()
_funasr_models: dict[tuple[str, str], Any] = {}


class BaseAsrProvider(ABC):
    name: str

    @abstractmethod
    def transcribe(self, audio_bytes: bytes, audio_fmt: str | None = None, language: str | None = None) -> AsrResult:
        raise NotImplementedError


class FunAsrLocalProvider(BaseAsrProvider):
    name = "funasr_local"

    def __init__(self, model_alias: str | None = None, device: str | None = None):
        self.model_alias = model_alias or getattr(config.asr, "funasr_model", "sensevoice")
        self.device = device or getattr(config.asr, "funasr_device", os.getenv("FUNASR_DEVICE", "cpu"))

    def _load_model(self):
        alias = self.model_alias if self.model_alias in FUNASR_MODELS else "sensevoice"
        key = (alias, self.device)
        if key in _funasr_models:
            return _funasr_models[key]
        with _model_lock:
            if key in _funasr_models:
                return _funasr_models[key]
            import sys
            if "editdistance" not in sys.modules:
                editdistance = types.ModuleType("editdistance")
                editdistance.eval = lambda *args, **kwargs: 0
                sys.modules["editdistance"] = editdistance
            from funasr import AutoModel
            cfg = dict(FUNASR_MODELS[alias])
            cfg["device"] = self.device
            cfg["disable_update"] = True
            log.info("Loading FunASR model alias=%s device=%s", alias, self.device)
            t0 = time.time()
            _funasr_models[key] = AutoModel(**cfg)
            log.info("FunASR model loaded in %.1fs", time.time() - t0)
            return _funasr_models[key]

    def transcribe(self, audio_bytes: bytes, audio_fmt: str | None = None, language: str | None = None) -> AsrResult:
        suffix = f".{audio_fmt}" if audio_fmt else ".wav"
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
            tmp.write(audio_bytes)
            tmp_path = tmp.name
        try:
            model = self._load_model()
            t0 = time.time()
            kwargs: dict[str, Any] = {"input": tmp_path, "batch_size": 1}
            if language:
                kwargs["language"] = language
            result = model.generate(**kwargs)
            elapsed_ms = int((time.time() - t0) * 1000)
            item = result[0] if result else {}
            text = _extract_text(item)
            sentence_info = item.get("sentence_info") or []
            segments = [
                AsrSegment.from_funasr_sentence(i, seg)
                for i, seg in enumerate(sentence_info)
                if isinstance(seg, dict)
            ]
            return AsrResult(
                provider=self.name,
                model=self.model_alias,
                text=text,
                language=language or "auto",
                duration_ms=elapsed_ms,
                segments=segments,
                raw={"funasr": item},
            )
        finally:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


class OpenAICompatibleAudioProvider(BaseAsrProvider):
    """OpenAI-compatible audio transcription provider.

    Used by funasr_server (local FunASR service on port 8008).
    """

    name = "funasr_server"

    def __init__(self, base_url: str | None = None, model: str | None = None, provider_name: str | None = None):
        self.base_url = (base_url or getattr(config.asr, "funasr_server_url", "http://127.0.0.1:8008") or "").rstrip("/")
        self.model = model or getattr(config.asr, "funasr_model", "sensevoice")
        if provider_name:
            self.name = provider_name

    def transcribe(self, audio_bytes: bytes, audio_fmt: str | None = None, language: str | None = None) -> AsrResult:
        if not self.base_url:
            raise RuntimeError("ASR_OPENAI_AUDIO_BASE_URL is not configured")
        import httpx
        endpoint = f"{self.base_url}/v1/audio/transcriptions"
        files = {"file": (f"audio.{audio_fmt or 'wav'}", audio_bytes, "application/octet-stream")}
        data: dict[str, str] = {"model": self.model, "response_format": "verbose_json"}
        if language:
            data["language"] = language
        t0 = time.time()
        with httpx.Client(timeout=180) as client:
            resp = client.post(endpoint, files=files, data=data)
            resp.raise_for_status()
            payload = resp.json()
        segments = [
            AsrSegment.from_openai_segment(i, seg)
            for i, seg in enumerate(payload.get("segments", []) or [])
            if isinstance(seg, dict)
        ]
        return AsrResult(
            provider=self.name,
            model=payload.get("model") or self.model,
            text=(payload.get("text") or "").strip(),
            language=payload.get("language") or language or "auto",
            duration_ms=int((time.time() - t0) * 1000),
            segments=segments,
            raw={"openai_audio": payload},
        )


class DoubaoFlashProvider(BaseAsrProvider):
    name = "doubao_flash"

    def transcribe(self, audio_bytes: bytes, audio_fmt: str | None = None, language: str | None = None) -> AsrResult:
        doubao = config.doubao_asr
        app_id = doubao.app_id or os.getenv("DOUBAO_ASR_APP_ID", "")
        access_key = doubao.access_key or os.getenv("DOUBAO_ASR_ACCESS_KEY", "")
        flash_url = doubao.flash_url or os.getenv(
            "DOUBAO_ASR_FLASH_URL",
            "https://openspeech.bytedance.com/api/v3/auc/bigmodel/recognize/flash",
        )
        audio_b64 = base64.b64encode(audio_bytes).decode("ascii")
        request_id = str(uuid.uuid4())
        headers = {
            "Content-Type": "application/json",
            "X-Api-App-Key": app_id,
            "X-Api-Access-Key": access_key,
            "X-Api-Resource-Id": "volc.bigasr.auc_turbo",
            "X-Api-Request-Id": request_id,
            "X-Api-Sequence": "-1",
        }
        body = json.dumps({
            "user": {"uid": app_id},
            "audio": {"data": audio_b64},
            "request": {"model_name": "bigmodel"},
        }).encode("utf-8")
        t0 = time.time()
        req = urllib.request.Request(flash_url, data=body, method="POST")
        for key, value in headers.items():
            req.add_header(key, value)
        with urllib.request.urlopen(req, timeout=120) as resp:
            result = json.loads(resp.read().decode("utf-8"))
        text = result.get("result", {}).get("text", "") if isinstance(result, dict) else ""
        return build_result_from_text(
            self.name,
            text,
            model="bigmodel-flash",
            raw={"doubao": result, "request_id": request_id, "duration_ms": int((time.time() - t0) * 1000)},
        )


class DoubaoStandardProvider(BaseAsrProvider):
    name = "doubao_standard"

    def transcribe(self, audio_bytes: bytes, audio_fmt: str | None = None, language: str | None = None) -> AsrResult:
        doubao = config.doubao_asr
        app_id = doubao.app_id or os.getenv("DOUBAO_ASR_APP_ID", "")
        access_key = doubao.access_key or os.getenv("DOUBAO_ASR_ACCESS_KEY", "")
        standard_url = doubao.standard_url or os.getenv(
            "DOUBAO_ASR_STANDARD_URL",
            "https://openspeech-direct.zijieapi.com/api/v3/auc/bigmodel/submit",
        )
        asr_audio, asr_fmt, asr_codec = compress_for_asr(audio_bytes)
        task_id = str(uuid.uuid4())
        audio_b64 = base64.b64encode(asr_audio).decode("ascii")
        audio_field: dict[str, Any] = {"data": audio_b64, "format": asr_fmt}
        if asr_codec:
            audio_field["codec"] = asr_codec
        headers = {
            "Content-Type": "application/json",
            "X-Api-App-Key": app_id,
            "X-Api-Access-Key": access_key,
            "X-Api-Resource-Id": "volc.bigasr.auc",
            "X-Api-Request-Id": task_id,
            "X-Api-Sequence": "-1",
        }
        body = json.dumps({
            "user": {"uid": app_id},
            "audio": audio_field,
            "request": {
                "model_name": "bigmodel",
                "enable_speaker_info": True,
                "enable_channel_split": True,
                "enable_ddc": True,
                "enable_punc": True,
                "enable_itn": True,
            },
        }).encode("utf-8")

        t0 = time.time()
        req = urllib.request.Request(standard_url, data=body, method="POST")
        for key, value in headers.items():
            req.add_header(key, value)
        # Standard ASR receives a base64 payload. Long recordings can take more
        # than one minute to write to the upstream service on constrained links.
        submit_timeout = _network_timeout("DOUBAO_ASR_SUBMIT_TIMEOUT", 300)
        with urllib.request.urlopen(req, timeout=submit_timeout) as resp:
            status_code = resp.headers.get("X-Api-Status-Code", "")
            logid = resp.headers.get("X-Tt-Logid", "")
            if status_code != "20000000":
                msg = resp.headers.get("X-Api-Message", "unknown error")
                raise RuntimeError(f"Doubao submit failed: {msg}")

        result = self._poll_result(standard_url, app_id, access_key, task_id, logid)
        payload = result.get("result", {}) if isinstance(result, dict) else {}
        utterances = payload.get("utterances", []) or []
        segments = [
            AsrSegment.from_doubao_utterance(i, utt)
            for i, utt in enumerate(utterances)
            if isinstance(utt, dict)
        ]
        text = payload.get("text") or "\n".join(seg.text for seg in segments if seg.text)
        formatted = AsrResult(
            provider=self.name,
            model="bigmodel-standard",
            text=text.strip(),
            language=language or "auto",
            duration_ms=int((time.time() - t0) * 1000),
            segments=segments,
            raw={"doubao": result, "request_id": task_id, "logid": logid},
        )
        if formatted.segments:
            formatted.text = formatted.formatted_text() or formatted.text
        return formatted

    def _poll_result(self, standard_url: str, app_id: str, access_key: str, task_id: str, logid: str) -> dict[str, Any]:
        query_url = (
            standard_url.replace("/submit", "/query")
            if standard_url.endswith("/submit")
            else "https://openspeech-direct.zijieapi.com/api/v3/auc/bigmodel/query"
        )
        headers = {
            "Content-Type": "application/json",
            "X-Api-App-Key": app_id,
            "X-Api-Access-Key": access_key,
            "X-Api-Resource-Id": "volc.bigasr.auc",
            "X-Api-Request-Id": task_id,
            "X-Tt-Logid": logid,
        }
        max_wait = int(os.getenv("DOUBAO_ASR_MAX_WAIT", "120"))
        elapsed = 0
        while elapsed < max_wait:
            time.sleep(2)
            elapsed += 2
            req = urllib.request.Request(query_url, data=b"{}", method="POST")
            for key, value in headers.items():
                req.add_header(key, value)
            with urllib.request.urlopen(req, timeout=30) as resp:
                code = resp.headers.get("X-Api-Status-Code", "")
                if code == "20000000":
                    return json.loads(resp.read().decode("utf-8"))
                if code in ("20000001", "20000002"):
                    continue
                msg = resp.headers.get("X-Api-Message", "unknown error")
                raise RuntimeError(f"Doubao query failed: {msg}")
        raise TimeoutError(f"Doubao query timeout after {max_wait}s")


def get_asr_provider(mode: str | None = None) -> BaseAsrProvider:
    provider = PROVIDER_ALIASES.get((mode or config.asr.mode or "cloud").strip(), mode or config.asr.mode)
    if provider == "funasr_server":
        return OpenAICompatibleAudioProvider(
            base_url=getattr(config.asr, "funasr_server_url", "http://127.0.0.1:8008"),
            model=getattr(config.asr, "funasr_model", "sensevoice"),
            provider_name="funasr_server",
        )
    if provider == "doubao_standard":
        return DoubaoStandardProvider()
    if provider == "doubao_flash":
        return DoubaoFlashProvider()
    raise ValueError(f"Unsupported ASR provider: {mode}")


def supported_provider_modes() -> tuple[str, ...]:
    return tuple(sorted(PROVIDER_ALIASES.keys()))


def _extract_text(item: dict[str, Any]) -> str:
    text = item.get("text") or item.get("sentence") or ""
    if not text and item.get("sentence_info"):
        text = "".join((seg.get("sentence") or seg.get("text") or "") for seg in item["sentence_info"])
    return str(text).strip()
