"""Vibry AI Core — 声纹识别引擎

从 VibryCard Flask Server 移植。
基于 MFCC 特征提取 + 余弦相似度匹配，零额外深度学习依赖。

核心功能:
- 声纹提取 (MFCC with librosa, FFT fallback)
- 声纹注册/列表/删除
- 说话人识别 (余弦相似度)
- 转写文本中的说话人标签自动替换
"""

import io
import logging
import os
from pathlib import Path

import numpy as np
import soundfile as sf

log = logging.getLogger("vibry.voiceprint")

# ---- 声纹目录 ----
VOICEPRINT_DIR = Path(__file__).parent.parent / "data" / "voiceprints"
VOICEPRINT_DIR.mkdir(parents=True, exist_ok=True)

# ---- 依赖检测 ----
try:
    import librosa
    _has_librosa = True
except ImportError:
    _has_librosa = False
    log.info("📊 librosa 未安装，声纹提取将使用 FFT fallback")


# ===========================================================================
# 声纹提取
# ===========================================================================

def extract_voiceprint(wav_bytes: bytes) -> np.ndarray:
    """从 WAV 字节提取 40 维声纹向量 (MFCC 均值+标准差，L2 归一化)

    Args:
        wav_bytes: 16kHz mono WAV 音频字节

    Returns:
        shape=(40,) float32 L2-normalized embedding vector

    Raises:
        ValueError: 音频太短 (< 0.5s)
    """
    audio, sr = sf.read(io.BytesIO(wav_bytes), dtype='float32')
    if len(audio) < sr * 0.5:
        raise ValueError("音频太短，至少需要 0.5 秒")

    if _has_librosa:
        # MFCC: 20 维均值 + 20 维标准差 = 40 维
        mfcc = librosa.feature.mfcc(y=audio, sr=sr, n_mfcc=20)
        mean = np.mean(mfcc, axis=1)
        std = np.std(mfcc, axis=1)
        vec = np.concatenate([mean, std]).astype(np.float32)
    else:
        # FFT fallback: 频谱均值 → 截取/填充到 40 维
        spec = np.abs(np.fft.rfft(audio))
        chunk_size = max(1, len(spec) // 40)
        mean_spec = np.array([np.mean(spec[i*chunk_size:(i+1)*chunk_size]) for i in range(40)])
        vec = mean_spec.astype(np.float32)

    # L2 归一化
    norm = np.linalg.norm(vec)
    return vec / norm if norm > 0 else vec


# ===========================================================================
# 声纹存储
# ===========================================================================

def load_voiceprints() -> dict:
    """加载所有已注册声纹 → {name: embedding_vector}"""
    vps = {}
    for f in VOICEPRINT_DIR.iterdir():
        if f.suffix == '.npy':
            name = f.stem
            vps[name] = np.load(str(f))
    return vps


def save_voiceprint(name: str, vec: np.ndarray):
    """保存声纹到文件"""
    filepath = VOICEPRINT_DIR / f"{name}.npy"
    np.save(str(filepath), vec)
    log.info(f"🎤 声纹已保存: {name} (维度 {len(vec)})")


def delete_voiceprint_file(name: str) -> bool:
    """删除声纹文件"""
    filepath = VOICEPRINT_DIR / f"{name}.npy"
    if filepath.exists():
        filepath.unlink()
        log.info(f"🗑️ 声纹已删除: {name}")
        return True
    return False


# ===========================================================================
# 说话人识别
# ===========================================================================

def identify_speaker(segment_wav: bytes, voiceprints: dict = None,
                     threshold: float = 0.85) -> tuple:
    """识别单个音频片段的发言人

    Args:
        segment_wav: WAV 音频片段字节
        voiceprints: {name: embedding} 字典，None 则自动加载
        threshold: 余弦相似度阈值 (默认 0.85)

    Returns:
        (name, confidence) — name 为 None 表示未识别
    """
    if voiceprints is None:
        voiceprints = load_voiceprints()
    if not voiceprints:
        return None, 0

    emb = extract_voiceprint(segment_wav)
    best_name, best_sim = None, 0
    for name, vp in voiceprints.items():
        sim = float(np.dot(emb, vp))  # 余弦相似度 (向量已 L2 归一化)
        if sim > best_sim:
            best_sim = sim
            best_name = name

    if best_sim > threshold:
        return best_name, best_sim
    return None, best_sim


# ===========================================================================
# WAV 切片
# ===========================================================================

def wav_slice(wav_bytes: bytes, start_ms: float, end_ms: float) -> bytes:
    """从 WAV 字节中截取时间段，返回新的 WAV 字节"""
    import wave as _wave

    audio, sr = sf.read(io.BytesIO(wav_bytes), dtype='float32')
    start_sample = int(start_ms / 1000 * sr)
    end_sample = int(end_ms / 1000 * sr)
    segment = audio[start_sample:end_sample]

    # int16 写入
    segment_i16 = (segment * 32767).clip(-32768, 32767).astype(np.int16)
    buf = io.BytesIO()
    with _wave.open(buf, 'wb') as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sr)
        wf.writeframes(segment_i16.tobytes())
    return buf.getvalue()


# ===========================================================================
# 转写文本增强
# ===========================================================================

def apply_voiceprint_to_transcript(text: str, utterances: list,
                                   wav_bytes: bytes) -> str:
    """将 utterances 中的 [发言人X] 标签替换为声纹识别的真实姓名

    Args:
        text: 原始转写文本 (含 [发言人0] 等标签)
        utterances: 豆包标准版返回的 utterances 列表
        wav_bytes: 原始 WAV 音频字节

    Returns:
        替换后的文本 (如 [刘晓庆] 替换 [发言人0])
    """
    if not utterances or not _has_librosa:
        return text

    voiceprints = load_voiceprints()
    if not voiceprints:
        return text

    # 收集所有 speaker_id → 取最长的 utterance 片段
    speaker_ids = set()
    for u in utterances:
        additions = u.get('additions', {}) or {}
        sid = str(additions.get('speaker', '?'))
        if sid != '?':
            speaker_ids.add(sid)

    # 为每个 speaker 截取音频
    speaker_wavs = {}
    for sid in speaker_ids:
        best_u = max(
            [u for u in utterances
             if str((u.get('additions', {}) or {}).get('speaker', '?')) == sid],
            key=lambda u: u.get('end_time', 0) - u.get('start_time', 0),
            default=None
        )
        if best_u:
            try:
                seg = wav_slice(wav_bytes, best_u['start_time'], best_u['end_time'])
                speaker_wavs[sid] = seg
            except Exception:
                pass

    # 识别每个 speaker
    speaker_names = {}
    for sid, seg_wav in speaker_wavs.items():
        name, conf = identify_speaker(seg_wav, voiceprints)
        if name:
            speaker_names[sid] = name
            log.info(f"   🎤 发言人{sid} → {name} (置信度 {conf:.2f})")
        else:
            speaker_names[sid] = f'发言人{sid}'

    # 替换文本中的标签
    result = text
    for sid, name in speaker_names.items():
        result = result.replace(f'[发言人{sid}]', f'[{name}]')

    return result
