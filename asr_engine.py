"""Vibry AI Core — ASR 语音识别引擎

支持多种模式:
- local: FunASR Paraformer 本地模型（离线，无需 API）
- cloud / cloud_flash: Doubao 极速版 ASR
- cloud_standard: Doubao 标准版 ASR（说话人分离 + 方言）

音频格式: WAV, Opus/OGG, MP3, FLAC（自动检测 + ffmpeg 转换）
"""

import base64
import json
import logging
import os
import re
import struct as _struct
import subprocess
import sys
import tempfile
import threading
import time
import types
import urllib.request
import urllib.error
import uuid as _uuid
from typing import Optional

from config import config

log = logging.getLogger("vibry.asr")

# ---- HuggingFace 镜像 ----
os.environ.setdefault("HF_ENDPOINT", config.asr.hf_endpoint)

# ---- Monkeypatch: FunASR 不需要 editdistance ----
if "editdistance" not in sys.modules:
    _ed = types.ModuleType("editdistance")
    _ed.eval = lambda *a, **kw: 0
    sys.modules["editdistance"] = _ed

# ---- 音频 magic bytes 检测 ----
AUDIO_MAGIC = {
    b"RIFF": "wav",
    b"OggS": "opus",
    b"\xff\xfb": "mp3",
    b"\xff\xf3": "mp3",
    b"\xff\xfa": "mp3",
    b"ID3": "mp3",
    b"fLaC": "flac",
}


def detect_audio_format(data: bytes) -> str:
    """根据文件头检测音频格式"""
    for magic, fmt in AUDIO_MAGIC.items():
        if data[: len(magic)] == magic:
            return fmt
    if len(data) > 0:
        toc = data[0]
        if (toc & 0xFC) == 0xFC:
            return "opus_raw"
    return "wav"


def _is_opus_data(data: bytes) -> bool:
    """检测数据是否为裸 Opus 帧流（非 OGG 容器，非 WAV）

    pnote 格式可能有 4 字节头 0x4b41XXXX，需要剥离后检测。
    Opus TOC 字节: bit7-3=配置(0-31), bit2=立体声, bit1-0=帧数代码
    有效配置范围: 0-19（SILK NB/WB/UWB + Hybrid + CELT + CELT-only）
    """
    if len(data) < 4:
        return False
    # RIFF = WAV, OggS = OGG 容器 → 不是裸 Opus
    if data[:4] == b'RIFF' or data[:4] == b'OggS':
        return False
    # pnote 4 字节头 0x4b41XXXX → 剥掉后检查 TOC
    check_data = data[4:] if data[:2] == b'KA' and len(data) > 4 else data
    if len(check_data) < 1:
        return False
    toc = check_data[0]
    config_bits = (toc >> 3) & 0x1F  # bits 7-3
    # Opus spec: config 0-19 are valid
    return 0 <= config_bits <= 19


def convert_to_wav(input_data: bytes, source_fmt: str = None) -> bytes:
    """ffmpeg 转换音频为 WAV (16kHz mono 16bit PCM)

    自动检测格式：
    - WAV (RIFF header) → ffmpeg 重采样
    - 裸 Opus 帧流（含 pnote 4b41 头） → OGG 封装后 ffmpeg 解码
    - 其他 → 尝试 ffmpeg 自动探测
    """
    ffmpeg = config.audio.ffmpeg_path if hasattr(config, 'audio') else "ffmpeg"

    if source_fmt is None:
        source_fmt = detect_audio_format(input_data)

    # ★ 检测裸 Opus 帧流 → OGG 封装后 ffmpeg 解码
    if _is_opus_data(input_data):
        # pnote 录音卡在 Opus 数据前加 4 字节头 0x4b41XXXX，必须剥掉
        if input_data[:2] == b'KA':
            opus_data = input_data[4:]  # 剥掉 4 字节 4b41 头
            log.info(f"   🎵 检测到 pnote Opus (4b41头+{len(input_data)}B)，剥头后{len(opus_data)}B")
        else:
            opus_data = input_data
            log.info(f"   🎵 检测到裸 Opus 帧流 ({len(opus_data)} bytes)")

        try:
            from ogg_opus_muxer import raw_opus_to_ogg
            ogg_data = raw_opus_to_ogg(opus_data, frame_size=40)

            # 写临时 OGG 文件供 ffmpeg 解码
            tmp_ogg = os.path.join(tempfile.gettempdir(), f"vibry_opus_{os.getpid()}.ogg")
            with open(tmp_ogg, 'wb') as f:
                f.write(ogg_data)

            proc = subprocess.run(
                [ffmpeg, '-y', '-i', tmp_ogg,
                 '-ar', '16000', '-ac', '1', '-sample_fmt', 's16',
                 '-f', 'wav', 'pipe:1'],
                capture_output=True, timeout=60
            )
            os.unlink(tmp_ogg)

            if proc.returncode == 0 and len(proc.stdout) > 44:
                dur = (len(proc.stdout) - 44) / (16000 * 2)
                log.info(f"   ✅ Opus→WAV 完成: {len(proc.stdout)} bytes, {dur:.1f}s")
                return proc.stdout
            else:
                err = proc.stderr.decode('utf-8', errors='replace')[-300:]
                log.warning(f"   ⚠️ OGG/Opus ffmpeg 解码失败: {err}")
                return input_data
        except Exception as e:
            log.warning(f"   ⚠️ Opus 解码异常: {e}")
            return input_data

    # ★ 标准 WAV → ffmpeg 规范化
    if source_fmt in ("wav", "opus_raw"):
        try:
            proc = subprocess.run(
                [ffmpeg, '-y', '-i', 'pipe:0',
                 '-ar', '16000', '-ac', '1', '-sample_fmt', 's16',
                 '-f', 'wav', 'pipe:1'],
                input=input_data, capture_output=True, timeout=60
            )
            if proc.returncode == 0 and len(proc.stdout) > 44:
                return proc.stdout
            else:
                err = proc.stderr.decode('utf-8', errors='replace')[-300:]
                log.warning(f"   ⚠️ ffmpeg 转换失败 (rc={proc.returncode}): {err}")
                if input_data[:4] == b'RIFF':
                    log.info(f"   ℹ️ WAV 头有效但 ffmpeg 失败，原样返回")
                return input_data
        except Exception as e:
            log.warning(f"   ⚠️ ffmpeg 异常: {e}")
            return input_data

    # 其他格式 → ffmpeg 自动探测
    try:
        proc = subprocess.run(
            [ffmpeg, "-y", "-f", source_fmt, "-i", "pipe:0",
             "-ar", "16000", "-ac", "1", "-sample_fmt", "s16",
             "-f", "wav", "pipe:1"],
            input=input_data,
            capture_output=True,
            timeout=30,
        )
        if proc.returncode == 0 and len(proc.stdout) > 44:
            log.info(f"   🔄 ffmpeg: {source_fmt} → WAV ({len(input_data)}→{len(proc.stdout)} bytes)")
            return proc.stdout
        else:
            log.warning(f"   ⚠️ ffmpeg 转换失败: {proc.stderr.decode()[:200]}")
            return input_data
    except FileNotFoundError:
        log.warning("   ⚠️ ffmpeg 未安装, 跳过格式转换")
        return input_data
    except Exception as e:
        log.warning(f"   ⚠️ ffmpeg 异常: {e}")
        return input_data


def enhance_audio(input_data: bytes) -> bytes:
    """ffmpeg 清晰化处理：降噪 + 去低频 + 响度归一化 → 高质量 WAV

    滤镜链:
      highpass=f=80   去 80Hz 以下低频隆隆声
      afftdn=nr=12    频域降噪（去空调/风扇等稳态噪声）
      loudnorm        EBU R128 响度归一化

    用于生成播放用 WAV（比转写WAV质量更好）。
    """
    ffmpeg = config.audio.ffmpeg_path if hasattr(config, 'audio') else "ffmpeg"
    afilters = "highpass=f=80,afftdn=nr=12,loudnorm=I=-16:TP=-1.5:LRA=11"

    try:
        proc = subprocess.run(
            [ffmpeg, '-y', '-i', 'pipe:0',
             '-af', afilters,
             '-ar', '16000', '-ac', '1', '-sample_fmt', 's16',
             '-f', 'wav', 'pipe:1'],
            input=input_data, capture_output=True, timeout=90
        )
        if proc.returncode == 0 and len(proc.stdout) > 44:
            log.info(f"   🎧 清晰化完成: WAV → WAV ({len(input_data)}→{len(proc.stdout)} bytes)")
            return proc.stdout
        else:
            log.warning(f"   ⚠️ 清晰化失败, 降级原样保存")
            return input_data
    except Exception as e:
        log.warning(f"   ⚠️ 清晰化异常: {e}")
        return input_data


def save_audio_wav(wav_bytes: bytes, rec_id: str) -> str:
    """保存清晰化 WAV 到 audio 目录，返回相对路径"""
    audio_dir = config.audio.audio_dir if hasattr(config, 'audio') else "audio"
    filename = f"{rec_id}.wav"
    filepath = os.path.join(audio_dir, filename)
    with open(filepath, 'wb') as f:
        f.write(wav_bytes)
    return filename  # 相对路径，如 rec_20260101_120000.wav


def _clean_debug_dir(keep: int = 10):
    """自动清理 debug/ 目录，只保留最近 N 个文件"""
    try:
        debug_dir = config.audio.debug_dir if hasattr(config, 'audio') else "debug"
        files = [os.path.join(debug_dir, f) for f in os.listdir(debug_dir)
                 if f.endswith('.wav')]
        files.sort(key=lambda f: os.path.getmtime(f), reverse=True)
        for f in files[keep:]:
            os.unlink(f)
            log.info(f"   🧹 清理旧文件: {os.path.basename(f)}")
    except Exception:
        pass


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
    返回: {"text": "...", "utterances": [...], "speakers": [...]}
    """
    doubao = config.doubao_asr if hasattr(config, 'doubao_asr') else None
    app_id = doubao.app_id if doubao else os.getenv("DOUBAO_ASR_APP_ID", "")
    access_key = doubao.access_key if doubao else os.getenv("DOUBAO_ASR_ACCESS_KEY", "")
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

    body = json.dumps({
        "user": {"uid": app_id},
        "audio": {"data": audio_b64},
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
        if asr_mode == "cloud_standard":
            log.info(f"☁️ 使用豆包标准版 ASR（说话人分离）")
            result = call_cloud_asr_standard(audio_bytes)
            text = result.get("text", "")
            if result.get("utterances"):
                utterances_data = result.get("utterances")
                log.info(f"   🗣️ 说话人: {result.get('speakers', [])}")
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
