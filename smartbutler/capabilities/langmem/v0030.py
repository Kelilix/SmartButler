"""langmem 0.0.30 具体实现(Phase 6.2 P0)。

软依赖: import 失败抛 ``LangmemError``(由工厂捕获切 fallback)。

0.0.30 的事实抽取 API 形态(我们走最稳的)::
    from langmem import create_manage_memory
    # 或者直接用 create_memory_store_manager(需要 store)
我们这里只抽事实,不存,所以直接走对话 → facts 的最简路径。

**注意**: 0.0.30 API 还在 beta,具体函数名可能微调,本文件是软依赖门面,
``try-import`` 失败就整体抛 ``LangmemError`` 让工厂切 fallback。
"""

from __future__ import annotations

from collections.abc import Sequence

from smartbutler.capabilities.langmem.base import (
    BaseLangmemAdapter,
    ExtractedFact,
    LangmemError,
)
from smartbutler.utils.logging import get_logger

logger = get_logger(__name__)


class Langmem0030Adapter(BaseLangmemAdapter):
    """langmem 0.0.30 适配器。"""

    def __init__(self) -> None:
        try:
            import langmem  # noqa: F401  软依赖校验
        except ImportError as exc:
            raise LangmemError(
                f"langmem 0.0.30 未安装,无法构造 Langmem0030Adapter: {exc}"
            ) from exc

    async def extract_facts(
        self, conversation: Sequence[dict[str, str]]
    ) -> list[ExtractedFact]:
        """从对话抽事实。

        0.0.30 的 ``create_manage_memory`` 需要 ``BaseStore``,Phase 6.2 P0 暂不接
        (我们只想抽 facts,不想直接存)。这里走最简路径: 用 langmem 内部的
        ``create_memory_store_manager`` 工具函数,只调抽取 prompt。

        如果 0.0.30 没暴露这种"只抽不存"函数,降级为 0.0.30 提供的基础 ``extract``。
        0.0.30 真正暴露的 API 是 ``langmem.create_memory_store_manager``,完整调用需要
        store,不符合 P0 最小诉求,所以这里**只确保 import 成功** + 抛清晰错误,
        让上层切 fallback。

        后续 langmem 稳定后,这里替换为真实抽事实调用。
        """
        if not conversation:
            return []
        # 0.0.30 真正稳定的纯抽事实 API 还没稳定暴露;先让上层走 fallback。
        # 0.0.30 的 store-bound manager 需要 BaseStore,我们的 QdrantStorage
        # 不是 BaseStore,接不进来。
        raise LangmemError(
            "langmem 0.0.30 的事实抽取 API 仍需 BaseStore,Phase 6.2 P0 暂走 fallback。"
            "后续 langmem 0.1.x 稳定后,本方法替换为真实抽事实调用。"
        )


__all__ = ["Langmem0030Adapter"]
