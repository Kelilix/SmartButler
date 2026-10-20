"""mic_test.py — 麦克风诊断工具。

用法:
    python -m smartbutler.scripts.mic_test

功能:
    1. 列出所有可用的输入设备
    2. 录制 3 秒音频
    3. 保存为 mic_test.wav
    4. 打印能量级别,验证麦克风工作
    5. 可选:实时显示能量条,边录边看

Author: SmartButler Team
Date: 2026-10-15
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from pathlib import Path

import numpy as np


async def list_devices() -> None:
    """列出所有音频输入设备。"""
    import sounddevice as sd

    print("=" * 70)
    print("可用的音频输入设备")
    print("=" * 70)

    devices = sd.query_devices()
    for i, dev in enumerate(devices):
        kind = []
        if dev["max_input_channels"] > 0:
            kind.append("输入")
        if dev["max_output_channels"] > 0:
            kind.append("输出")
        kind_str = "/".join(kind) if kind else "未知"

        default_marker = ""
        try:
            default_in = sd.query_devices(kind="input")
            if default_in["name"] == dev["name"]:
                default_marker = " ← 默认"
        except Exception:
            pass

        print(
            f"  [{i}] {dev['name']} "
            f"({kind_str}, 采样率={int(dev['default_samplerate'])}Hz, "
            f"输入通道={dev['max_input_channels']}){default_marker}"
        )
    print()


def record_wav(
    duration: float,
    sample_rate: int,
    channels: int,
    device: int | None,
    output_path: Path,
    show_realtime: bool = False,
) -> np.ndarray:
    """录制音频并保存为 WAV。"""
    import sounddevice as sd
    from scipy.io import wavfile

    print(f"准备录制: 时长={duration}s, 采样率={sample_rate}, 通道={channels}, 设备={device}")
    print(f"输出文件: {output_path}")
    print()

    if show_realtime:
        print("开始实时能量监控(能量条: [###....] = 当前音量):")
    else:
        print("开始录制...")

    # 录制
    audio = sd.rec(
        int(duration * sample_rate),
        samplerate=sample_rate,
        channels=channels,
        dtype="float32",
        device=device,
    )
    sd.wait()  # 等待录音完成

    if show_realtime:
        # 录完之后再分析每帧能量,画个 ASCII 图
        frame_size = int(0.05 * sample_rate)  # 50ms 一帧
        n_frames = len(audio) // frame_size
        print()
        print("能量时间线(每 50ms 一格):")
        print("-" * 70)
        for i in range(n_frames):
            start = i * frame_size
            end = start + frame_size
            frame = audio[start:end, 0] if audio.ndim > 1 else audio[start:end]
            rms = float(np.sqrt(np.mean(frame**2)))
            # 归一化到 0-50
            bar_len = int(min(50, rms * 200))
            bar = "#" * bar_len
            t_sec = i * 0.05
            print(f"  t={t_sec:5.2f}s | {bar:<50} | RMS={rms:.4f}")
        print("-" * 70)

    # 单声道化(确保 wavfile 能写)
    if audio.ndim > 1 and audio.shape[1] > 1:
        audio_mono = np.mean(audio, axis=1)
    else:
        audio_mono = audio[:, 0] if audio.ndim > 1 else audio

    # 统计
    rms_overall = float(np.sqrt(np.mean(audio_mono**2)))
    peak = float(np.max(np.abs(audio_mono)))

    print()
    print(f"录制完成:")
    print(f"  时长: {len(audio_mono) / sample_rate:.2f}s")
    print(f"  RMS 能量: {rms_overall:.4f} (0.0=静音, >0.05=有声音, >0.2=很大声)")
    print(f"  峰值: {peak:.4f}")
    print()

    # 能量判断
    if rms_overall < 0.005:
        print("⚠ 警告: 录音几乎静音!")
        print("  可能原因:")
        print("  1. 麦克风没选对(打开了默认设备,可能是静音的)")
        print("  2. Windows 隐私设置阻止了麦克风访问")
        print("  3. 耳机麦克风没插好")
        print("  4. 系统音量/麦克风增益被关掉了")
    elif rms_overall < 0.05:
        print("⚠ 声音很小,可能需要:")
        print("  1. 对着麦克风大声说话")
        print("  2. 调高麦克风增益(系统设置 -> 声音 -> 输入)")
    else:
        print("✓ 麦克风工作正常,可以听录到的声音")

    # 保存 WAV
    pcm_int16 = np.clip(audio_mono * 32767, -32768, 32767).astype(np.int16)
    wavfile.write(str(output_path), sample_rate, pcm_int16)
    print(f"✓ 已保存: {output_path}")
    print(f"  播放: 双击文件,或用 'start {output_path}' 打开")

    return audio_mono


def record_with_realtime_meter(
    duration: float,
    sample_rate: int,
    channels: int,
    device: int | None,
) -> None:
    """实时能量条模式(边录边看)。"""
    import sounddevice as sd

    print(f"实时录音(将持续 {duration} 秒),请说话:")
    print()

    block_size = int(0.05 * sample_rate)  # 50ms
    blocks: list[np.ndarray] = []

    start_time = time.time()
    elapsed = 0.0

    try:
        with sd.InputStream(
            samplerate=sample_rate,
            channels=channels,
            dtype="float32",
            device=device,
            blocksize=block_size,
        ) as stream:
            while elapsed < duration:
                block, _ = stream.read(block_size)
                block_mono = block[:, 0] if block.ndim > 1 else block
                rms = float(np.sqrt(np.mean(block_mono**2)))
                bar_len = int(min(60, rms * 200))
                bar = "#" * bar_len
                print(f"\r  [{elapsed:5.1f}s] |{bar:<60}| RMS={rms:.4f}", end="", flush=True)
                blocks.append(block_mono)
                elapsed = time.time() - start_time
    except KeyboardInterrupt:
        print("\n\n  (用户中断)")

    print("\n")
    audio = np.concatenate(blocks) if blocks else np.array([], dtype=np.float32)
    rms = float(np.sqrt(np.mean(audio**2))) if len(audio) > 0 else 0.0
    print(f"完成: 录了 {len(audio) / sample_rate:.2f}s, RMS={rms:.4f}")


def play_wav(path: Path) -> None:
    """播放 WAV(用 pygame,无 pygame 时用 winsound 兜底)。

    用同步方式阻塞播放,避免和主协程冲突。
    """
    # Windows 上 winsound.PlaySound(path, SND_FILENAME) 是同步阻塞的
    if sys.platform == "win32":
        try:
            import winsound

            winsound.PlaySound(str(path), winsound.SND_FILENAME | winsound.SND_NODEFAULT)
            return
        except Exception as e:
            print(f"winsound 播放失败: {e}")

    # 兜底:让用户手动播
    print(f"请手动播放: 双击 {path}")


async def main() -> None:
    parser = argparse.ArgumentParser(description="麦克风诊断工具")
    parser.add_argument("--duration", type=float, default=3.0, help="录制时长(秒)")
    parser.add_argument("--sample-rate", type=int, default=16000, help="采样率")
    parser.add_argument("--channels", type=int, default=1, help="通道数(1=单声道)")
    parser.add_argument("--device", type=int, default=None, help="输入设备 ID(默认=系统默认)")
    parser.add_argument("--output", type=str, default="mic_test.wav", help="输出 WAV 文件名")
    parser.add_argument("--no-realtime", action="store_true", help="禁用实时能量显示")
    parser.add_argument("--realtime-only", action="store_true", help="仅实时显示,不录制")
    parser.add_argument("--no-list-devices", action="store_true", help="不列出设备")
    parser.add_argument("--no-play", action="store_true", help="录制后不自动播放")
    args = parser.parse_args()

    print("=" * 70)
    print("SmartButler 麦克风诊断工具")
    print("=" * 70)
    print()

    if not args.no_list_devices:
        await list_devices()

    if args.realtime_only:
        record_with_realtime_meter(
            duration=args.duration,
            sample_rate=args.sample_rate,
            channels=args.channels,
            device=args.device,
        )
        return

    output_path = Path(args.output).resolve()

    try:
        audio = record_wav(
            duration=args.duration,
            sample_rate=args.sample_rate,
            channels=args.channels,
            device=args.device,
            output_path=output_path,
            show_realtime=not args.no_realtime,
        )
    except Exception as e:
        print(f"\n✗ 录音失败: {e}")
        print("\n排查建议:")
        print("  1. 确认 sounddevice 已安装: pip install sounddevice")
        print("  2. Windows 隐私设置 -> 麦克风 -> 允许应用访问")
        print("  3. 确认麦克风是默认设备(用 --device <id> 指定)")
        sys.exit(1)

    if not args.no_play:
        print()
        print("自动播放录到的音频...")
        play_wav(output_path)


if __name__ == "__main__":
    asyncio.run(main())
