"""PresenceService 抽象接口（Phase 7）。

## 职责

回答"用户现在在哪儿,用哪个设备最合适"。

## Phase 7 启动时的简化实现

| 阶段 | 实现 | 数据源 |
|------|------|--------|
| 7.5a | 用户手动配置"我现在在客厅" | 配置文件 / 语音命令 |
| 7.5b | 智能门锁最后开门位置推断 | 门锁历史 |
| 7.5c | 智能手环 / 手机 GPS 接入 | Bluetooth / 手机定位 |
| 7.5d | 摄像头人脸识别 | HomeAssistant + 摄像头 |
| 7.5e | 智能音响"我在听"指示灯 | 麦克风阵列 |

## 关键字段

| 字段 | 说明 |
|------|------|
| ``device_id`` | 当前最适合的输出设备(智能音响) |
| ``device_type`` | 设备类型(SPEAKER / PHONE / WATCH) |
| ``confidence`` | 推断置信度 [0, 1] |
| ``last_seen_at`` | 用户最后活跃时间 |
| ``is_sleeping`` | 是否在睡觉(影响是否推 TTS) |
| ``is_dnd`` | 是否请勿打扰(影响是否推 TTS) |
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class PresenceInfo:
    """用户在场信息。"""

    user_id: str
    device_id: str                        # "speaker.living_room"
    device_type: str                      # "speaker" / "phone" / "watch"
    confidence: float                     # [0, 1]
    last_seen_at: datetime
    is_sleeping: bool = False
    is_dnd: bool = False                  # 请勿打扰

    def should_notify(self, priority: str) -> bool:
        """根据 priority 决定是否实际通知。"""
        if self.is_dnd and priority != "urgent":
            return False
        if self.is_sleeping and priority == "normal":
            return False
        return True


class PresenceService(ABC):
    """用户在场服务抽象接口。"""

    @abstractmethod
    async def get_active_device(self, user_id: str) -> PresenceInfo | None:
        """获取用户当前活跃设备。

        Returns:
            - PresenceInfo: 当前最合适的输出设备
            - None: 用户不在家 / 设备全离线 → 调用方决定 fallback
        """
        ...

    @abstractmethod
    async def update(self, user_id: str, info: PresenceInfo) -> None:
        """更新用户在场信息(由门锁 / 手环 / 手机等主动上报)。"""
        ...


__all__ = ["PresenceService", "PresenceInfo"]
