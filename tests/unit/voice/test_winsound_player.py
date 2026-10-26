"""WinsoundAudioPlayer 单元测试。

验证:
- MP3 输入能被转码后播放(不依赖真实声卡,用 monkeypatch 拦截 winsound)
- 空音频流不报错
- 非 Windows 平台抛 AudioPlayerError
- 播放失败时抛 AudioPlayerError

注意:真实调用 winsound.PlaySound 会占用声卡,测试里全部 monkeypatch 掉。
"""

from __future__ import annotations

import io
import sys
import types
from collections.abc import AsyncIterator

import numpy as np
import pytest

from smartbutler.voice.player.base import AudioPlayerError
from smartbutler.voice.player.winsound_player import WinsoundAudioPlayer


async def _stream(*chunks: bytes) -> AsyncIterator[bytes]:
    """构造一个异步字节流。"""
    for c in chunks:
        yield c


def _make_mp3() -> bytes:
    frames = (0.1 * np.sin(2 * np.pi * 440 * np.linspace(0, 1, 24000))).astype(np.float32)
    buf = io.BytesIO()
    import soundfile as sf

    sf.write(buf, frames, 24000, format="MP3")
    return buf.getvalue()


@pytest.fixture
def fake_winsound(monkeypatch: pytest.MonkeyPatch) -> types.SimpleNamespace:
    """注入一个假的 winsound 模块,记录 PlaySound 调用。"""
    calls: list[tuple[object, int]] = []

    def _play_sound(sound: object, flags: int) -> None:
        calls.append((sound, flags))

    fake = types.SimpleNamespace(
        PlaySound=_play_sound,
        SND_MEMORY=1,
        SND_FILENAME=0x20000,
        SND_NODEFAULT=0x2,
        calls=calls,
    )
    monkeypatch.setitem(sys.modules, "winsound", fake)
    return fake


async def test_play_mp3_converts_and_plays(fake_winsound: types.SimpleNamespace) -> None:
    """MP3 应被转成 WAV 后播放,且走 SND_MEMORY 路径。"""
    player = WinsoundAudioPlayer()
    await player.play(_stream(_make_mp3()))

    assert len(fake_winsound.calls) == 1
    played, flags = fake_winsound.calls[0]
    assert isinstance(played, bytes)
    assert played[:4] == b"RIFF"
    assert flags == fake_winsound.SND_MEMORY


async def test_play_wav_direct(fake_winsound: types.SimpleNamespace) -> None:
    """WAV 输入应直接播放,不被重新转码。"""
    from smartbutler.voice.player.audio_decode import decode_to_wav

    wav = decode_to_wav(_make_mp3())
    player = WinsoundAudioPlayer()
    await player.play(_stream(wav))

    assert len(fake_winsound.calls) == 1
    played, _flags = fake_winsound.calls[0]
    assert played == wav


async def test_play_multiple_chunks(fake_winsound: types.SimpleNamespace) -> None:
    """多个 chunk 应被拼接后整体播放。"""
    mp3 = _make_mp3()
    half = len(mp3) // 2
    player = WinsoundAudioPlayer()
    await player.play(_stream(mp3[:half], mp3[half:]))

    assert len(fake_winsound.calls) == 1
    played, _flags = fake_winsound.calls[0]
    assert len(played) > 0


async def test_play_empty_stream_is_noop(fake_winsound: types.SimpleNamespace) -> None:
    """空音频流不应报错,也不应调用播放。"""
    player = WinsoundAudioPlayer()
    await player.play(_stream())

    assert fake_winsound.calls == []


async def test_play_non_windows_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """非 Windows 平台应抛 AudioPlayerError。"""
    monkeypatch.setattr(sys, "platform", "linux")
    player = WinsoundAudioPlayer()
    with pytest.raises(AudioPlayerError, match="只支持 Windows"):
        await player.play(_stream(b"x"))


async def test_play_failure_raises_audio_player_error(
    fake_winsound: types.SimpleNamespace,
) -> None:
    """winsound 播放失败应包装成 AudioPlayerError。"""

    def _boom(sound: object, flags: int) -> None:
        raise RuntimeError("no audio device")

    fake_winsound.PlaySound = _boom
    player = WinsoundAudioPlayer()
    with pytest.raises(AudioPlayerError, match="no audio device"):
        await player.play(_stream(_make_mp3()))


async def test_snd_memory_failure_falls_back_to_file(
    fake_winsound: types.SimpleNamespace,
) -> None:
    """SND_MEMORY 不支持时应回退到临时文件路径。"""
    calls: list[tuple[object, int]] = []

    def _play_sound(sound: object, flags: int) -> None:
        calls.append((sound, flags))
        if flags == fake_winsound.SND_MEMORY:
            raise RuntimeError("SND_MEMORY unsupported")

    fake_winsound.PlaySound = _play_sound
    player = WinsoundAudioPlayer()
    await player.play(_stream(_make_mp3()))

    assert len(calls) == 2
    # 第一次内存失败,第二次是文件路径
    assert calls[0][1] == fake_winsound.SND_MEMORY
    assert isinstance(calls[1][0], str)
    assert calls[1][1] == fake_winsound.SND_FILENAME | fake_winsound.SND_NODEFAULT


async def test_close_is_noop() -> None:
    """close 不应抛异常。"""
    player = WinsoundAudioPlayer()
    await player.close()
