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
    monkeypatch.setattr(
        asr_providers,
        "_standard_asr_chunks",
        lambda _audio, _fmt: [(b"first", 0), (b"second", 900_000)],
    )
    provider = asr_providers.DoubaoStandardProvider()
    results = iter([
        AsrResult(provider="doubao_standard", text="first", segments=[AsrSegment(id=0, start_ms=10, end_ms=20, text="first", speaker="0")]),
        AsrResult(provider="doubao_standard", text="second", segments=[AsrSegment(id=0, start_ms=30, end_ms=40, text="second", speaker="0")]),
    ])
    monkeypatch.setattr(provider, "_transcribe_chunk", lambda *_args, **_kwargs: next(results))

    merged = provider.transcribe(b"long-recording", audio_fmt="opus")

    assert [(segment.start_ms, segment.speaker) for segment in merged.segments] == [
        (10, "1-0"), (900_030, "2-0"),
    ]
