"""反馈路由层（Phase 7）。

EventTrigger 拿到管家 answer 后,如何决定推给哪个设备?

```
管家 answer = "欢迎回家"
        │
        ▼
   AnswerRouter
        │
        ├─► PresenceService.get_active_device(user_id) → "speaker.living_room"
        │
        └─► 调 SpeakerAdapter.tts(text="欢迎回家", target="speaker.living_room")
```

Phase 7 启动时实现。
"""
from __future__ import annotations

from smartbutler.events.routing.answer_router import AnswerRouter
from smartbutler.events.routing.answer_router_console import ConsoleAnswerRouter
from smartbutler.events.routing.device_registry import DeviceRegistry
from smartbutler.events.routing.presence import PresenceService
from smartbutler.events.routing.presence_simple import SimplePresenceService
from smartbutler.events.routing.route_result import RouteResult

__all__ = [
    "AnswerRouter",
    "ConsoleAnswerRouter",
    "DeviceRegistry",
    "PresenceService",
    "SimplePresenceService",
    "RouteResult",
]
