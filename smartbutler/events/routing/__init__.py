"""反馈路由层(Phase 7)。

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

🆕 7.6 声纹识别扩展示意图::

    VoiceAdapter 麦克风音频 → 声纹 embedding
       ↓
    SpeakerRegistry.identify(embedding) → user_id
       ↓
    VoiceEvent.speakers = [SpeakerInfo(user_id=..., confidence=...)]
       ↓
    EventTrigger 调 ainvoke(speakers) → orchestrator 知道跟谁对话
"""
from __future__ import annotations

from smartbutler.events.routing.answer_router import (
    AnswerRouter,
    EchoInputStrategy,
    FirstRegisteredStrategy,
    PresenceStrategy,
    RoutingContext,
    RoutingStrategy,
    VisualNearestStrategy,  # 7.8 占位
)
from smartbutler.events.routing.answer_router_console import (
    ConsoleAnswerRouter,
    StrategyChainAnswerRouter,
    build_default_chain_answer_router,
)
from smartbutler.events.routing.device_registry import DeviceRegistry
from smartbutler.events.routing.presence import PresenceService
from smartbutler.events.routing.presence_simple import SimplePresenceService
from smartbutler.events.routing.route_result import RouteResult
from smartbutler.events.routing.speaker_registry import (
    SpeakerMatch,
    SpeakerProfile,
    SpeakerRegistry,
)
from smartbutler.events.routing.speaker_registry_simple import SimpleSpeakerRegistry
from smartbutler.events.routing.speaker_registry_embedding import EmbeddingSpeakerRegistry


__all__ = [
    # AnswerRouter 主类
    "AnswerRouter",
    "StrategyChainAnswerRouter",
    "ConsoleAnswerRouter",
    "build_default_chain_answer_router",
    # 路由策略
    "RoutingStrategy",
    "RoutingContext",
    "EchoInputStrategy",
    "PresenceStrategy",
    "FirstRegisteredStrategy",
    "VisualNearestStrategy",  # 7.8 占位
    # 设备与在场
    "DeviceRegistry",
    "PresenceService",
    "SimplePresenceService",
    "RouteResult",
    # 🆕 7.6 声纹识别
    "SpeakerRegistry",
    "SimpleSpeakerRegistry",
    "EmbeddingSpeakerRegistry",
    "SpeakerProfile",
    "SpeakerMatch",
]
