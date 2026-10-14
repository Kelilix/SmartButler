"""DeviceRegistry — 集中管理所有 OutputAdapter(Phase 7.5 D2)。

AnswerRouter / ButlerServer / FutureTTSAdapter / CameraAdapter 都注入同一个 Registry,
不再各自持有 adapter 列表。

决策记录:2026-10-14 D2。
"""
from __future__ import annotations

from smartbutler.server.adapters import OutputAdapter


class DeviceRegistry:
    """OutputAdapter 注册表。

    用法::

        registry = DeviceRegistry([output1, output2])
        registry.register(output3)
        adapter = registry.get("speaker.living_room")
        all_adapters = registry.all()
        first = registry.first()
    """

    def __init__(self, adapters: list[OutputAdapter] | None = None) -> None:
        self._by_id: dict[str, OutputAdapter] = {}
        self._all: list[OutputAdapter] = list(adapters) if adapters else []
        for ad in self._all:
            self._by_id[self._device_id(ad)] = ad

    def register(self, adapter: OutputAdapter) -> None:
        """运行时注册一个 OutputAdapter。"""
        self._all.append(adapter)
        self._by_id[self._device_id(adapter)] = adapter

    def get(self, device_id: str) -> OutputAdapter | None:
        """按 device_id 查一个 OutputAdapter。"""
        return self._by_id.get(device_id)

    def all(self) -> list[OutputAdapter]:
        """所有 OutputAdapter(顺序同注册顺序)。"""
        return list(self._all)

    def first(self) -> OutputAdapter | None:
        """第一个注册的 OutputAdapter(fallback 用)。"""
        return self._all[0] if self._all else None

    @staticmethod
    def _device_id(adapter: OutputAdapter) -> str:
        """从 OutputAdapter 拿 device_id。

        约定:
        - 有 ``device_id`` 属性 → 直接用(如 SpeakerOutputAdapter → "speaker.living_room")
        - 无 ``device_id`` → 用类名的 lower 名(如 InMemoryOutputAdapter → "inmemoryoutput")
        """
        explicit = getattr(adapter, "device_id", None)
        if explicit is not None:
            return str(explicit)
        return type(adapter).__name__.lower()


__all__ = ["DeviceRegistry"]
