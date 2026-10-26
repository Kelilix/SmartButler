"""TTS 输出适配器 — AnswerRouter ↔ VoiceAdapter 桥接。

## 问题背景

AnswerRouter 通过 `OutputAdapter.push(ButlerReply)` 推送回复,
但当前 VoiceAdapter 只有 `speak(text)` 方法,没有实现 `OutputAdapter` 接口。

## 解决方案

`TTSOutputAdapter` 包装 `VoiceAdapter`,实现 `OutputAdapter` 接口:

```
AnswerRouter.route()
    │
    ├─► OutputAdapter.push(reply)
    │       │
    │       ▼
    │   TTSOutputAdapter.push(reply)
    │       │
    │       ▼
    │   VoiceAdapter.speak(reply.content)
    │       │
    │       ▼
    │   TTS.speak() → AudioPlayer.play()
    │
    └─► RouteResult
```

## 设计原则

1. **依赖注入**:TTS 和 AudioPlayer 通过构造函数注入,支持 Fake 实现单测。
2. **BaseTTS 接口**:内部可替换 edge-tts / piper,不改变 `OutputAdapter` 调用方。
3. **播放策略**:
   - 语音期间忽略新指令(防止打断)
   - 播放完成后自动释放锁

## 依赖

- `BaseTTS`:edge-tts / piper
- `BaseAudioPlayer`:pygame / 厂商 SDK
- `VoiceAdapter.speak()` 已实现音频流输出

Author: SmartButler Team
Date: 2026-10-15
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from smartbutler.server.adapters import ButlerReply, OutputAdapter
from smartbutler.utils import get_logger
from smartbutler.voice.tts.base import BaseTTS, TTSVoice

if TYPE_CHECKING:
    from smartbutler.voice.player.base import BaseAudioPlayer

logger = get_logger(__name__)


class TTSOutputAdapter(OutputAdapter):
    """TTS 输出适配器 — 实现 OutputAdapter 接口,内部调用 TTS + AudioPlayer。

    Usage:

        # 生产:真实 TTS + Player
        tts = EdgeTTSImpl()
        player = PygameAudioPlayer()
        adapter = TTSOutputAdapter(tts=tts, player=player, voice=ZH_DEFAULT_VOICE)

        # 单测:Fake
        adapter = TTSOutputAdapter(tts=FakeTTS(), player=FakeAudioPlayer())

        # AnswerRouter 注入
        router = StrategyChainAnswerRouter(
            registry=DeviceRegistry([adapter]),
            strategies=[...],
        )
    """

    def __init__(
        self,
        tts: BaseTTS,
        player: "BaseAudioPlayer | None" = None,
        voice: TTSVoice | None = None,
        device_id: str = "tts_output",
        block_while_speaking: bool = True,
    ) -> None:
        """构造 TTSOutputAdapter。

        Args:
            tts: TTS 实例(BaseTTS 实现)。
            player: 音频播放器(BaseAudioPlayer 实现),None=只生成音频不播放。
            voice: 默认音色,None=用 TTS 默认音色。
            device_id: 设备 ID(OutputAdapter 接口需要)。
            block_while_speaking: 是否在播放时阻塞后续 push。
                True=语音期间忽略新指令;False=立即处理新指令(可能打断)。
        """
        self._tts = tts
        self._player = player
        self._voice = voice or TTSVoice(id="zh-CN-XiaoxiaoNeural", lang="zh-CN")
        self._device_id = device_id
        self._block_while_speaking = block_while_speaking

        # 播放锁:防止同时播放多段音频
        self._speaking_lock = asyncio.Lock()
        self._is_speaking = False

    @property
    def device_id(self) -> str:
        """OutputAdapter 接口:设备 ID。"""
        return self._device_id

    async def push(self, reply: ButlerReply) -> None:
        """推送 TTS 语音。

        Args:
            reply: ButlerReply,content 字段是要 TTS 的文本。

        Raises:
            TTSOutputError: TTS 或播放异常。
        """
        if not reply.content or not reply.content.strip():
            logger.debug("TTSOutputAdapter received empty content, skipping")
            return

        # 如果正在播放,选择是否等待
        if self._block_while_speaking:
            # 等待上一个播放完成
            if self._speaking_lock.locked():
                logger.debug("TTSOutputAdapter waiting for previous speech to finish")
                # 不等待,直接跳过(防止积压)
                # TODO:可改为排队机制
                return
        else:
            # 打断当前播放
            if self._speaking_lock.locked():
                logger.debug("TTSOutputAdapter interrupting current speech")
                # 取消当前播放
                # TODO:实现打断机制

        async with self._speaking_lock:
            self._is_speaking = True
            try:
                logger.debug(
                    "TTSOutputAdapter speaking",
                    content=reply.content[:50],
                    user_id=reply.user_id,
                )

                # 1. TTS 生成音频流
                audio_stream = self._tts.speak(reply.content, voice=self._voice)

                # 2. 播放音频流
                if self._player:
                    await self._player.play(audio_stream)
                else:
                    # 没有播放器:只生成音频流(测试用)
                    async for _ in audio_stream:
                        pass

                logger.debug("TTSOutputAdapter finished speaking")
            except Exception as e:
                # 注意:这里必须用 structlog 的 kwargs 写法。
                # 若用 stdlib logging.getLogger,`error=` 会被当成 Logger._log() 的
                # 非法参数抛 TypeError,反而把下面这行真正的原始异常给掩盖掉。
                logger.error(
                    "TTSOutputAdapter error",
                    error=str(e),
                    content=reply.content[:50],
                    exc_info=True,
                )
                raise TTSOutputError(f"TTS output failed: {e}") from e
            finally:
                self._is_speaking = False

    async def supports(self, reply: ButlerReply) -> bool:
        """OutputAdapter 接口:本适配器支持文本转语音。"""
        return True

    async def close(self) -> None:
        """释放资源。"""
        if self._tts:
            await self._tts.close()
        if self._player:
            await self._player.close()


class TTSOutputError(Exception):
    """TTS 输出异常。"""
    pass


__all__ = ["TTSOutputAdapter", "TTSOutputError"]
