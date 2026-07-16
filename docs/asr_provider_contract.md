# Vibry ASR Provider Contract

VibryServer now treats ASR engines as providers. Every provider returns the same
internal result and can be rendered as:

- Vibry standard JSON: used by `/api/transcribe` and `/api/asr/transcribe`.
- OpenAI-compatible JSON: used by `/v1/audio/transcriptions`.
- Doubao-compatible JSON: `{"result": {"text": "...", "utterances": [...]}}`.

## Provider Names

Current provider aliases:

- `funasr_local` or legacy `local`: local FunASR, default model `sensevoice`.
- `funasr_server`: a separately deployed FunASR/OpenAI-compatible audio server.
- `openai_audio` or `openai_compatible`: any OpenAI-style audio transcription endpoint.
- `whisper_local` or `whisper`: local OpenAI Whisper, optional dependency.
- `doubao_flash`, legacy `cloud`/`cloud_flash`: Doubao synchronous flash ASR.
- `doubao_standard`, legacy `cloud_standard`: Doubao async standard ASR with speaker diarization.

Recommended configuration:

```env
ASR_MODE=funasr_local
ASR_VOICE_MODE=doubao_flash
FUNASR_MODEL=sensevoice
FUNASR_DEVICE=cpu
FUNASR_SERVER_URL=http://127.0.0.1:8008
ASR_OPENAI_AUDIO_BASE_URL=
ASR_OPENAI_AUDIO_MODEL=
WHISPER_SIZE=small
WHISPER_DEVICE=cpu
```

## Standard Result

```json
{
  "provider": "funasr_local",
  "model": "sensevoice",
  "text": "[发言人0] 你好。",
  "language": "auto",
  "duration_ms": 1234,
  "segments": [
    {
      "id": 0,
      "start_ms": 640,
      "end_ms": 1860,
      "text": "你好。",
      "speaker": "0",
      "confidence": null
    }
  ],
  "utterances": [
    {
      "text": "你好。",
      "start_time": 640,
      "end_time": 1860,
      "speaker": "0",
      "additions": {"speaker": "0"}
    }
  ],
  "speakers": ["0"],
  "raw": {},
  "error": null
}
```

## Segment Rules

- `start_ms` and `end_ms` are milliseconds.
- `speaker` is a provider-local speaker id as a string.
- `utterances` intentionally mirrors Doubao's shape so existing speaker discovery and voiceprint enrollment can reuse it.
- If a provider has no segment support, return an empty `segments` list and fill `text`.
- New providers should preserve their original response in `raw`.

## Standalone FunASR Server

Run locally:

```bash
uvicorn audio_processing_server:app --host 0.0.0.0 --port 8008
```

OpenAI-compatible transcription:

```bash
curl http://localhost:8008/v1/audio/transcriptions \
  -F file=@meeting.wav \
  -F model=sensevoice \
  -F response_format=verbose_json
```

Vibry standard JSON:

```bash
curl http://localhost:8008/api/asr/transcribe \
  -F audio=@meeting.wav \
  -F model=sensevoice
```

Doubao-compatible JSON:

```bash
curl http://localhost:8008/api/asr/transcribe \
  -F audio=@meeting.wav \
  -F response_format=doubao_json
```
