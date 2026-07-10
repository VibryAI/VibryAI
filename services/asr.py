"""Vibry AI Core — ASR Engine (FunASR local + Doubao cloud) + LLM Summarization"""

import base64, json, logging, os, re, struct, sys, tempfile, threading, time, types, urllib.request, urllib.error, uuid as _uuid
from typing import Optional

from app.config import config
from utils.audio import (
    detect_audio_format, convert_to_wav, enhance_audio,
    save_audio_wav, _clean_debug_dir, compress_for_asr,
)

log = logging.getLogger("vibry.asr")

# ---- HuggingFace mirror ----
os.environ.setdefault("HF_ENDPOINT", config.asr.hf_endpoint)

# ---- Monkeypatch: FunASR doesn't need editdistance ----
if "editdistance" not in sys.modules:
    _ed = types.ModuleType("editdistance")
    _ed.eval = lambda *a, **kw: 0
    sys.modules["editdistance"] = _ed

# ===================================================================
# FunASR Paraformer (lazy load)
# ===================================================================

_paraformer_model = None
_model_lock = threading.Lock()


def get_paraformer():
    """懒加载 FunASR Paraformer 模型（线程安全）"""
    global _paraformer_model
    if _paraformer_model is None:
        with _model_lock:
            if _paraformer_model is None:
                from funasr import AutoModel
                t0 = time.time()
                log.info("📥 加载 Paraformer ASR 模型...")
                _paraformer_model = AutoModel(
                    model="iic/speech_paraformer_asr_nat-zh-cn-16k-common-vocab8358-tensorflow1",
                    disable_update=True,
                )
                log.info(f"✅ 模型加载完成 ({time.time() - t0:.1f}s)")
    return _paraformer_model


# ===================================================================
# 本地 ASR (FunASR)
# ===================================================================

def transcribe_local(audio_bytes: bytes, audio_fmt: str = None) -> str:
    """FunASR Paraformer 本地语音转文字"""
    import soundfile as sf

    # 格式转换
    if audio_fmt and audio_fmt != "wav":
        audio_bytes = convert_to_wav(audio_bytes, audio_fmt)

    # 写临时文件（soundfile 需要文件路径）
    tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
    tmp.write(audio_bytes)
    tmp_path = tmp.name
    tmp.close()

    try:
        audio, sr = sf.read(tmp_path, dtype="float32")
        log.info(f"   📢 soundfile: sr={sr}, samples={len(audio)}, duration={len(audio) / sr:.1f}s")

        model = get_paraformer()
        result = model.generate(input=audio)
        text = result[0]["text"] if result and len(result) > 0 else ""
        return text.strip()
    finally:
        os.unlink(tmp_path)


# ===================================================================
# OGG/Opus 压缩 (移植自 VibryCard)
# ===================================================================

def compress_for_asr(wav_bytes: bytes) -> tuple:
    """WAV → 标准 OGG/Opus (libopus 16kbps)，给豆包 ASR 用

    pnote 的非标 Opus 豆包不认识；ffmpeg 标准 libopus 编码的 OGG 豆包完美支持。
    体积减少约 16 倍，识别准确率不变。

    Returns:
        (compressed_bytes, format, codec) — 失败则返回原 WAV
    """
    ffmpeg = config.audio.ffmpeg_path if hasattr(config, 'audio') else "ffmpeg"
    try:
        proc = subprocess.run(
            [ffmpeg, '-y', '-i', 'pipe:0',
             '-codec:a', 'libopus', '-b:a', '16k', '-ac', '1', '-ar', '16000',
             '-f', 'opus', 'pipe:1'],
            input=wav_bytes, capture_output=True, timeout=120
        )
        if proc.returncode == 0 and len(proc.stdout) > 100:
            ratio = len(proc.stdout) / len(wav_bytes) * 100 if wav_bytes else 0
            log.info(f"   🗜️ OGG/Opus: {len(wav_bytes)//1024}KB→{len(proc.stdout)//1024}KB ({ratio:.0f}%)")
            return (proc.stdout, 'ogg', 'opus')
    except Exception as e:
        log.warning(f"   ⚠️ Opus 编码失败: {e}")
    return (wav_bytes, 'wav', None)  # 失败则原样返回 WAV


# ===================================================================
# 云端 ASR (Doubao) — 极速版 + 标准版
# ===================================================================

def call_cloud_asr_flash(audio_bytes: bytes) -> str:
    """豆包 BigModel ASR (极速版) — 同步提交，返回转写文本"""
    doubao = config.doubao_asr if hasattr(config, 'doubao_asr') else None
    app_id = doubao.app_id if doubao else os.getenv("DOUBAO_ASR_APP_ID", "")
    access_key = doubao.access_key if doubao else os.getenv("DOUBAO_ASR_ACCESS_KEY", "")
    flash_url = doubao.flash_url if doubao else os.getenv("DOUBAO_ASR_FLASH_URL",
        "https://openspeech.bytedance.com/api/v3/auc/bigmodel/recognize/flash")

    audio_b64 = base64.b64encode(audio_bytes).decode('ascii')
    headers = {
        "Content-Type": "application/json",
        "X-Api-App-Key": app_id,
        "X-Api-Access-Key": access_key,
        "X-Api-Resource-Id": "volc.bigasr.auc_turbo",
        "X-Api-Request-Id": str(_uuid.uuid4()),
        "X-Api-Sequence": "-1",
    }
    body = json.dumps({
        "user": {"uid": app_id},
        "audio": {"data": audio_b64},
        "request": {"model_name": "bigmodel"},
    }).encode('utf-8')

    t0 = time.time()
    req = urllib.request.Request(flash_url, data=body, method='POST')
    for k, v in headers.items():
        req.add_header(k, v)
    resp = urllib.request.urlopen(req, timeout=120)
    result = json.loads(resp.read().decode('utf-8'))
    text = result.get('result', {}).get('text', '') if isinstance(result, dict) else ''
    log.info(f"   ☁️ 豆包极速: {len(text)}字符 | {time.time()-t0:.1f}s")
    return text


def call_cloud_asr_standard(audio_bytes: bytes) -> dict:
    """豆包 BigModel ASR (标准版) — 异步提交+轮询
    支持: 说话人分离、方言、多格式音频
    自动将 WAV 压缩为 OGG/Opus 以加速上传

    返回: {"text": "...", "utterances": [...], "speakers": [...]}
    """
    doubao = config.doubao_asr if hasattr(config, 'doubao_asr') else None
    app_id = doubao.app_id if doubao else os.getenv("DOUBAO_ASR_APP_ID", "")
    access_key = doubao.access_key if doubao else os.getenv("DOUBAO_ASR_ACCESS_KEY", "")

    # ★ 压缩为 OGG/Opus 以加速上传
    asr_audio, asr_fmt, asr_codec = compress_for_asr(audio_bytes)
    log.info(f"   ☁️ 标准版ASR ({asr_fmt}: {len(asr_audio)//1024}KB)")

    standard_url = doubao.standard_url if doubao else os.getenv("DOUBAO_ASR_STANDARD_URL",
        "https://openspeech-direct.zijieapi.com/api/v3/auc/bigmodel/submit")

    # ---- Step 1: 提交任务 ----
    task_id = str(_uuid.uuid4())
    audio_b64 = base64.b64encode(audio_bytes).decode('ascii')

    headers = {
        "Content-Type": "application/json",
        "X-Api-App-Key": app_id,
        "X-Api-Access-Key": access_key,
        "X-Api-Resource-Id": "volc.bigasr.auc",
        "X-Api-Request-Id": task_id,
        "X-Api-Sequence": "-1",
    }

    audio_field = {"data": audio_b64, "format": asr_fmt}
    if asr_codec:
        audio_field["codec"] = asr_codec

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
    }).encode('utf-8')

    t0 = time.time()
    req = urllib.request.Request(standard_url, data=body, method='POST')
    for k, v in headers.items():
        req.add_header(k, v)

    try:
        resp = urllib.request.urlopen(req, timeout=60)
        status_code = resp.headers.get('X-Api-Status-Code', '')
        x_tt_logid = resp.headers.get('X-Tt-Logid', '')

        if status_code != '20000000':
            msg = resp.headers.get('X-Api-Message', '未知错误')
            log.error(f"   ☁️ 豆包标准提交失败: code={status_code} msg={msg}")
            return {"text": "", "error": f"提交失败: {msg}"}

        log.info(f"   ☁️ 豆包标准已提交: task={task_id[:8]}... logid={x_tt_logid}")
    except Exception as e:
        log.error(f"   ☁️ 豆包标准提交异常: {e}")
        return {"text": "", "error": f"提交异常: {e}"}

    # ---- Step 2: 轮询结果 ----
    query_url = "https://openspeech-direct.zijieapi.com/api/v3/auc/bigmodel/query"
    query_headers = {
        "Content-Type": "application/json",
        "X-Api-App-Key": app_id,
        "X-Api-Access-Key": access_key,
        "X-Api-Resource-Id": "volc.bigasr.auc",
        "X-Api-Request-Id": task_id,
        "X-Tt-Logid": x_tt_logid,
    }

    max_wait = 120  # 最多等 120 秒
    poll_interval = 2  # 每 2 秒轮询
    elapsed = 0

    while elapsed < max_wait:
        time.sleep(poll_interval)
        elapsed += poll_interval

        try:
            req = urllib.request.Request(query_url, data=b'{}', method='POST')
            for k, v in query_headers.items():
                req.add_header(k, v)
            resp = urllib.request.urlopen(req, timeout=30)
            code = resp.headers.get('X-Api-Status-Code', '')

            if code == '20000000':
                # ★ 完成！
                result = json.loads(resp.read().decode('utf-8'))
                text = result.get('result', {}).get('text', '') if isinstance(result, dict) else ''
                utterances = result.get('result', {}).get('utterances', []) if isinstance(result, dict) else []

                # 提取说话人信息
                speakers = {}
                for utt in utterances:
                    spk = utt.get('speaker', 'unknown')
                    if spk not in speakers:
                        speakers[spk] = []
                    speakers[spk].append(utt.get('text', ''))

                # 如果有多说话人，格式化输出
                if len(speakers) > 1:
                    speaker_text = []
                    for spk, texts in speakers.items():
                        speaker_text.append(f"[{spk}]: {' '.join(texts)}")
                    formatted_text = '\n'.join(speaker_text)
                else:
                    formatted_text = text

                log.info(f"   ☁️ 豆包标准完成: {len(text)}字符, {len(utterances)}语音段, {len(speakers)}说话人 | {time.time()-t0:.1f}s")
                return {
                    "text": formatted_text,
                    "text_raw": text,
                    "utterances": utterances,
                    "speakers": list(speakers.keys()),
                }
            elif code == '20000001' or code == '20000002':
                # 还在处理中，继续等待
                if elapsed % 10 == 0:
                    log.info(f"   ☁️ 豆包标准等待中... ({elapsed}s)")
                continue
            else:
                msg = resp.headers.get('X-Api-Message', '未知错误')
                log.error(f"   ☁️ 豆包标准查询失败: code={code} msg={msg}")
                return {"text": "", "error": f"查询失败: {msg}"}

        except Exception as e:
            log.error(f"   ☁️ 豆包标准轮询异常: {e}")
            if elapsed >= max_wait:
                return {"text": "", "error": f"轮询超时: {e}"}

    log.error(f"   ☁️ 豆包标准超时 ({max_wait}s)")
    return {"text": "", "error": f"超时({max_wait}s)，请稍后重试"}


# ===================================================================
# 统一转写接口
# ===================================================================

def transcribe(audio_bytes: bytes, title: str = "", user_id: str = "anonymous") -> dict:
    """统一转写入口

    根据 config.asr.mode 选择模式：
    - local: FunASR Paraformer 本地模型
    - cloud / cloud_flash: 豆包极速版 ASR
    - cloud_standard: 豆包标准版 ASR（说话人分离）

    Returns:
        {"text": str, "audio_url": str|None, "audio_token": str|None, "error": str|None}
    """
    import db  # lazy import to avoid circular

    size_kb = len(audio_bytes) / 1024
    asr_mode = config.asr.mode

    # 检测音频格式
    audio_fmt = detect_audio_format(audio_bytes)
    if audio_fmt != "wav":
        log.info(f"📦 检测到 {audio_fmt} 格式, 转换为 WAV...")
        audio_bytes = convert_to_wav(audio_bytes, audio_fmt)

    log.info(f"🎤 ASR开始 [user={user_id}] | 音频{size_kb:.1f}KB | 格式={audio_fmt} | mode={asr_mode}")

    # ★ 去重缓存：已完成的录音直接返回缓存结果（移植自 VibryCard）
    if title:
        rec_id = db.generate_id(title)
        cached = db.get_recording(rec_id)
        if cached and cached.get('status') == 'completed' and cached.get('transcript', '').strip():
            log.info(f"   ♻️ 命中缓存: {rec_id} ({len(cached['transcript'])}字)")
            return {
                "text": cached['transcript'],
                "audio_url": f"/api/audio/{rec_id}",
                "audio_token": cached.get('audio_token', ''),
                "error": None,
            }

    # ★ 保存到 debug/ 目录
    debug_dir = config.audio.debug_dir if hasattr(config, 'audio') else "debug"
    dbg_stem = title.replace('.opus', '').replace('.wav', '') if title else f"req_{user_id}"
    dbg_path = os.path.join(debug_dir, f"{dbg_stem}.wav")
    os.makedirs(debug_dir, exist_ok=True)
    with open(dbg_path, 'wb') as df:
        df.write(audio_bytes)
    _clean_debug_dir()

    size_bytes = len(audio_bytes)

    # ★ 计算音频特征
    if len(audio_bytes) > 44:
        dur_sec = (size_bytes - 44) / (16000 * 2)
        sample_count = min(int(dur_sec * 16000), 48000) if dur_sec > 0 else 0
        if sample_count > 0:
            max_amp = 0
            for i in range(44, min(44 + sample_count * 2, size_bytes - 1), 2):
                v = abs(_struct.unpack('<h', audio_bytes[i:i+2])[0])
                if v > max_amp:
                    max_amp = v
            audio_quality = f"max_amp={max_amp}" if max_amp > 500 else "⚠️疑似静音"
        else:
            audio_quality = "N/A"
        log.info(f"   🔄 音频: {size_bytes/1024:.0f}KB WAV, {dur_sec:.0f}s, {audio_quality}")
    else:
        log.warning(f"   ⚠️ WAV太小: {size_bytes}B (可能格式错误)")

    t0 = time.time()
    utterances_data = None
    try:
        utterances_data = None
        if asr_mode == "cloud_standard":
            log.info(f"☁️ 使用豆包标准版 ASR（说话人分离）")
            result = call_cloud_asr_standard(audio_bytes)
            text = result.get("text", "")
            if result.get("utterances"):
                utterances_data = result.get("utterances")
                log.info(f"   🗣️ 说话人: {result.get('speakers', [])}")
                # ★ 声纹识别：将 [发言人0] 替换为已注册声纹的真实姓名
                try:
                    from services.voiceprint import apply_voiceprint_to_transcript
                    text = apply_voiceprint_to_transcript(text, utterances_data, audio_bytes)
                except Exception as e:
                    log.warning(f"   ⚠️ 声纹识别跳过: {e}")
        elif asr_mode in ("cloud", "cloud_flash"):
            log.info(f"☁️ 使用豆包极速版 ASR")
            text = call_cloud_asr_flash(audio_bytes)
        else:
            text = transcribe_local(audio_bytes, "wav")

        asr_time = time.time() - t0
        log.info(f"✅ ASR完成 | {asr_time:.1f}s | {len(text)}字符")

        # ★ 0字 = 无声/无效录音，返回失败
        if not text.strip():
            if title:
                rec_id = db.generate_id(title)
                db.upsert_recording(rec_id, user_id=user_id, title=title, filename=title, status="failed")
            return {"text": "", "audio_url": None, "audio_token": None, "error": "未识别到有效语音"}

        # ★ 存入数据库 + 清晰化保存WAV + 生成token
        audio_url = None
        audio_token_val = None
        if title:
            rec_id = db.generate_id(title)
            # 保存原始 WAV（用于后续声纹切片，移植自 VibryCard）
            audio_dir = config.audio.audio_dir if hasattr(config, 'audio') else "audio"
            raw_wav_filename = f"{rec_id}_raw.wav"
            raw_wav_path = os.path.join(audio_dir, raw_wav_filename)
            with open(raw_wav_path, 'wb') as rf:
                rf.write(audio_bytes)

            # 清晰化处理 + 保存播放WAV
            enh_t0 = time.time()
            enhanced_wav = enhance_audio(audio_bytes)
            audio_rel_path = save_audio_wav(enhanced_wav, rec_id)
            audio_token_val = db.generate_token()
            enh_time = time.time() - enh_t0
            log.info(f"   🎧 音频清晰化+保存: {audio_rel_path} ({enh_time:.1f}s)")

            db.upsert_recording(
                rec_id,
                user_id=user_id,
                title=title,
                filename=title,
                file_size=size_kb * 1024,
                transcript=text,
                transcript_chars=len(text),
                status="transcribing",
                audio_path=audio_rel_path,
                audio_token=audio_token_val,
                utterances_json=json.dumps(utterances_data, ensure_ascii=False) if utterances_data else '',
                raw_wav_path=f"audio/{raw_wav_filename}",
            )
            db.log_analysis(
                rec_id, "transcribe", "success",
                user_id=user_id,
                input_size=size_kb * 1024,
                output_chars=len(text),
                duration_ms=int(asr_time * 1000),
            )
            audio_url = f"/api/audio/{rec_id}"

        return {"text": text, "audio_url": audio_url, "audio_token": audio_token_val, "error": None}

    except Exception as e:
        import traceback
        log.error(f"❌ ASR失败: {e}\n{traceback.format_exc()}")
        if title:
            rec_id = db.generate_id(title)
            db.upsert_recording(rec_id, user_id=user_id, title=title, filename=title, status="failed")
            db.log_analysis(rec_id, "transcribe", "error", user_id=user_id, error_msg=str(e)[:500])
        return {"text": "", "audio_url": None, "audio_token": None, "error": str(e)}


# ===================================================================
# 语音聊天转写（始终用极速版）
# ===================================================================

def transcribe_voice(audio_bytes: bytes, title: str = "", user_id: str = "anonymous") -> dict:
    """语音聊天实时转写 — 始终使用豆包极速版 ASR

    与 transcribe() 的区别:
    - 固定使用极速版（cloud_flash），同步返回，低延迟
    - 不做说话人分离
    - 不做音频清晰化保存（聊天场景不需要）
    - 不写入数据库录音记录（聊天语音是临时的）

    Returns:
        {"text": str, "audio_url": None, "audio_token": None, "error": str|None}
    """
    import db

    size_kb = len(audio_bytes) / 1024

    # 检测并转换音频格式
    audio_fmt = detect_audio_format(audio_bytes)
    if audio_fmt != "wav":
        log.info(f"📦 语音ASR: 检测到 {audio_fmt} 格式, 转换为 WAV...")
        audio_bytes = convert_to_wav(audio_bytes, audio_fmt)

    log.info(f"🎤 语音聊天ASR [user={user_id}] | {size_kb:.1f}KB | 极速版")

    t0 = time.time()
    try:
        text = call_cloud_asr_flash(audio_bytes)
        asr_time = time.time() - t0
        log.info(f"✅ 语音ASR完成 | {asr_time:.1f}s | {len(text)}字符")

        if not text.strip():
            return {"text": "", "audio_url": None, "audio_token": None, "error": "未识别到有效语音"}

        return {"text": text, "audio_url": None, "audio_token": None, "error": None}

    except Exception as e:
        import traceback
        log.error(f"❌ 语音ASR失败: {e}\n{traceback.format_exc()}")
        return {"text": "", "audio_url": None, "audio_token": None, "error": str(e)}


# ===================================================================
# LLM 调用（用于摘要）
# ===================================================================

def call_llm(model: str, messages: list[dict], max_time: int = 180) -> dict:
    """调用上游 LLM（同步，用于摘要）"""
    llm_cfg = config.upstream
    url = f"{llm_cfg.base_url}/chat/completions"
    payload = {"model": model, "messages": messages}
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json; charset=utf-8")
    req.add_header("Authorization", f"Bearer {llm_cfg.api_key}")
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
    """从 LLM 返回中提取 JSON"""
    json_str = raw
    match = re.search(r"\{[\s\S]*\}", raw)
    if match:
        json_str = match.group()
    try:
        return json.loads(json_str)
    except json.JSONDecodeError:
        return {
            "current_intent": "",
            "key_decisions": [],
            "action_items": [],
            "memory_conflict": "",
            "proactive_next": "",
            "tags": [],
            "detailed_summary": raw,
            "full_summary": raw,
        }


def summarize(
    transcript: str,
    title: str = "录音",
    context: str = "",
    user_id: str = "anonymous",
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
    llm_cfg = config.upstream

    model = sum_cfg.model or llm_cfg.model
    char_count = len(transcript)

    log.info(f"🧠 纪要开始 [user={user_id}] | 标题:{title} | {char_count}字符 | model={model}")

    # 构建 system prompt
    system_prompt = sum_cfg.system_prompt + "\n\n" + sum_cfg.user_profile_text

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": f"录音标题：{title}\n\n转写内容：\n{transcript}\n\n额外上下文：{context}"},
    ]

    t0 = time.time()
    result = call_llm(model, messages, max_time=180)
    api_time = time.time() - t0

    if "error" in result:
        log.error(f"❌ 纪要失败 ({api_time:.1f}s): {result['error']}")
        rec_id = db.generate_id(title)
        db.upsert_recording(rec_id, user_id=user_id, title=title, filename=title, status="failed")
        db.log_analysis(rec_id, "summarize", "error", user_id=user_id,
                        input_size=char_count, error_msg=str(result["error"])[:500])
        return {"error": str(result["error"])}

    raw = result.get("choices", [{}])[0].get("message", {}).get("content", "")
    parsed = parse_summary(raw)

    tokens = result.get("usage", {}).get("total_tokens", "?")
    decisions = len(parsed.get("key_decisions", []))
    log.info(f"✅ 纪要完成 | API{api_time:.1f}s | tokens={tokens} | {decisions}决策")

    # 存入数据库
    rec_id = db.generate_id(title)
    tags = parsed.get("tags", [])
    db.upsert_recording(
        rec_id,
        user_id=user_id,
        title=title,
        filename=title,
        status="completed",
        summary_json=json.dumps(parsed, ensure_ascii=False),
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

    return parsed
