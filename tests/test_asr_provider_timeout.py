from __future__ import annotations

from services import asr_providers
from services.asr_contract import AsrResult, AsrSegment


class _SubmitResponse:
    headers = {
        "X-Api-Status-Code": "20000000",
        "X-Tt-Logid": "test-log-id",
    }

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False


def test_doubao_standard_uses_configurable_submit_timeout(monkeypatch):
    seen: dict[str, int] = {}

    def fake_urlopen(request, timeout):
        seen["timeout"] = timeout
        return _SubmitResponse()

    monkeypatch.setenv("DOUBAO_ASR_SUBMIT_TIMEOUT", "321")
    monkeypatch.setattr(asr_providers, "_standard_asr_chunks", lambda audio, _fmt: [(audio, 0)])
    monkeypatch.setattr(asr_providers, "compress_for_asr", lambda audio: (audio, "ogg", "opus"))
    monkeypatch.setattr(asr_providers.urllib.request, "urlopen", fake_urlopen)

    provider = asr_providers.DoubaoStandardProvider()
    monkeypatch.setattr(provider, "_poll_result", lambda *args: {"result": {"text": "ok"}})

    result = provider.transcribe(b"audio", audio_fmt="opus")

    assert seen["timeout"] == 321
    assert result.text == "ok"


def test_network_timeout_falls_back_for_invalid_values(monkeypatch):
    monkeypatch.setenv("DOUBAO_ASR_SUBMIT_TIMEOUT", "invalid")
    assert asr_providers._network_timeout("DOUBAO_ASR_SUBMIT_TIMEOUT", 300) == 300


def test_doubao_standard_merges_long_recording_chunks(monkeypatch):
    provider = asr_providers.DoubaoStandardProvider()
    tasks = [
        {"task_id": "first", "logid": "a", "offset_ms": 0, "chunk_index": 0, "submitted_at": 1.0},
        {"task_id": "second", "logid": "b", "offset_ms": 900_000, "chunk_index": 1, "submitted_at": 1.0},
    ]
    results = [
        {"result": {"utterances": [{"text": "first", "start_time": 10, "end_time": 20, "speaker": "0"}]}},
        {"result": {"utterances": [{"text": "second", "start_time": 30, "end_time": 40, "speaker": "0"}]}},
    ]

    merged = provider.merge_task_results(tasks, results)

    assert [(segment.start_ms, segment.speaker) for segment in merged.segments] == [
        (10, "1-0"), (900_030, "2-0"),
    ]


class _QueryResponse:
    def __init__(self, status_code, payload=b"{}"):
        self.headers = {"X-Api-Status-Code": status_code}
        self._payload = payload

    def read(self):
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False


def test_doubao_standard_poll_is_one_short_request(monkeypatch):
    calls = []

    def fake_urlopen(request, timeout):
        calls.append(timeout)
        return _QueryResponse("20000001")

    monkeypatch.setattr(asr_providers.urllib.request, "urlopen", fake_urlopen)
    provider = asr_providers.DoubaoStandardProvider()

    assert provider.poll_task({"task_id": "task", "logid": "log"}) is None
    assert calls == [30]
