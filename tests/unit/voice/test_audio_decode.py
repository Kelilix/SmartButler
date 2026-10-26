"""音频转码层(audio_decode)单元测试。

验证:
- WAV 输入原样返回(不重复转码)
- MP3 等压缩格式能解成合法 WAV
- 空输入 / 非法输入抛 AudioPlayerError
- wav_info 能读出采样率与声道数
"""

from __future__ import annotations

import io
import wave

import numpy as np
import pytest

from smartbutler.voice.player.audio_decode import decode_to_wav, is_wav, wav_info
from smartbutler.voice.player.base import AudioPlayerError


def _make_tone_wav(sample_rate: int = 24000, channels: int = 1) -> bytes:
    """生成一段测试用 WAV(正弦波)。"""
    frames = np.sin(2 * np.pi * 440 * np.linspace(0, 1, sample_rate)).astype(np.float32)
    buf = io.BytesIO()
    import soundfile as sf

    sf.write(buf, frames, sample_rate, format="WAV", subtype="PCM_16")
    return buf.getvalue()


def _make_tone_mp3(sample_rate: int = 24000) -> bytes:
    """生成一段测试用 MP3。"""
    frames = (0.1 * np.sin(2 * np.pi * 440 * np.linspace(0, 1, sample_rate))).astype(np.float32)
    buf = io.BytesIO()
    import soundfile as sf

    sf.write(buf, frames, sample_rate, format="MP3")
    return buf.getvalue()


def test_is_wav_detects_wav_header() -> None:
    """合法 WAV 应被识别。"""
    assert is_wav(_make_tone_wav()) is True


def test_is_wav_rejects_mp3() -> None:
    """MP3 不应被误判为 WAV。"""
    assert is_wav(_make_tone_mp3()) is False


def test_is_wav_rejects_short_data() -> None:
    """长度不足的数据不应被判定为 WAV。"""
    assert is_wav(b"RIFF") is False


def test_decode_to_wav_passthrough_for_wav() -> None:
    """已经是 WAV 的输入应原样返回,不重复转码。"""
    wav = _make_tone_wav()
    assert decode_to_wav(wav) == wav


def test_decode_to_wav_converts_mp3() -> None:
    """MP3 应被转成合法 WAV。"""
    wav = decode_to_wav(_make_tone_mp3())

    assert wav[:4] == b"RIFF"
    assert wav[8:12] == b"WAVE"

    # 用标准库 wave 模块验证头部可解析
    with wave.open(io.BytesIO(wav), "rb") as w:
        assert w.getnchannels() == 1
        assert w.getframerate() == 24000
        assert w.getnframes() > 0


def test_decode_to_wav_preserves_sample_rate() -> None:
    """转码不应改变采样率。"""
    wav = decode_to_wav(_make_tone_mp3(sample_rate=24000))
    sample_rate, _channels = wav_info(wav)
    assert sample_rate == 24000


def test_decode_to_wav_raises_on_empty() -> None:
    """空输入应抛 AudioPlayerError。"""
    with pytest.raises(AudioPlayerError, match="音频数据为空"):
        decode_to_wav(b"")


def test_decode_to_wav_raises_on_garbage() -> None:
    """既不是 WAV 也解不出音频的垃圾数据应抛 AudioPlayerError。"""
    with pytest.raises(AudioPlayerError):
        decode_to_wav(b"not audio at all, just random bytes")


def test_wav_info_reads_header() -> None:
    """wav_info 应正确读出采样率与声道数。"""
    sample_rate, channels = wav_info(_make_tone_wav(sample_rate=24000))
    assert sample_rate == 24000
    assert channels == 1


def test_wav_info_raises_on_invalid() -> None:
    """非法 WAV 应抛 AudioPlayerError。"""
    with pytest.raises(AudioPlayerError):
        wav_info(b"not a wav file")
