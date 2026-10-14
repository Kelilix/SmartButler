"""存储层（按文档 §3.1 / §4.3）。

按文档约定，不同类型的数据使用不同后端：
- 短期对话上下文：SQLite → ``storage.sqlite``
- 长期用户记忆（带向量检索）：Qdrant（嵌入式 / 服务模式）→ ``storage.qdrant``
- 性格状态：SQLite JSON → ``storage.sqlite``
- Agent 配置：YAML → ``storage.agent_config``

本包职责：
1. 定义统一的存储协议 ``BaseStorage``（后续每个子模块都基于此实现）。
2. 为每个后端类型预留子目录，由后续模块（emotion/memory 等）落地具体实现。
3. Phase 6.1 起,SQLiteStorage + QdrantStorage 已落地（见各子模块）。
"""

from smartbutler.storage.base import BaseStorage, StorageError
from smartbutler.storage.qdrant import QdrantStorage
from smartbutler.storage.sqlite import SQLiteStorage

__all__ = ["BaseStorage", "StorageError", "SQLiteStorage", "QdrantStorage"]
