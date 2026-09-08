"""Process-scoped MCP tool catalog cache used by lazy startup."""
# 文件职责：负责MCP 外部工具接入中的 tool_catalog_cache 子模块。
# 逻辑关系：上层通过 mcp/tool_catalog_cache.py 使用本模块；本模块把处理结果交给同领域服务、持久化层或 API 响应层。

from __future__ import annotations

import json
import time
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import McpServerConfig


# 变量说明：TOOL_CATALOG_CACHE_CAPACITY 表示当前步骤使用的 TOOL_CATALOG_CACHE_CAPACITY 值。
TOOL_CATALOG_CACHE_CAPACITY = 32
# 变量说明：TOOL_CATALOG_CACHE_TTL_SEC 表示当前步骤使用的 TOOL_CATALOG_CACHE_TTL_SEC 值。
TOOL_CATALOG_CACHE_TTL_SEC = 30 * 60


# 类职责：定义 CachedMcpTool 在本领域中的数据与行为。
@dataclass(frozen=True, slots=True)
class CachedMcpTool:
    # 变量说明：raw_name 表示当前步骤使用的 raw_name 值。
    raw_name: str
    # 变量说明：description 表示当前步骤使用的 description 值。
    description: str
    # 变量说明：input_schema 表示当前步骤使用的 input_schema 值。
    input_schema: dict[str, Any]
    # 变量说明：read_only 表示当前步骤使用的 read_only 值。
    read_only: bool


# 类职责：定义 ToolCatalogIdentity 在本领域中的数据与行为。
@dataclass(frozen=True, slots=True)
class ToolCatalogIdentity:
    # 变量说明：server_name 表示当前步骤使用的 server_name 值。
    server_name: str
    # 变量说明：workspace_root 表示当前步骤使用的 workspace_root 值。
    workspace_root: str
    # 变量说明：expanded_config 表示当前步骤使用的 expanded_config 值。
    expanded_config: str

    # 函数职责：完成 create 对应的业务处理。
    # 参数关系：server_name 表示当前步骤使用的 server_name 值；config 表示当前生效的配置；workspace_root 表示当前步骤使用的 workspace_root 值。
    # 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
    @classmethod
    def create(
        cls,
        server_name: str,
        config: McpServerConfig,
        workspace_root: Path,
    ) -> "ToolCatalogIdentity":
        return cls(
            server_name=server_name,
            workspace_root=str(workspace_root.resolve()),
            expanded_config=json.dumps(
                config.model_dump(mode="json", exclude_none=True),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
        )


# 类职责：定义 _CacheEntry 在本领域中的数据与行为。
@dataclass(slots=True)
class _CacheEntry:
    # 变量说明：tools 表示本轮可调用的工具集合。
    tools: tuple[CachedMcpTool, ...]
    # 变量说明：published_at 表示published_at 对应的时间信息。
    published_at: float


# 类职责：定义 McpToolCatalogCache 在本领域中的数据与行为。
class McpToolCatalogCache:
    """Small LRU mirroring Codex's process-scoped, expiring catalog cache."""

    # 函数职责：初始化实例依赖与初始状态。
    # 参数关系：capacity 表示当前步骤使用的 capacity 值；ttl_sec 表示当前步骤使用的 ttl_sec 值。
    # 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
    def __init__(
        self,
        *,
        capacity: int = TOOL_CATALOG_CACHE_CAPACITY,
        ttl_sec: float = TOOL_CATALOG_CACHE_TTL_SEC,
    ) -> None:
        # 变量说明：capacity 表示当前步骤使用的 capacity 值。
        self.capacity = capacity
        # 变量说明：ttl_sec 表示当前步骤使用的 ttl_sec 值。
        self.ttl_sec = ttl_sec
        # 变量说明：_entries 表示当前流程使用的 _entries 集合。
        self._entries: OrderedDict[ToolCatalogIdentity, _CacheEntry] = OrderedDict()

    # 函数职责：完成 get 对应的业务处理。
    # 参数关系：identity 表示当前步骤使用的 identity 值。
    # 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
    def get(self, identity: ToolCatalogIdentity) -> tuple[CachedMcpTool, ...] | None:
        # 变量说明：entry 表示当前步骤使用的 entry 值。
        entry = self._entries.get(identity)
        if entry is None:
            return None
        if time.monotonic() - entry.published_at > self.ttl_sec:
            del self._entries[identity]
            return None
        self._entries.move_to_end(identity)
        return entry.tools

    # 函数职责：完成 put 对应的业务处理。
    # 参数关系：identity 表示当前步骤使用的 identity 值；tools 表示本轮可调用的工具集合。
    # 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
    def put(self, identity: ToolCatalogIdentity, tools: tuple[CachedMcpTool, ...]) -> None:
        # 变量说明：映射 的索引项 表示该语句创建或更新的目标数据。
        self._entries[identity] = _CacheEntry(tools=tools, published_at=time.monotonic())
        self._entries.move_to_end(identity)
        while len(self._entries) > self.capacity:
            self._entries.popitem(last=False)

    # 函数职责：完成 remove 对应的业务处理。
    # 参数关系：identity 表示当前步骤使用的 identity 值。
    # 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
    def remove(self, identity: ToolCatalogIdentity) -> None:
        self._entries.pop(identity, None)

    # 函数职责：完成 clear 对应的业务处理。
    # 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
    def clear(self) -> None:
        self._entries.clear()

    # 函数职责：完成 len 对应的业务处理。
    # 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
    def __len__(self) -> int:
        return len(self._entries)
