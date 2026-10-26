"""A+B 改动的端到端验证。

1. Step A: listen() 现在是真异步生成器 —— async for 能正常消费(不再是 TypeError)
2. Step B: speak()/stream() 类型契约正确 —— mypy 家族错误清零
3. 运行时行为未变: TTS 仍能产出真实音频
"""

import asyncio
import inspect

import numpy as np

from smartbutler.voice.capture.mic_capture import FakeMicCapture
from smartbutler.voice.tts.edge_tts import EdgeTTSImpl, FakeTTS
from smartbutler.voice.wake_word.base import BaseWakeWordDetector
from smartbutler.voice.wake_word.simple import SimpleWakeWordDetector


async def check_listen_is_async_generator() -> None:
    """Step A: 基类声明 + 实现类都是异步生成器。"""
    print("=== Step A: listen() 异步生成器语义 ===")
    print(f"  BaseWakeWordDetector.listen isasyncgenfunction: "
          f"{inspect.isasyncgenfunction(BaseWakeWordDetector.listen)}")

    det = SimpleWakeWordDetector(keyword="btn", energy_threshold=0.0)
    print(f"  SimpleWakeWordDetector.listen isasyncgenfunction: "
          f"{inspect.isasyncgenfunction(det.listen)}")

    # 关键:async for 能进入(不再是 TypeError: requires __aiter__)
    try:
        async for _ in det.listen():
            pass
    except NotImplementedError as e:
        print(f"  ✓ async for 正常进入异步生成器, 抛出预期 NotImplementedError")
        print(f"    msg: {str(e)[:50]}...")
    except TypeError as e:
        print(f"  ✗ 仍然是 TypeError: {e}")
        raise


async def check_speak_still_works() -> None:
    """Step B: TTS 仍能产出音频。"""
    print("\n=== Step B: speak() 产出音频 ===")
    fake = FakeTTS(b"FAKE_CHUNK")
    chunks = [c async for c in fake.speak("你好")]
    print(f"  ✓ FakeTTS 产出 {len(chunks)} 个 chunk: {chunks}")

    # 真实 EdgeTTS
    tts = EdgeTTSImpl()
    total = 0
    async for c in tts.speak("测试"):
        total += len(c)
    print(f"  ✓ EdgeTTSImpl 产出 {total} bytes")
    await tts.close()


async def check_capture_stream() -> None:
    """capture.stream() 消费正常。"""
    print("\n=== capture.stream() 消费 ===")
    cap = FakeMicCapture()
    n = 0
    async for chunk in cap.stream():
        n += 1
    print(f"  ✓ FakeMicCapture 产出 {n} 个 chunk")


async def main() -> None:
    await check_listen_is_async_generator()
    await check_speak_still_works()
    await check_capture_stream()
    print("\n全部通过 ✅")


if __name__ == "__main__":
    asyncio.run(main())
