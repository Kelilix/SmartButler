"""SimplePresenceService — PresenceService 最小实现(Phase 7.5 D3)。

职责:回答"用户当前最合适的输出设备是谁"(7.5a:用户手动配置,in-memory dict)。

接入点(2026-10-14 决策 D3):
- 单测:直接调 ``presence.update(user_id, PresenceInfo(...))``
- dev_chat:加 /presence set/get 命令
- 7.5+ 不开 HTTP/CLI(等 7.5b 接门锁数据流时一并做)

Phase 7.5 启动时的简化路径(见 presence.py §10):

| 阶段 | 实现 | 数据源 |
|------|------|--------|
| 7.5a(本文件) | 用户手动配置"我现在在客厅" | dict |
| 7.5b | 智能门锁最后开门位置推断 | 门锁历史 |
| 7.5c | 智能手环 / 手机 GPS 接入 | Bluetooth / 手机定位 |
| 7.5d | 摄像头人脸识别 | HomeAssistant + 摄像头 |
| 7.5e | 智能音响"我在听"指示灯 | 麦克风阵列 |

决策记录:2026-10-14 D3。
"""
from __future__ import annotations

from smartbutler.events.routing.presence import PresenceInfo, PresenceService


class SimplePresenceService(PresenceService):
    """Phase 7.5a:in-memory dict,用户显式 update()。

    用法::

        presence = SimplePresenceService()
        await presence.update(
            "u1",
            PresenceInfo(
                user_id="u1",
                device_id="speaker.living_room",
                device_type="speaker",
                confidence=0.9,
                last_seen_at=datetime.now(UTC),
            )
        )
        info = await presence.get_active_device("u1")
        assert info.device_id == "speaker.living_room"
    """

    def __init__(self) -> None:
        self._store: dict[str, PresenceInfo] = {}

    async def get_active_device(self, user_id: str) -> PresenceInfo | None:
        """返回最近一次 update 的 PresenceInfo。"""
        return self._store.get(user_id)

    async def update(self, user_id: str, info: PresenceInfo) -> None:
        """用户显式写入在场信息(7.5a:dev_chat / 单测调)。"""
        self._store[user_id] = info

    # ---- 便利方法(单测 / dev_chat 用) ----

    def set_sync(self, user_id: str, info: PresenceInfo) -> None:
        """同步版 update(单测场景不跑 event loop 时用)。"""
        self._store[user_id] = info

    def clear(self, user_id: str) -> None:
        """清除某个用户的在场信息。"""
        self._store.pop(user_id, None)

    def clear_all(self) -> None:
        """清除所有在场信息(测试隔离用)。"""
        self._store.clear()


__all__ = ["SimplePresenceService"]
