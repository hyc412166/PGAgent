"""Process-scoped MCP tool catalog cache used by lazy startup."""

from __future__ import annotations

import json
import time
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import McpServerConfig


TOOL_CATALOG_CACHE_CAPACITY = 32
TOOL_CATALOG_CACHE_TTL_SEC = 30 * 60


@dataclass(frozen=True, slots=True)
class CachedMcpTool:
    raw_name: str
    description: str
    input_schema: dict[str, Any]
    read_only: bool


@dataclass(frozen=True, slots=True)
class ToolCatalogIdentity:
    server_name: str
    workspace_root: str
    expanded_config: str

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


@dataclass(slots=True)
class _CacheEntry:
    tools: tuple[CachedMcpTool, ...]
    published_at: float


class McpToolCatalogCache:
    """Small LRU mirroring Codex's process-scoped, expiring catalog cache."""

    def __init__(
        self,
        *,
        capacity: int = TOOL_CATALOG_CACHE_CAPACITY,
        ttl_sec: float = TOOL_CATALOG_CACHE_TTL_SEC,
    ) -> None:
        self.capacity = capacity
        self.ttl_sec = ttl_sec
        self._entries: OrderedDict[ToolCatalogIdentity, _CacheEntry] = OrderedDict()

    def get(self, identity: ToolCatalogIdentity) -> tuple[CachedMcpTool, ...] | None:
        entry = self._entries.get(identity)
        if entry is None:
            return None
        if time.monotonic() - entry.published_at > self.ttl_sec:
            del self._entries[identity]
            return None
        self._entries.move_to_end(identity)
        return entry.tools

    def put(self, identity: ToolCatalogIdentity, tools: tuple[CachedMcpTool, ...]) -> None:
        self._entries[identity] = _CacheEntry(tools=tools, published_at=time.monotonic())
        self._entries.move_to_end(identity)
        while len(self._entries) > self.capacity:
            self._entries.popitem(last=False)

    def remove(self, identity: ToolCatalogIdentity) -> None:
        self._entries.pop(identity, None)

    def clear(self) -> None:
        self._entries.clear()

    def __len__(self) -> int:
        return len(self._entries)
