from __future__ import annotations

from services import asr_providers


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
