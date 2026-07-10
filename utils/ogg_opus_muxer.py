"""裸 Opus 帧 → OGG/Opus 容器封装器
pnote 录音卡输出原始 Opus 帧流（40字节/帧，16kHz mono，无 OGG 容器），
ffmpeg 无法直接读取。本模块将其封装为标准 OGG/Opus 容器，供 ffmpeg 解码。

参考: RFC 7845 (Ogg Encapsulation for the Opus)
"""
import struct

# 完整的 OGG CRC 查找表（多项式 0x04C11DB7）— 来自 libogg ogg_crc.c
_CRC_TABLE = []
for _i in range(256):
    _r = _i << 24
    for _ in range(8):
        _r = ((_r << 1) ^ 0x04C11DB7) & 0xFFFFFFFF if (_r & 0x80000000) else (_r << 1) & 0xFFFFFFFF
    _CRC_TABLE.append(_r)


def _ogg_crc(data: bytes) -> int:
    crc = 0
    for b in data:
        crc = ((crc << 8) ^ _CRC_TABLE[((crc >> 24) & 0xFF) ^ b]) & 0xFFFFFFFF
    return crc


def _make_ogg_page(header_type: int, granule: int, serial: int,
                   page_seq: int, packets: list) -> bytes:
    """构造一个 OGG 页面

    OGG 页面头布局（RFC 3533）:
      OggS(4) | version(1) | type(1) | granule(8) | serial(4) | pageseq(4) | CRC(4) | segcount(1) | segtable
      注意: CRC 在 segcount 之前！
    """
    segments = []
    body = b''
    for pkt in packets:
        body += pkt
        seg_len = len(pkt)
        while seg_len >= 255:
            segments.append(255)
            seg_len -= 255
        segments.append(seg_len)

    seg_table = bytes(segments)
    # OGG 页面头（27字节 + 段表）：CRC 字段先填0，算完再写回
    header = (
        b'OggS'                              # capture pattern
        + struct.pack('<BBqIII',
            0,            # version
            header_type,  # 0x02=BOS, 0x04=EOS, 0x00=continuation
            granule,      # granule position (int64)
            serial,       # serial number (uint32)
            page_seq,     # page sequence (uint32)
            0)            # CRC placeholder（偏移22，4字节）
        + struct.pack('<B', len(seg_table))  # segment count（偏移26）
        + seg_table
    )
    page_no_crc = header + body
    crc = _ogg_crc(page_no_crc)
    # 写回 CRC（偏移22，4字节 LE）
    return page_no_crc[:22] + struct.pack('<I', crc) + page_no_crc[26:]


def raw_opus_to_ogg(raw_opus: bytes, frame_size: int = 40,
                    sample_rate: int = 16000, channels: int = 1) -> bytes:
    """把裸 Opus 帧流封装为 OGG/Opus 容器字节流

    Args:
        raw_opus: 裸 Opus 帧字节流
        frame_size: 每帧字节数（pnote 默认 40）
        sample_rate: 采样率（默认 16000）
        channels: 声道数（默认 1）
    Returns:
        OGG/Opus 容器字节流
    """
    frames = [raw_opus[i:i+frame_size] for i in range(0, len(raw_opus), frame_size)]
    serial = 0x56495242  # 'VIBR' (任意)
    samples_per_frame = sample_rate // 50  # 20ms 帧的采样数（16kHz=320）
    pre_skip = 312  # Opus 推荐 preskip

    # OpusHead ID Header (RFC 7845 §5.1)
    # Magic "OpusHead" + version(1) channels(1) preskip(2LE) rate(4LE) gain(2LE) mappingfamily(1)
    opus_head = (
        b'OpusHead'
        + struct.pack('<BBHIhB',
            1,           # version
            channels,    # channel count
            pre_skip,    # preskip
            sample_rate, # sample rate
            0,           # output gain
            0)           # channel mapping family (0=mono/stereo)
    )

    out = bytearray()
    # Page 0: OpusHead (BOS)
    out += _make_ogg_page(0x02, 0, serial, 0, [opus_head])
    # Page 1: OpusTags (RFC 7845 §5.2 — 必须有，否则 ffmpeg 报 Header processing failed)
    # 格式: "OpusTags" + vendor_len(4LE) + vendor + comment_count(4LE) + [comment]
    vendor = b'VibryCard'
    opus_tags = b'OpusTags' + struct.pack('<I', len(vendor)) + vendor + struct.pack('<I', 0)
    out += _make_ogg_page(0x00, 0, serial, 1, [opus_tags])

    # Pages 2..N: Opus 音频帧（每页放一批帧，减少页面数）
    granule = 0
    page_seq = 2
    frames_per_page = 50  # 每页50帧（1秒）
    for i in range(0, len(frames), frames_per_page):
        batch = frames[i:i+frames_per_page]
        granule += len(batch) * samples_per_frame
        is_last = (i + frames_per_page >= len(frames))
        out += _make_ogg_page(
            0x04 if is_last else 0x00,  # 最后一页设 EOS
            granule, serial, page_seq, batch)
        page_seq += 1

    return bytes(out)


if __name__ == '__main__':
    # 自测
    raw = open('debug/req57876_raw.opus', 'rb').read()
    ogg = raw_opus_to_ogg(raw)
    out_path = 'debug/test_muxer.opus'
    with open(out_path, 'wb') as f:
        f.write(ogg)
    print(f'裸帧 {len(raw)} bytes → OGG {len(ogg)} bytes ({len(raw)//40} 帧)')
    print(f'输出: {out_path}')
