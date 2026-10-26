"""音频解码工具 —— 把 TTS 输出的压缩音频(MP3 等)转成 PCM WAV。

为什么需要这一层:
    - edge-tts 输出的是 MP3,而 Windows 的 winsound 只能直接播放 WAV。
    - pygame 在 Python 3.14 上没有预编译 wheel(最高只到 cp313),当前环境装不上。
    - 把转码收敛到一处,各播放器只负责"播放",避免重复实现与行为漂移。

依赖:
    soundfile (pip install soundfile) —— 基于 libsndfile,支持 MP3/AAC/FLAC/OGG 解码。

Author: SmartButler Team
Date: 2026-10-26
"""

from __future__ import annotations

import io
import wave

from smartbutler.voice.player.base import AudioPlayerError

__all__ = ["decode_to_wav", "is_wav", "wav_info"]


def is_wav(data: bytes) -> bool:
    """判断字节流是否为 WAV(RIFF/WAVE)。"""
    return len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WAVE"


def decode_to_wav(audio_bytes: bytes, *, subtype: str = "PCM_16") -> bytes:
    """把音频字节流转成 PCM WAV 字节流。

    已经是 WAV 的输入原样返回;其余格式(MP3/AAC/FLAC/OGG...)经 soundfile 解码后
    重编码为 WAV。

    Args:
        audio_bytes: 原始音频字节。
        subtype: WAV 采样类型,默认 PCM_16(与 winsound 兼容性最好)。

    Returns:
        WAV 字节流(以 b"RIFF" 开头)。

    Raises:
        AudioPlayerError: 音频为空、缺少 soundfile、或解码/编码失败。
    """
    if not audio_bytes:
        raise AudioPlayerError("音频数据为空,无法解码")

    if is_wav(audio_bytes):
        return audio_bytes

    try:
        import soundfile as sf
    except ImportError as e:
        raise AudioPlayerError(
            "缺少 soundfile 依赖,无法把压缩音频转成 WAV。\n"
            "建议: pip install soundfile"
        ) from e

    try:
        data, samplerate = sf.read(io.BytesIO(audio_bytes), dtype="float32")
    except Exception as e:
        raise AudioPlayerError(f"音频解码失败(非 WAV 且无法转码): {e}") from e

    if len(data) == 0:
        raise AudioPlayerError("音频解码结果为空")

    out = io.BytesIO()
    try:
        sf.write(out, data, samplerate, format="WAV", subtype=subtype)
    except Exception as e:
        raise AudioPlayerError(f"WAV 编码失败: {e}") from e

    return out.getvalue()


def wav_info(wav_bytes: bytes) -> tuple[int, int]:
    """读取 WAV 的采样率与声道数。

    Args:
        wav_bytes: WAV 字节流。

    Returns:
        ``(采样率, 声道数)``。

    Raises:
        AudioPlayerError: 不是合法 WAV 或头部损坏。
    """
    try:
        with wave.open(io.BytesIO(wav_bytes), "rb") as w:
            return w.getframerate(), w.getnchannels()
    except Exception as e:
        raise AudioPlayerError(f"无法解析 WAV 头: {e}") from e
