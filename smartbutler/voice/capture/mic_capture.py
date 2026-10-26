"""MicCapture — sounddevice 麦克风采集实现。

实现 BaseAudioCapture 接口,统一输出 16kHz mono float32 PCM。

Dependencies:
    sounddevice (pip install sounddevice)

单测:用 FakeMicCapture 注 mock stream 即可。

Author: SmartButler Team
Date: 2026-10-15
"""

from __future__ import annotations

import asyncio
import logging
import queue as _std_queue  # 线程安全队列,用于 sounddevice callback 跨线程投递
import threading
from collections.abc import AsyncGenerator

import numpy as np

_logger = logging.getLogger(__name__)

from smartbutler.voice.capture.base import (
    AudioCaptureError,
    AudioChunk,
    BaseAudioCapture,
)

# ---------------------------------------------------------------------------
# 辅助:音频格式转换
# ---------------------------------------------------------------------------

def _to_float32_normalized(audio: np.ndarray) -> np.ndarray:
    """统一转 16kHz mono float32 [-1, 1]。

    处理情况:
    - int16 PCM → float32
    - int32/float64 → float32
    - 多声道 → mono(平均)
    - 任何采样率 → 16kHz(简单线性重采样,临时用;生产环境建议用 sox/ffmpeg)
    """
    target_sr = 16000

    # 类型归一化
    if audio.dtype == np.int16:
        audio = audio.astype(np.float32) / 32768.0
    elif audio.dtype == np.int32:
        audio = audio.astype(np.float32) / 2147483648.0
    elif audio.dtype == np.float64:
        audio = audio.astype(np.float32)
    elif audio.dtype != np.float32:
        audio = audio.astype(np.float32)

    # 声道归一化 → mono
    if audio.ndim == 2:
        # shape=(samples, channels)
        audio = audio.mean(axis=1)
    elif audio.ndim == 1:
        pass
    else:
        raise AudioCaptureError(f"Unexpected audio ndim: {audio.ndim}")

    # 重采样 → 16kHz(若需要)
    current_sr = getattr(audio, "_sr", target_sr)
    if current_sr != target_sr:
        audio = _resample_simple(audio, current_sr, target_sr)

    return audio.astype(np.float32)


def _resample_simple(audio: np.ndarray, from_sr: int, to_sr: int) -> np.ndarray:
    """简单线性重采样(质量一般,延迟不敏感场景够用)。"""
    if from_sr == to_sr:
        return audio
    ratio = len(audio) * to_sr / from_sr
    new_len = int(ratio)
    indices = np.linspace(0, len(audio) - 1, new_len)
    return np.interp(indices, np.arange(len(audio)), audio).astype(np.float32)


# ---------------------------------------------------------------------------
# MicCapture 实现
# ---------------------------------------------------------------------------

class MicCapture(BaseAudioCapture):
    """sounddevice 麦克风采集。

    Usage:
        async with MicCapture(device_index=0, chunk_ms=100) as capture:
            async for chunk in capture.stream():
                process(chunk.audio)

    Attributes:
        device_index: sounddevice 设备索引;None=默认设备。
        chunk_ms: 每块音频时长(毫秒),默认 100ms。
        ready: 是否已启动。
    """

    def __init__(
        self,
        device_index: int | None = None,
        chunk_ms: int = 100,
        target_sr: int = 16000,
        auto_gain: bool = True,
        gain_target_rms: float = 0.1,
    ) -> None:
        self._device_index = device_index
        self._chunk_ms = chunk_ms
        self._target_sr = target_sr
        self._auto_gain = auto_gain
        self._gain_target_rms = gain_target_rms
        # 自适应增益:用滑动平均估算"近 5 秒"的 RMS
        self._gain_ema: float | None = None
        self._gain_factor: float = 1.0
        self._ready = False
        self._stop_event = asyncio.Event()
        # 🐛 关键修复:用 std queue.Queue(线程安全)替代 asyncio.Queue
        # 因为 sounddevice callback 在 PortAudio 内部线程调,
        # asyncio.Queue 不是线程安全的,跨线程 put_nowait 会丢消息
        self._thread_queue: _std_queue.Queue[AudioChunk | None] = _std_queue.Queue(maxsize=200)
        self._stream: sd.InputStream | None = None
        # 🆕 每个 stream() 自己的 pump thread(无 bridge,无 round-robin)
        self._consumer_threads: list[tuple[threading.Thread, threading.Event]] = []
        self._next_consumer_idx: int = 0

    async def start(self) -> None:
        import logging
        _st_logger = logging.getLogger(__name__)
        _st_logger.info("▶️ MicCapture.start() 进入, _ready=%s, _stream=%s", self._ready, self._stream)
        if self._ready:
            _st_logger.info("▶️ start() 早就 _ready=True, 直接返回")
            return
        if self._stop_event.is_set():
            self._stop_event.clear()
        self._ready = True

        # 在新线程运行 sounddevice 同步流(避免阻塞事件循环)
        loop = asyncio.get_running_loop()
        _st_logger.info("▶️ 调 _start_stream() (executor)")
        await loop.run_in_executor(None, self._start_stream)
        _st_logger.info("▶️ _start_stream() 完成, _stream=%s", self._stream)

    def _start_stream(self) -> None:
        """同步启动 sounddevice 流。"""
        import logging

        import sounddevice as sd
        _sst_logger = logging.getLogger(__name__)
        _sst_logger.info("🎛️ _start_stream() 进入, device=%s, sr=%d, chunk_ms=%d",
                         self._device_index, self._target_sr, self._chunk_ms)

        try:
            self._stream = sd.InputStream(
                device=self._device_index,
                samplerate=self._target_sr,
                channels=1,
                dtype=np.float32,
                blocksize=int(self._target_sr * self._chunk_ms / 1000),
                callback=self._on_audio,
            )
            self._stream.start()
            _sst_logger.info("🎛️ sounddevice InputStream 启动成功, blocksize=%d",
                             int(self._target_sr * self._chunk_ms / 1000))
        except sd.PortAudioError as e:
            _sst_logger.error("🎛️ sounddevice 启动失败: %s", e)
            raise AudioCaptureError(f"Failed to open microphone: {e}") from e

    def _on_audio(
        self,
        indata: np.ndarray,
        frames: int,
        time_info: dict,
        status: sd.CallbackFlags,
    ) -> None:
        """sounddevice 回调(在音频线程同步调用)。"""
        import logging
        _cb_logger = logging.getLogger(__name__)
        # 累计调用次数(用于诊断)
        if not hasattr(self, "_cb_count"):
            self._cb_count = 0
        self._cb_count += 1
        if self._cb_count == 1 or self._cb_count % 50 == 0:
            _cb_logger.info(
                "🎙️ _on_audio 第 %d 次, frames=%d, status=%s",
                self._cb_count, frames, status
            )

        if status:
            # 溢出/超时警告,记录但继续
            logging.getLogger(__name__).warning(f"snddevice status: {status}")

        audio = indata[:, 0].astype(np.float32)
        if self._auto_gain:
            audio = self._apply_auto_gain(audio)

        chunk = AudioChunk(
            audio=audio,
            sample_rate=self._target_sr,
            channels=1,
            is_final=False,
        )
        try:
            self._thread_queue.put_nowait(chunk)
        except _std_queue.Full:
            pass  # 丢帧,避免阻塞音频线程

    def _apply_auto_gain(self, audio: np.ndarray) -> np.ndarray:
        """自适应增益:让输入音量稳定在目标 RMS 附近。"""
        rms = float(np.sqrt(np.mean(audio**2)))
        if rms < 1e-6:
            return audio
        alpha = 0.05
        if self._gain_ema is None:
            self._gain_ema = rms
        else:
            self._gain_ema = alpha * rms + (1 - alpha) * self._gain_ema
        if self._gain_ema > 1e-4:
            target_gain = self._gain_target_rms / self._gain_ema
        else:
            target_gain = 1.0
        target_gain = float(np.clip(target_gain, 1.0, 50.0))
        self._gain_factor = 0.9 * self._gain_factor + 0.1 * target_gain
        amplified = audio * self._gain_factor
        amplified = np.clip(amplified, -1.0, 1.0).astype(np.float32)
        return amplified

    async def stream(self) -> AsyncGenerator[AudioChunk, None]:
        """异步音频流(每次调用返回独立的消费者,有自己的 asyncio queue)。

        🐛 关键设计:每个 stream() 启动**自己专属的后台 thread**,从 _thread_queue
        拿 chunk,put 到自己私有的 asyncio.Queue。这样:
        - 不需要 round-robin,不会因为某个 consumer 慢而阻塞其他
        - 每个 stream() 退出不影响其他 stream
        - 没有共享的 bridge task,debug 简单
        """
        import logging
        import threading
        _br_logger = logging.getLogger(__name__)
        _br_logger.info("🟢 stream() 被调用, _ready=%s, _capture_id=%s", self._ready, id(self))

        try:
            if not self._ready:
                _br_logger.info("🟢 stream() 调 self.start()...")
                await self.start()
                _br_logger.info("🟢 self.start() 完成")

            consumer_queue: asyncio.Queue[AudioChunk | None] = asyncio.Queue(maxsize=50)

            loop = asyncio.get_running_loop()
            stop_event = threading.Event()
            consumer_idx = self._next_consumer_idx
            self._next_consumer_idx += 1

            def _pump_to_queue() -> None:
                """专属后台 thread:从 _thread_queue 拿 chunk, put 到 consumer_queue。
                这是**同步 thread**——不能直接 await asyncio.Queue.put(),
                需要用 loop.call_soon_threadsafe 把 put 调度回 asyncio 线程。
                """
                _local = logging.getLogger(__name__)
                _local.info("🔌 pump[%d] thread 启动, _thread_qsize=%d", consumer_idx, self._thread_queue.qsize())
                while not stop_event.is_set():
                    try:
                        chunk = self._thread_queue.get(timeout=0.1)
                    except _std_queue.Empty:
                        continue
                    if chunk is None:
                        # 停止信号
                        try:
                            loop.call_soon_threadsafe(consumer_queue.put_nowait, None)
                        except Exception:
                            pass
                        break
                    # 把 put 调度到 asyncio 线程
                    try:
                        rms = float(np.sqrt(np.mean(chunk.audio**2)))
                        _local.debug("🔌 pump[%d] 拿 chunk, RMS=%.4f, _thread_qsize=%d", consumer_idx, rms, self._thread_queue.qsize())
                    except Exception:
                        pass
                    fut = asyncio.run_coroutine_threadsafe(consumer_queue.put(chunk), loop)
                    try:
                        fut.result(timeout=0.5)
                    except Exception as e:
                        _local.debug("🔌 pump[%d] put 失败: %s", consumer_idx, e)
                _local.info("🔌 pump[%d] thread 退出", consumer_idx)

            pump_thread = threading.Thread(target=_pump_to_queue, name=f"mic-pump-{consumer_idx}", daemon=True)
            pump_thread.start()
            self._consumer_threads.append((pump_thread, stop_event))
            _br_logger.info("🟢 stream() 启动 pump thread #%d, 当前共 %d 个 pump",
                            consumer_idx, len(self._consumer_threads))

            while True:
                chunk = await consumer_queue.get()
                _br_logger.debug("🟢 stream()#%d await get() 返回, chunk=%s, qsize_before=%d",
                                consumer_idx, "None" if chunk is None else "AudioChunk", consumer_queue.qsize())
                if chunk is None:  # 结束信号
                    break
                yield chunk
                _br_logger.debug("🟢 stream()#%d yield 完成", consumer_idx)
        except BaseException as e:
            if not isinstance(e, GeneratorExit):
                _br_logger.exception("🟢 stream() 异常: %s", e)
            raise
        finally:
            try:
                stop_event.set()
                pump_thread.join(timeout=1.0)
                if pump_thread.is_alive():
                    _br_logger.warning("🟢 pump thread #%d 没能在 1s 内退出", consumer_idx)
                if (pump_thread, stop_event) in self._consumer_threads:
                    self._consumer_threads.remove((pump_thread, stop_event))
                _br_logger.info("🟢 consumer #%d 退出, 剩 %d 个 pump", consumer_idx, len(self._consumer_threads))
            except Exception:
                pass

    async def stop(self) -> None:
        """停止采集。"""
        if not self._ready:
            return
        self._ready = False
        self._stop_event.set()

        # 发送结束信号
        try:
            self._thread_queue.put_nowait(None)
        except _std_queue.Full:
            pass

        # 停止 sounddevice 流
        if self._stream is not None:
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, self._stream.stop)
            self._stream = None

    def __repr__(self) -> str:
        return f"MicCapture(device={self._device_index}, sr={self._target_sr})"


# ---------------------------------------------------------------------------
# Fake 实现(单测用)
# ---------------------------------------------------------------------------

class FakeMicCapture(BaseAudioCapture):
    """单测用假麦克风,直接注入 AudioChunk list。

    Usage:
        fake = FakeMicCapture([
            AudioChunk(audio=np.random.rand(1600).astype(np.float32)),
            AudioChunk(audio=np.random.rand(1600).astype(np.float32), is_final=True),
        ])
        async for chunk in fake.stream():
            ...

    Attributes:
        chunks: 要 yield 的音频块列表。
    """

    def __init__(self, chunks: list[AudioChunk]) -> None:
        self._chunks = chunks
        self._ready = False

    async def start(self) -> None:
        self._ready = True

    async def stop(self) -> None:
        self._ready = False

    async def stream(self) -> AsyncGenerator[AudioChunk, None]:
        if not self._ready:
            await self.start()
        for chunk in self._chunks:
            yield chunk


__all__ = [
    "MicCapture",
    "FakeMicCapture",
    "AudioCaptureError",
    "AudioChunk",
    "BaseAudioCapture",
]
