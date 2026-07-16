"""Unified ASR provider contract for VibryServer.

The internal shape is intentionally provider-neutral. Provider adapters can
still expose OpenAI-compatible or Doubao-compatible views at the API edge.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any


def _clean_text(text: str) -> str:
    """Remove tags emitted by SenseVoice/FunASR while keeping readable text."""
    return re.sub(r"<\|[^|]*\|>", "", text or "").strip()


@dataclass
class AsrSegment:
    id: int
    start_ms: int = 0
    end_ms: int = 0
    text: str = ""
    speaker: str | None = None
    confidence: float | None = None
    words: list[dict[str, Any]] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_doubao_utterance(cls, index: int, utterance: dict[str, Any]) -> "AsrSegment":
        additions = utterance.get("additions", {}) or {}
        speaker = utterance.get("speaker", additions.get("speaker"))
        return cls(
            id=index,
            start_ms=int(utterance.get("start_time", utterance.get("start", 0)) or 0),
            end_ms=int(utterance.get("end_time", utterance.get("end", 0)) or 0),
            text=_clean_text(utterance.get("text", "")),
            speaker=str(speaker) if speaker is not None and str(speaker) != "?" else None,
            confidence=_safe_float(utterance.get("confidence")),
            raw=utterance,
        )

    @classmethod
    def from_openai_segment(cls, index: int, segment: dict[str, Any]) -> "AsrSegment":
        speaker = segment.get("speaker")
        return cls(
            id=index,
            start_ms=int(float(segment.get("start", 0) or 0) * 1000),
            end_ms=int(float(segment.get("end", 0) or 0) * 1000),
            text=_clean_text(segment.get("text", "")),
            speaker=str(speaker) if speaker is not None else None,
            confidence=_safe_float(segment.get("confidence")),
            raw=segment,
        )

    @classmethod
    def from_funasr_sentence(cls, index: int, segment: dict[str, Any]) -> "AsrSegment":
        speaker = segment.get("spk", segment.get("speaker"))
        text = segment.get("sentence", segment.get("text", ""))
        return cls(
            id=index,
            start_ms=int(segment.get("start", 0) or 0),
            end_ms=int(segment.get("end", 0) or 0),
            text=_clean_text(text),
            speaker=str(speaker) if speaker is not None else None,
            confidence=_safe_float(segment.get("confidence")),
            raw=segment,
        )

    def to_dict(self) -> dict[str, Any]:
        data = {
            "id": self.id,
            "start_ms": self.start_ms,
            "end_ms": self.end_ms,
            "text": self.text,
            "speaker": self.speaker,
            "confidence": self.confidence,
        }
        if self.words:
            data["words"] = self.words
        return data

    def to_openai_segment(self) -> dict[str, Any]:
        data = {
            "id": self.id,
            "start": round(self.start_ms / 1000, 3),
            "end": round(self.end_ms / 1000, 3),
            "text": self.text,
        }
        if self.speaker is not None:
            data["speaker"] = self.speaker
        return data

    def to_doubao_utterance(self) -> dict[str, Any]:
        data = {
            "text": self.text,
            "start_time": self.start_ms,
            "end_time": self.end_ms,
            "additions": {},
        }
        if self.speaker is not None:
            data["additions"]["speaker"] = str(self.speaker)
            data["speaker"] = str(self.speaker)
        if self.confidence is not None:
            data["confidence"] = self.confidence
        return data


@dataclass
class AsrResult:
    provider: str
    model: str = ""
    text: str = ""
    language: str = "auto"
    duration_ms: int | None = None
    segments: list[AsrSegment] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)
    error: str | None = None

    @property
    def speakers(self) -> list[str]:
        seen: list[str] = []
        for seg in self.segments:
            if seg.speaker is not None and seg.speaker not in seen:
                seen.append(seg.speaker)
        return seen

    @property
    def utterances(self) -> list[dict[str, Any]]:
        return [seg.to_doubao_utterance() for seg in self.segments if seg.text.strip()]

    def formatted_text(self) -> str:
        speaker_segments = [seg for seg in self.segments if seg.speaker is not None and seg.text.strip()]
        if not speaker_segments:
            return self.text.strip()
        label = "\u53d1\u8a00\u4eba"
        return "\n".join(f"[{label}{seg.speaker}] {seg.text}" for seg in speaker_segments)

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "model": self.model,
            "text": self.text,
            "language": self.language,
            "duration_ms": self.duration_ms,
            "segments": [seg.to_dict() for seg in self.segments],
            "utterances": self.utterances,
            "speakers": self.speakers,
            "raw": self.raw,
            "error": self.error,
        }

    def to_openai(self, verbose: bool = True) -> dict[str, Any]:
        payload = {"text": self.text}
        if verbose:
            payload.update({
                "segments": [seg.to_openai_segment() for seg in self.segments],
                "language": self.language,
                "duration": round((self.duration_ms or 0) / 1000, 3),
                "model": self.model,
                "provider": self.provider,
            })
        return payload

    def to_doubao_compatible(self) -> dict[str, Any]:
        return {
            "result": {
                "text": self.text,
                "utterances": self.utterances,
                "additions": {
                    "provider": self.provider,
                    "model": self.model,
                    "language": self.language,
                    "speakers": self.speakers,
                },
            }
        }


def build_result_from_text(provider: str, text: str, model: str = "", raw: dict[str, Any] | None = None) -> AsrResult:
    clean = _clean_text(text)
    return AsrResult(provider=provider, model=model, text=clean, raw=raw or {})


def _safe_float(value: Any) -> float | None:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None
