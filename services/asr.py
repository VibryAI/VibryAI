"""Vibry AI Core — ASR Engine + LLM Summarization

Provider-based transcription (FunASR / Doubao / Whisper / OpenAI-compatible)
plus meeting summary generation via upstream LLM.

Public API:
- transcribe(audio_bytes, title, user_id) → dict
- transcribe_voice(audio_bytes, title, user_id) → dict
- summarize(transcript, title, context, user_id) → dict
- call_llm(model, messages, max_time) → dict
"""

import json
import logging
import os
import re
import sys
import time
import types
import urllib.error
import urllib.request

from app.config import config
from utils.audio import (
    detect_audio_format, enhance_audio,
    save_audio_wav, _clean_debug_dir, get_audio_duration_seconds,
)
from services.asr_providers import get_asr_provider

log = logging.getLogger("vibry.asr")

# ---- HuggingFace mirror ----
os.environ.setdefault("HF_ENDPOINT", config.asr.hf_endpoint)

# ---- Monkeypatch: FunASR doesn't need editdistance ----
if "editdistance" not in sys.modules:
    _ed = types.ModuleType("editdistance")
    _ed.eval = lambda *a, **kw: 0
    sys.modules["editdistance"] = _ed


# ===================================================================
# 统一转写接口
# ===================================================================

def transcribe(audio_bytes: bytes, title: str = "", user_id: str = "anonymous", category: str = "") -> dict:
    """Provider-based transcription entrypoint.

    Args:
        audio_bytes: Raw audio data (WAV/Opus/MP3 etc.)
        title: Recording title (used for DB record ID + cache key)
        user_id: User identifier for multi-tenant isolation
        category: Optional category for the recording (e.g. "会议", "通话")

    Returns:
        {"text", "audio_url", "audio_token", "recording_id",
         "provider", "model", "segments", "utterances", "speakers", "error"}
    """
    import db

    size_kb = len(audio_bytes) / 1024
    asr_mode = config.asr.mode
    audio_fmt = detect_audio_format(audio_bytes)
    log.info("ASR start user=%s size=%.1fKB fmt=%s provider=%s", user_id, size_kb, audio_fmt, asr_mode)

    # ★ 去重缓存：已完成的录音直接返回缓存结果
    if title:
        rec_id = db.generate_id(title)
        cached = db.get_recording(rec_id)
        if cached and cached.get("status") == "completed" and cached.get("transcript", "").strip():
            return {
                "text": cached["transcript"],
                "audio_url": f"/api/audio/{rec_id}",
                "audio_token": cached.get("audio_token", ""),
                "recording_id": rec_id,
                "provider": asr_mode,
                "error": None,
            }

    # ★ 保存到 debug/ 目录
    debug_dir = config.audio.debug_dir if hasattr(config, "audio") else "debug"
    dbg_stem = title.replace(".opus", "").replace(".wav", "") if title else f"req_{user_id}"
    os.makedirs(debug_dir, exist_ok=True)
    with open(os.path.join(debug_dir, f"{dbg_stem}.wav"), "wb") as df:
        df.write(audio_bytes)
    _clean_debug_dir()

    t0 = time.time()
    rec_id = None
    try:
        provider = get_asr_provider(asr_mode)
        result = provider.transcribe(audio_bytes, audio_fmt=audio_fmt)
        text = result.text.strip()
        utterances_data = result.utterances

        # ★ 声纹识别：将 [发言人X] 替换为已注册声纹的真实姓名
        if utterances_data:
            try:
                from services.voiceprint import apply_voiceprint_to_transcript
                text = apply_voiceprint_to_transcript(result.formatted_text() or text, utterances_data, audio_bytes)
            except Exception as e:
                log.warning("voiceprint apply skipped: %s", e)

        asr_time = time.time() - t0
        log.info("ASR done provider=%s model=%s chars=%d time=%.1fs", result.provider, result.model, len(text), asr_time)

        # ★ 0字 = 无声/无效录音
        if not text.strip():
            if title:
                rec_id = db.generate_id(title)
                db.upsert_recording(rec_id, user_id=user_id, title=title, filename=title, status="failed")
            return {"text": "", "audio_url": None, "audio_token": None, "error": "No valid speech recognized"}

        # ★ 存入数据库 + 清晰化保存WAV + 生成token
        audio_url = None
        audio_token_val = None
        if title:
            rec_id = db.generate_id(title)
            audio_dir = config.audio.audio_dir if hasattr(config, "audio") else "audio"
            raw_wav_filename = f"{rec_id}_raw.wav"
            raw_wav_path = os.path.join(audio_dir, raw_wav_filename)
            with open(raw_wav_path, "wb") as rf:
                rf.write(audio_bytes)

            enhanced_wav = enhance_audio(audio_bytes)
            audio_rel_path = save_audio_wav(enhanced_wav, rec_id)
            audio_token_val = db.generate_token()
            db.upsert_recording(
                rec_id,
                user_id=user_id,
                title=title,
                filename=title,
                file_size=len(audio_bytes),
                transcript=text,
                transcript_chars=len(text),
                status="transcribing",
                category=category or "未分类",
                audio_path=audio_rel_path,
                audio_token=audio_token_val,
                utterances_json=json.dumps(utterances_data, ensure_ascii=False) if utterances_data else "",
                raw_wav_path=f"audio/{raw_wav_filename}",
            )
            db.log_analysis(
                rec_id,
                "transcribe",
                "success",
                user_id=user_id,
                input_size=len(audio_bytes),
                output_chars=len(text),
                duration_ms=int(asr_time * 1000),
            )
            # ★ 计费：ASR 按音频时长计费
            audio_dur = get_audio_duration_seconds(audio_bytes)
            db.log_usage(
                user_id=user_id,
                endpoint="/api/transcribe",
                model=result.provider,
                audio_seconds=audio_dur,
                duration_ms=int(asr_time * 1000),
            )
            # ★ 更新录音时长（upsert_recording 已写入基础信息，这里补 duration_sec）
            if audio_dur > 0:
                db.upsert_recording(rec_id, duration_sec=round(audio_dur, 1))
            audio_url = f"/api/audio/{rec_id}"

        return {
            "text": text,
            "audio_url": audio_url,
            "audio_token": audio_token_val,
            "recording_id": rec_id,
            "provider": result.provider,
            "model": result.model,
            "segments": [seg.to_dict() for seg in result.segments],
            "utterances": utterances_data,
            "speakers": result.speakers,
            "error": None,
        }
    except Exception as e:
        import traceback
        log.error("ASR failed: %s\n%s", e, traceback.format_exc())
        if title:
            rec_id = db.generate_id(title)
            db.upsert_recording(rec_id, user_id=user_id, title=title, filename=title, status="failed")
            db.log_analysis(rec_id, "transcribe", "error", user_id=user_id, error_msg=str(e)[:500])
        return {"text": "", "audio_url": None, "audio_token": None, "recording_id": rec_id, "error": str(e)}


# ===================================================================
# 语音聊天转写（始终用极速版）
# ===================================================================

def transcribe_voice(audio_bytes: bytes, title: str = "", user_id: str = "anonymous") -> dict:
    """Realtime voice transcription using the configured voice ASR provider.

    与 transcribe() 的区别:
    - 固定使用 voice_mode 配置（默认极速版），低延迟
    - 不做说话人分离、不做音频清晰化保存、不写录音记录

    Returns:
        {"text", "provider", "model", "segments", "error"}
    """
    audio_fmt = detect_audio_format(audio_bytes)
    mode = getattr(config.doubao_asr, "voice_mode", "doubao_flash") or "doubao_flash"
    t0 = time.time()
    import db  # lazy import to avoid circular
    try:
        provider = get_asr_provider(mode)
        result = provider.transcribe(audio_bytes, audio_fmt=audio_fmt)
        text = result.text.strip()
        log.info("voice ASR done provider=%s chars=%d time=%.1fs", result.provider, len(text), time.time() - t0)
        if not text:
            return {"text": "", "audio_url": None, "audio_token": None, "error": "No valid speech recognized"}
        # ★ 计费：ASR 按音频时长计费
        audio_dur = get_audio_duration_seconds(audio_bytes)
        db.log_usage(
            user_id=user_id,
            endpoint="/api/transcribe/voice",
            model=result.provider,
            audio_seconds=audio_dur,
            duration_ms=int((time.time() - t0) * 1000),
        )
        return {
            "text": text,
            "audio_url": None,
            "audio_token": None,
            "provider": result.provider,
            "model": result.model,
            "segments": [seg.to_dict() for seg in result.segments],
            "error": None,
        }
    except Exception as e:
        import traceback
        log.error("voice ASR failed: %s\n%s", e, traceback.format_exc())
        return {"text": "", "audio_url": None, "audio_token": None, "error": str(e)}


# ===================================================================
# LLM 调用（用于摘要/洞察）
# ===================================================================

def call_llm(model: str, messages: list[dict], max_time: int = 180) -> dict:
    """调用上游 LLM（同步，用于摘要）

    注意: 此函数调用 /chat/completions 端点，用于生成文本。
    不要用于获取 embedding 向量——请使用 services.embedder.VolcengineEmbedder。
    """
    chat_cfg = config.chat
    url = f"{chat_cfg.base_url.rstrip('/')}/chat/completions"
    payload = {"model": model, "messages": messages}
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json; charset=utf-8")
    req.add_header("Authorization", f"Bearer {chat_cfg.api_key}")
    try:
        with urllib.request.urlopen(req, timeout=max_time) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return {"error": f"HTTP {e.code}: {e.read().decode('utf-8')}"}
    except Exception as e:
        return {"error": str(e)}


# ===================================================================
# 会议纪要
# ===================================================================

def parse_summary(raw: str) -> dict:
    """Build the legacy structured view from Markdown without trusting model JSON."""
    from services.markdown_content import parse_summary_markdown

    return parse_summary_markdown(raw)


def summarize(
    transcript: str,
    title: str = "录音",
    context: str = "",
    user_id: str = "anonymous",
    persist_recording: bool = True,
) -> dict:
    """生成会议纪要

    Args:
        transcript: 转写文本
        title: 录音标题
        context: 额外上下文
        user_id: 用户标识

    Returns:
        结构化纪要 dict (AiSummaryResult 兼容)
    """
    import db

    sum_cfg = config.summary

    model = sum_cfg.effective_model
    char_count = len(transcript)

    log.info(f"🧠 纪要开始 [user={user_id}] | 标题:{title} | {char_count}字符 | model={model}")

    # 构建 system prompt
    output_contract = """\
【最终输出协议：本段优先于前文中任何冲突要求】
- 最终响应只能是纯 Markdown 正文；禁止 JSON、代码块、HTML、开场说明和结尾解释。
- 以“# 录音纪要”为唯一一级标题。
- 使用“## 基本信息”“## 核心目的”“## 会议主要内容”等章节；具体内容主题使用三级标题。
- “关键决定”“行动项”“标签”没有可靠内容时，必须省略整个章节，不得输出空字段或占位文字。
- 不得推测时间、时长、负责人、决定、行动项、结论、数据、比例或因果关系。
- 客观完整地保留实质内容；识别不清的原文标注为【录音模糊】。"""
    system_prompt = (
        sum_cfg.system_prompt + "\n\n" + sum_cfg.user_profile_text + "\n\n" + output_contract
    )

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": f"录音标题：{title}\n\n转写内容：\n{transcript}\n\n额外上下文：{context}"},
    ]

    t0 = time.time()
    result = call_llm(model, messages, max_time=180)
    api_time = time.time() - t0

    if "error" in result:
        log.error(f"❌ 纪要失败 ({api_time:.1f}s): {result['error']}")
        if persist_recording:
            rec_id = db.generate_id(title)
            db.upsert_recording(
                rec_id, user_id=user_id, title=title, filename=title, status="failed",
            )
            db.log_analysis(
                rec_id, "summarize", "error", user_id=user_id,
                input_size=char_count, error_msg=str(result["error"])[:500],
            )
        return {"error": str(result["error"])}

    raw = result.get("choices", [{}])[0].get("message", {}).get("content", "")
    parsed = parse_summary(raw)
    summary_markdown = str(parsed.get("markdown") or raw).strip()
    parsed["markdown"] = summary_markdown

    tokens = result.get("usage", {}).get("total_tokens", "?")
    decisions = len(parsed.get("key_decisions", []))
    log.info(f"✅ 纪要完成 | API{api_time:.1f}s | tokens={tokens} | {decisions}决策")

    if persist_recording:
        rec_id = db.generate_id(title)
        tags = parsed.get("tags", [])
        db.upsert_recording(
            rec_id,
            user_id=user_id,
            title=title,
            filename=title,
            status="completed",
            summary_json=json.dumps(parsed, ensure_ascii=False),
            summary_markdown=summary_markdown,
            tags=json.dumps(tags, ensure_ascii=False),
            transcript_chars=char_count,
        )
        db.log_analysis(
            rec_id, "summarize", "success",
            user_id=user_id,
            input_size=char_count,
            output_chars=len(json.dumps(parsed, ensure_ascii=False)),
            duration_ms=int(api_time * 1000),
        )
    # ★ 计费：LLM 按 token 计费
    usage = result.get("usage", {})
    db.log_usage(
        user_id=user_id,
        endpoint="/api/summarize",
        model=model,
        prompt_tokens=usage.get("prompt_tokens", 0),
        completion_tokens=usage.get("completion_tokens", 0),
        total_tokens=usage.get("total_tokens", 0),
        duration_ms=int(api_time * 1000),
    )

    return parsed
