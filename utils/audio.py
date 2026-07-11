"""Vibry AI Core — Audio processing utilities (format detection, conversion, enhancement)"""

import io, os, sys, struct, subprocess, tempfile, logging

log = logging.getLogger("vibry.audio")

# ---- Audio format detection ----
AUDIO_MAGIC = {
    b"RIFF": "wav", b"OggS": "opus",
    b"\xff\xfb": "mp3", b"\xff\xf3": "mp3",
    b"\xff\xfa": "mp3", b"ID3": "mp3", b"fLaC": "flac",
}

def detect_audio_format(data: bytes) -> str:
    for magic, fmt in AUDIO_MAGIC.items():
        if data[:len(magic)] == magic:
            return fmt
    if len(data) > 0:
        toc = data[0]
        if (toc & 0xFC) == 0xFC:
            return "opus_raw"
    return "wav"

def _is_opus_data(data: bytes) -> bool:
    if len(data) < 4: return False
    if data[:4] == b'RIFF' or data[:4] == b'OggS': return False
    check_data = data[4:] if data[:2] == b'KA' and len(data) > 4 else data
    if len(check_data) < 1: return False
    toc = check_data[0]
    config_bits = (toc >> 3) & 0x1F
    return 0 <= config_bits <= 19

def convert_to_wav(input_data: bytes, source_fmt: str = None) -> bytes:
    from app.config import config
    ffmpeg = config.audio.ffmpeg_path if hasattr(config, 'audio') else "ffmpeg"
    if source_fmt is None:
        source_fmt = detect_audio_format(input_data)
    if _is_opus_data(input_data):
        from utils.ogg_opus_muxer import raw_opus_to_ogg
        if input_data[:2] == b'KA':
            opus_data = input_data[4:]
        else:
            opus_data = input_data
        try:
            ogg_data = raw_opus_to_ogg(opus_data, frame_size=40)
            tmp_ogg = os.path.join(tempfile.gettempdir(), f"vibry_opus_{os.getpid()}.ogg")
            with open(tmp_ogg, 'wb') as f: f.write(ogg_data)
            proc = subprocess.run([ffmpeg, '-y', '-i', tmp_ogg, '-ar', '16000', '-ac', '1', '-sample_fmt', 's16', '-f', 'wav', 'pipe:1'], capture_output=True, timeout=60)
            os.unlink(tmp_ogg)
            if proc.returncode == 0 and len(proc.stdout) > 44: return proc.stdout
        except Exception as e:
            log.warning(f"Opus decode failed: {e}")
        return input_data
    if source_fmt in ("wav", "opus", "opus_raw"):
        # 自动检测 (不用 -f): OGG/Opus 容器和 WAV 都兼容
        try:
            proc = subprocess.run([ffmpeg, '-y', '-i', 'pipe:0', '-ar', '16000', '-ac', '1', '-sample_fmt', 's16', '-f', 'wav', 'pipe:1'], input=input_data, capture_output=True, timeout=60)
            if proc.returncode == 0 and len(proc.stdout) > 44: return proc.stdout
        except Exception: pass
        return input_data
    try:
        proc = subprocess.run([ffmpeg, "-y", "-f", source_fmt, "-i", "pipe:0", "-ar", "16000", "-ac", "1", "-sample_fmt", "s16", "-f", "wav", "pipe:1"], input=input_data, capture_output=True, timeout=30)
        if proc.returncode == 0 and len(proc.stdout) > 44: return proc.stdout
    except: pass
    return input_data

def enhance_audio(input_data: bytes) -> bytes:
    from app.config import config
    ffmpeg = config.audio.ffmpeg_path if hasattr(config, 'audio') else "ffmpeg"
    afilters = "highpass=f=80,afftdn=nr=12,loudnorm=I=-16:TP=-1.5:LRA=11"
    try:
        proc = subprocess.run([ffmpeg, '-y', '-i', 'pipe:0', '-af', afilters, '-ar', '16000', '-ac', '1', '-sample_fmt', 's16', '-f', 'wav', 'pipe:1'], input=input_data, capture_output=True, timeout=90)
        if proc.returncode == 0 and len(proc.stdout) > 44: return proc.stdout
    except: pass
    return input_data

def save_audio_wav(wav_bytes: bytes, rec_id: str) -> str:
    from app.config import config
    audio_dir = config.audio.audio_dir if hasattr(config, 'audio') else "audio"
    filename = f"{rec_id}.wav"
    filepath = os.path.join(audio_dir, filename)
    with open(filepath, 'wb') as f: f.write(wav_bytes)
    return filename

def _clean_debug_dir(keep: int = 10):
    try:
        from app.config import config
        debug_dir = config.audio.debug_dir if hasattr(config, 'audio') else "debug"
        files = [os.path.join(debug_dir, f) for f in os.listdir(debug_dir) if f.endswith('.wav')]
        files.sort(key=lambda f: os.path.getmtime(f), reverse=True)
        for f in files[keep:]: os.unlink(f)
    except: pass

def compress_for_asr(wav_bytes: bytes) -> tuple:
    from app.config import config
    ffmpeg = config.audio.ffmpeg_path if hasattr(config, 'audio') else "ffmpeg"
    try:
        proc = subprocess.run([ffmpeg, '-y', '-i', 'pipe:0', '-codec:a', 'libopus', '-b:a', '16k', '-ac', '1', '-ar', '16000', '-f', 'opus', 'pipe:1'], input=wav_bytes, capture_output=True, timeout=120)
        if proc.returncode == 0 and len(proc.stdout) > 100: return (proc.stdout, 'ogg', 'opus')
    except: pass
    return (wav_bytes, 'wav', None)
