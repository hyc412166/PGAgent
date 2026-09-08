"""MCP connections, tool discovery, routing, and session ownership."""
# 文件职责：负责MCP 外部工具接入中的 runtime 子模块。
# 逻辑关系：上层通过 mcp/runtime.py 使用本模块；本模块把处理结果交给同领域服务、持久化层或 API 响应层。

from __future__ import annotations

import asyncio
import json
import re
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Literal, TypeVar

import httpx2
from mcp import Client
from mcp.client.stdio import StdioServerParameters
from mcp.client.streamable_http import streamable_http_client

from src.config import settings
from src.tools.types import ToolResult

from .config import McpConfig, McpServerConfig, load_mcp_config
from .tool_catalog_cache import CachedMcpTool, McpToolCatalogCache, ToolCatalogIdentity


# 变量说明：_INVALID_TOOL_NAME 表示当前步骤使用的 _INVALID_TOOL_NAME 值。
_INVALID_TOOL_NAME = re.compile(r"[^A-Za-z0-9_-]+")
# 变量说明：_MAX_MODEL_TOOL_NAME 表示当前步骤使用的 _MAX_MODEL_TOOL_NAME 值。
_MAX_MODEL_TOOL_NAME = 64
# 变量说明：_ConfigurationResult 表示当前步骤使用的 _ConfigurationResult 值。
_ConfigurationResult = TypeVar("_ConfigurationResult")
# 变量说明：McpStartupPolicy 表示当前步骤使用的 McpStartupPolicy 值。
McpStartupPolicy = Literal["eager", "lazy_when_cached"]


# 函数职责：完成 model_dump 对应的业务处理。
# 参数关系：value 表示当前字段或计算值。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def _model_dump(value: object) -> object:
    # 变量说明：method 表示当前步骤使用的 method 值。
    method = getattr(value, "model_dump", None)
    if callable(method):
        return method(mode="json", by_alias=True, exclude_none=True)
    return value


# 函数职责：完成 public_server_info 对应的业务处理。
# 参数关系：value 表示当前字段或计算值。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def _public_server_info(value: object) -> dict[str, str] | None:
    # 变量说明：dumped 表示当前步骤使用的 dumped 值。
    dumped = _model_dump(value)
    if not isinstance(dumped, dict):
        return None
    # 变量说明：public 表示当前步骤使用的 public 值。
    public = {
        key: str(dumped[key])
        for key in ("name", "version")
        if isinstance(dumped.get(key), str)
    }
    return public or None


# 函数职责：完成 sanitize_tool_part 对应的业务处理。
# 参数关系：value 表示当前字段或计算值；fallback 表示当前步骤使用的 fallback 值。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def _sanitize_tool_part(value: str, fallback: str) -> str:
    # 变量说明：normalized 表示当前步骤使用的 normalized 值。
    normalized = _INVALID_TOOL_NAME.sub("_", value).strip("_")
    return normalized or fallback


# 类职责：定义 McpToolBinding 在本领域中的数据与行为。
@dataclass(frozen=True, slots=True)
class McpToolBinding:
    # 变量说明：server_name 表示当前步骤使用的 server_name 值。
    server_name: str
    # 变量说明：raw_name 表示当前步骤使用的 raw_name 值。
    raw_name: str
    # 变量说明：model_name 表示当前步骤使用的 model_name 值。
    model_name: str
    # 变量说明：description 表示当前步骤使用的 description 值。
    description: str
    # 变量说明：input_schema 表示当前步骤使用的 input_schema 值。
    input_schema: dict[str, Any]
    # 变量说明：read_only 表示当前步骤使用的 read_only 值。
    read_only: bool
    # 变量说明：supports_parallel 表示当前步骤使用的 supports_parallel 值。
    supports_parallel: bool

    # 函数职责：完成 snapshot 对应的业务处理。
    # 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
    def snapshot(self) -> dict[str, Any]:
        return {
            "server": self.server_name,
            "tool": self.raw_name,
            "model_name": self.model_name,
            "description": self.description,
            "input_schema": self.input_schema,
            "read_only": self.read_only,
            "supports_parallel": self.supports_parallel,
        }


# 类职责：定义 ConnectedMcpServer 在本领域中的数据与行为。
@dataclass(slots=True)
class ConnectedMcpServer:
    # 变量说明：name 表示当前对象名称。
    name: str
    # 变量说明：config 表示当前生效的配置。
    config: McpServerConfig
    # 变量说明：workspace_root 表示当前步骤使用的 workspace_root 值。
    workspace_root: Path
    # 变量说明：client 表示访问外部服务的客户端。
    client: Client | None = None
    # 变量说明：error 表示当前捕获或准备上报的错误。
    error: str | None = None
    # 变量说明：error_kind 表示当前步骤使用的 error_kind 值。
    error_kind: str | None = None
    # 变量说明：protocol_version 表示当前步骤使用的 protocol_version 值。
    protocol_version: str | None = None
    # 变量说明：server_info 表示当前步骤使用的 server_info 值。
    server_info: dict[str, Any] | None = None
    # 变量说明：tools 表示本轮可调用的工具集合。
    tools: list[object] = field(default_factory=list)
    # 变量说明：call_lock 表示当前步骤使用的 call_lock 值。
    call_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    # 变量说明：startup_lock 表示当前步骤使用的 startup_lock 值。
    startup_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    # 变量说明：catalog_identity 表示当前步骤使用的 catalog_identity 值。
    catalog_identity: ToolCatalogIdentity | None = None
    # 变量说明：cached_tools 表示当前流程使用的 cached_tools 集合。
    cached_tools: tuple[CachedMcpTool, ...] = ()
    # 变量说明：dormant 表示当前步骤使用的 dormant 值。
    dormant: bool = False
    # 变量说明：catalog_changed 表示当前步骤使用的 catalog_changed 值。
    catalog_changed: bool = False
    # 变量说明：catalog_cacheable 表示当前步骤使用的 catalog_cacheable 值。
    catalog_cacheable: bool = True
    # 变量说明：ready 表示当前步骤使用的 ready 值。
    ready: bool = False
    # 变量说明：closed 表示当前步骤使用的 closed 值。
    closed: bool = False
    # 变量说明：_close_event 表示当前步骤使用的 _close_event 值。
    _close_event: asyncio.Event | None = None
    # 变量说明：_owner_task 表示当前步骤使用的 _owner_task 值。
    _owner_task: asyncio.Task[None] | None = None

    # 函数职责：异步完成 start 对应的业务处理。
    # 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
    async def start(self) -> None:
        if self.closed:
            raise RuntimeError(f"MCP server {self.name!r} is closed")
        # 变量说明：dormant 表示当前步骤使用的 dormant 值。
        self.dormant = False
        # 变量说明：ready 表示当前步骤使用的 ready 值。
        self.ready = False
        # 变量说明：error 表示当前捕获或准备上报的错误。
        self.error = None
        # 变量说明：error_kind 表示当前步骤使用的 error_kind 值。
        self.error_kind = None
        # 变量说明：ready 表示当前步骤使用的 ready 值。
        ready: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        # 变量说明：close_event 表示当前步骤使用的 close_event 值。
        close_event = asyncio.Event()
        # 变量说明：_close_event 表示当前步骤使用的 _close_event 值。
        self._close_event = close_event
        # 变量说明：_owner_task 表示当前步骤使用的 _owner_task 值。
        self._owner_task = asyncio.create_task(
            self._run_connection(ready, close_event),
            name=f"mcp-{self.name}",
        )
        try:
            await ready
        except asyncio.CancelledError:
            # 变量说明：task 表示当前步骤使用的 task 值；_owner_task 表示当前步骤使用的 _owner_task 值。
            task, self._owner_task = self._owner_task, None
            # Awaiting-task cancellation marks the Future itself cancelled.
            # A CancelledError published by the owner leaves it non-cancelled
            # and must be allowed to finish its own transport unwind.
            if ready.cancelled() and task is not None and not task.done():
                task.cancel()
            if task is not None:
                await asyncio.gather(task, return_exceptions=True)
            raise
        except BaseException:
            # 变量说明：task 表示当前步骤使用的 task 值；_owner_task 表示当前步骤使用的 _owner_task 值。
            task, self._owner_task = self._owner_task, None
            if task is not None:
                # The owner already began unwinding its transport after
                # publishing this startup error. A second cancellation here
                # could interrupt AsyncExitStack.aclose() and leak stdio.
                await asyncio.gather(task, return_exceptions=True)
            raise

    # 函数职责：异步确保 started 对应的数据或流程。
    # 参数关系：accept_catalog 表示当前步骤使用的 accept_catalog 值。
    # 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
    async def ensure_started(self, accept_catalog: Callable[[], None]) -> None:
        if self.closed:
            raise RuntimeError(f"MCP server {self.name!r} is closed")
        if self.ready:
            return
        async with self.startup_lock:
            if self.closed:
                raise RuntimeError(f"MCP server {self.name!r} is closed")
            if self.ready:
                return
            await asyncio.wait_for(self.start(), timeout=self.config.startup_timeout_sec)
            if self.closed:
                raise RuntimeError(f"MCP server {self.name!r} was closed during startup")
            if self.client is None:
                raise RuntimeError(f"MCP server {self.name!r} finished startup without a client")
            accept_catalog()
            # 变量说明：ready 表示当前步骤使用的 ready 值。
            self.ready = True

    # 函数职责：异步执行 connection 对应的数据或流程。
    # 参数关系：ready 表示当前步骤使用的 ready 值；close_event 表示当前步骤使用的 close_event 值。
    # 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
    async def _run_connection(
        self,
        ready: asyncio.Future[None],
        close_event: asyncio.Event,
    ) -> None:
        """Enter and exit SDK transports in their single owning task."""

        # 变量说明：stack 表示当前步骤使用的 stack 值。
        stack = AsyncExitStack()
        try:
            if self.config.command:
                # 变量说明：cwd 表示当前步骤使用的 cwd 值。
                cwd = Path(self.config.cwd) if self.config.cwd else self.workspace_root
                if not cwd.is_absolute():
                    # 变量说明：cwd 表示当前步骤使用的 cwd 值。
                    cwd = self.workspace_root / cwd
                # 变量说明：target 表示当前步骤使用的 target 值。
                target: object = StdioServerParameters(
                    command=self.config.command,
                    args=list(self.config.args),
                    env=dict(self.config.env) or None,
                    cwd=cwd,
                )
            else:
                # 变量说明：http_client 表示当前步骤使用的 http_client 值。
                http_client = await stack.enter_async_context(
                    httpx2.AsyncClient(headers=dict(self.config.headers), follow_redirects=False)
                )
                # 变量说明：target 表示当前步骤使用的 target 值。
                target = streamable_http_client(str(self.config.url), http_client=http_client)
            # PGAgent owns one observable timeout boundary around each call.
            # Leaving the SDK timer disabled avoids competing cancellations.
            # 变量说明：client 表示访问外部服务的客户端。
            client = Client(target, read_timeout_seconds=None)
            # 变量说明：client 表示访问外部服务的客户端。
            self.client = await stack.enter_async_context(client)
            # 变量说明：protocol_version 表示当前步骤使用的 protocol_version 值。
            self.protocol_version = str(client.protocol_version)
            # 变量说明：info 表示当前步骤使用的 info 值。
            info = client.server_info
            # 变量说明：dumped_info 表示当前步骤使用的 dumped_info 值。
            dumped_info = _model_dump(info) if info is not None else None
            # 变量说明：server_info 表示当前步骤使用的 server_info 值。
            self.server_info = dumped_info if isinstance(dumped_info, dict) else None
            # 变量说明：capabilities 表示当前流程使用的 capabilities 集合。
            capabilities = getattr(client, "server_capabilities", None)
            # 变量说明：experimental 表示当前步骤使用的 experimental 值。
            experimental = getattr(capabilities, "experimental", None) or {}
            # 变量说明：cache_capability 表示当前步骤使用的 cache_capability 值。
            cache_capability = experimental.get("codex/tool-catalog-cache", {})
            # 变量说明：catalog_cacheable 表示当前步骤使用的 catalog_cacheable 值。
            self.catalog_cacheable = not (
                isinstance(cache_capability, dict) and cache_capability.get("cacheable") is False
            )
            await self.refresh_tools()
            ready.set_result(None)
            await close_event.wait()
        except BaseException as exc:
            if not ready.done():
                ready.set_exception(exc)
            elif not isinstance(exc, asyncio.CancelledError):
                # 变量说明：error 表示当前捕获或准备上报的错误。
                self.error = str(exc) or type(exc).__name__
                # 变量说明：error_kind 表示当前步骤使用的 error_kind 值。
                self.error_kind = type(exc).__name__
            raise
        finally:
            # 变量说明：ready 表示当前步骤使用的 ready 值。
            self.ready = False
            # 变量说明：client 表示访问外部服务的客户端。
            self.client = None
            await stack.aclose()

    # 函数职责：异步完成 refresh_tools 对应的业务处理。
    # 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
    async def refresh_tools(self) -> None:
        if self.client is None:
            return
        # 变量说明：cursor 表示当前步骤使用的 cursor 值。
        cursor: str | None = None
        # 变量说明：discovered 表示当前步骤使用的 discovered 值。
        discovered: list[object] = []
        while True:
            # 变量说明：result 表示本步骤产生的结果。
            result = await self.client.list_tools(cursor=cursor)
            discovered.extend(result.tools)
            # 变量说明：cursor 表示当前步骤使用的 cursor 值。
            cursor = getattr(result, "next_cursor", None)
            if not cursor:
                break
        # 变量说明：tools 表示本轮可调用的工具集合。
        self.tools = [tool for tool in discovered if self.config.allows_tool(str(getattr(tool, "name", "")))]

    # 函数职责：完成 current_catalog 对应的业务处理。
    # 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
    def current_catalog(self) -> tuple[CachedMcpTool, ...]:
        if self.client is None:
            return self.cached_tools
        # 变量说明：items 表示待处理的元素集合。
        items = [
            CachedMcpTool(
                raw_name=str(getattr(tool, "name", "")),
                description=str(getattr(tool, "description", "") or f"MCP tool {self.name}/{getattr(tool, 'name', '')}"),
                input_schema=(
                    dumped if isinstance((dumped := _model_dump(getattr(tool, "input_schema", None))), dict)
                    else {"type": "object", "properties": {}}
                ),
                read_only=bool(getattr(getattr(tool, "annotations", None), "read_only_hint", False)),
            )
            for tool in self.tools
        ]
        return tuple(sorted(items, key=lambda item: item.raw_name))

    # 函数职责：异步完成 close 对应的业务处理。
    # 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
    async def close(self) -> None:
        # 变量说明：closed 表示当前步骤使用的 closed 值。
        self.closed = True
        async with self.startup_lock:
            # 变量说明：event 表示当前运行事件；_close_event 表示当前步骤使用的 _close_event 值。
            event, self._close_event = self._close_event, None
            # 变量说明：task 表示当前步骤使用的 task 值；_owner_task 表示当前步骤使用的 _owner_task 值。
            task, self._owner_task = self._owner_task, None
            if event is not None:
                event.set()
            if task is not None:
                await asyncio.gather(task, return_exceptions=True)
            # 变量说明：ready 表示当前步骤使用的 ready 值。
            self.ready = False
            # 变量说明：client 表示访问外部服务的客户端。
            self.client = None


# 类职责：协调 McpSessionRuntime 负责的业务流程与依赖。
class McpSessionRuntime:
    """Connections and the latest catalog owned by one PGAgent session."""

    # 函数职责：初始化实例依赖与初始状态。
    # 参数关系：session_key 表示当前步骤使用的 session_key 值；workspace_root 表示当前步骤使用的 workspace_root 值；config 表示当前生效的配置；tool_catalog_cache 表示当前步骤使用的 tool_catalog_cache 值；startup_policy 表示当前步骤使用的 startup_policy 值；runtime_scope 表示当前步骤使用的 runtime_scope 值。
    # 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
    def __init__(
        self,
        session_key: str,
        workspace_root: Path,
        config: McpConfig,
        tool_catalog_cache: McpToolCatalogCache | None = None,
        *,
        startup_policy: McpStartupPolicy = "eager",
        runtime_scope: str | None = None,
    ) -> None:
        # 变量说明：session_key 表示当前步骤使用的 session_key 值。
        self.session_key = session_key
        # 变量说明：runtime_scope 表示当前步骤使用的 runtime_scope 值。
        self.runtime_scope = runtime_scope or session_key
        # 变量说明：workspace_root 表示当前步骤使用的 workspace_root 值。
        self.workspace_root = workspace_root
        # 变量说明：config 表示当前生效的配置。
        self.config = config
        # 变量说明：startup_policy 表示当前步骤使用的 startup_policy 值。
        self.startup_policy = startup_policy
        # 变量说明：tool_catalog_cache 表示当前步骤使用的 tool_catalog_cache 值。
        self.tool_catalog_cache = tool_catalog_cache if tool_catalog_cache is not None else McpToolCatalogCache()
        # 变量说明：servers 表示当前流程使用的 servers 集合。
        self.servers: dict[str, ConnectedMcpServer] = {}

    # 函数职责：异步完成 start 对应的业务处理。
    # 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
    async def start(self) -> None:
        # 变量说明：enabled 表示当前步骤使用的 enabled 值。
        enabled = [
            (name, config)
            for name, config in sorted(self.config.servers.items())
            if config.enabled
        ]
        # 变量说明：servers 表示当前流程使用的 servers 集合。
        servers: list[ConnectedMcpServer] = []
        # 变量说明：eager_servers 表示当前流程使用的 eager_servers 集合。
        eager_servers: list[ConnectedMcpServer] = []
        for name, server_config in enabled:
            # 变量说明：identity 表示当前步骤使用的 identity 值。
            identity = ToolCatalogIdentity.create(name, server_config, self.workspace_root)
            # 变量说明：cached_tools 表示当前流程使用的 cached_tools 集合。
            cached_tools = self.tool_catalog_cache.get(identity)
            # 变量说明：server 表示当前步骤使用的 server 值。
            server = ConnectedMcpServer(
                name,
                server_config,
                self.workspace_root,
                catalog_identity=identity,
                cached_tools=cached_tools or (),
            )
            # 变量说明：can_defer 表示表示是否满足 _defer 条件的布尔标记。
            can_defer = (
                self.startup_policy == "lazy_when_cached"
                and bool(cached_tools)
            )
            # 变量说明：dormant 表示当前步骤使用的 dormant 值。
            server.dormant = can_defer
            servers.append(server)
            if not can_defer:
                eager_servers.append(server)
        # 变量说明：results 表示批量处理结果集合。
        results = await asyncio.gather(
            *(asyncio.wait_for(server.start(), timeout=server.config.startup_timeout_sec) for server in eager_servers),
            return_exceptions=True,
        )
        # 变量说明：required_errors 表示当前流程使用的 required_errors 集合。
        required_errors: list[str] = []
        for server in servers:
            self.servers[server.name] = server
        for server, result in zip(eager_servers, results, strict=True):
            if isinstance(result, BaseException):
                # 变量说明：error 表示当前捕获或准备上报的错误。
                server.error = str(result) or type(result).__name__
                # 变量说明：error_kind 表示当前步骤使用的 error_kind 值。
                server.error_kind = type(result).__name__
                if server.config.required:
                    required_errors.append(f"{server.name}: {server.error}")
            else:
                self._accept_started_server(server, server.cached_tools)
                # 变量说明：ready 表示当前步骤使用的 ready 值。
                server.ready = server.client is not None
        if required_errors:
            await self.close()
            raise RuntimeError("required MCP servers failed to initialize: " + "; ".join(required_errors))

    # 函数职责：完成 publish_server_catalog 对应的业务处理。
    # 参数关系：server 表示当前步骤使用的 server 值。
    # 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
    def _publish_server_catalog(self, server: ConnectedMcpServer) -> None:
        # 变量说明：catalog 表示当前步骤使用的 catalog 值。
        catalog = server.current_catalog()
        # 变量说明：cached_tools 表示当前流程使用的 cached_tools 集合。
        server.cached_tools = catalog
        if server.catalog_identity is not None:
            if server.catalog_cacheable:
                self.tool_catalog_cache.put(server.catalog_identity, catalog)
            else:
                self.tool_catalog_cache.remove(server.catalog_identity)

    # 函数职责：完成 accept_started_server 对应的业务处理。
    # 参数关系：server 表示当前步骤使用的 server 值；previous_catalog 表示当前步骤使用的 previous_catalog 值。
    # 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
    def _accept_started_server(
        self,
        server: ConnectedMcpServer,
        previous_catalog: tuple[CachedMcpTool, ...],
    ) -> None:
        # 变量说明：live_catalog 表示当前步骤使用的 live_catalog 值。
        live_catalog = server.current_catalog()
        # 变量说明：error 表示当前捕获或准备上报的错误。
        server.error = None
        # 变量说明：error_kind 表示当前步骤使用的 error_kind 值。
        server.error_kind = None
        # 变量说明：catalog_changed 表示当前步骤使用的 catalog_changed 值。
        server.catalog_changed = bool(previous_catalog) and live_catalog != previous_catalog
        self._publish_server_catalog(server)

    # 函数职责：完成 bindings 对应的业务处理。
    # 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
    def bindings(self) -> list[McpToolBinding]:
        # 变量说明：candidates 表示当前流程使用的 candidates 集合。
        candidates: list[tuple[ConnectedMcpServer, CachedMcpTool]] = []
        for server in self.servers.values():
            candidates.extend((server, tool) for tool in server.current_catalog())
        candidates.sort(key=lambda item: (item[0].name, item[1].raw_name))

        # 变量说明：used_names 表示当前流程使用的 used_names 集合。
        used_names: set[str] = set()
        # 变量说明：bindings 表示当前流程使用的 bindings 集合。
        bindings: list[McpToolBinding] = []
        for server, tool in candidates:
            # 变量说明：raw_name 表示当前步骤使用的 raw_name 值。
            raw_name = tool.raw_name
            # 变量说明：server_part 表示当前步骤使用的 server_part 值。
            server_part = _sanitize_tool_part(server.name, "server")
            # 变量说明：tool_part 表示当前步骤使用的 tool_part 值。
            tool_part = _sanitize_tool_part(raw_name, "tool")
            # 变量说明：base 表示当前步骤使用的 base 值。
            base = f"mcp__{server_part}__{tool_part}"[:_MAX_MODEL_TOOL_NAME]
            # 变量说明：model_name 表示当前步骤使用的 model_name 值。
            model_name = base
            # 变量说明：suffix 表示当前步骤使用的 suffix 值。
            suffix = 2
            while model_name in used_names:
                # 变量说明：marker 表示当前步骤使用的 marker 值。
                marker = f"_{suffix}"
                # 变量说明：model_name 表示当前步骤使用的 model_name 值。
                model_name = base[: _MAX_MODEL_TOOL_NAME - len(marker)] + marker
                suffix += 1
            used_names.add(model_name)
            bindings.append(McpToolBinding(
                server_name=server.name,
                raw_name=raw_name,
                model_name=model_name,
                description=tool.description,
                input_schema=tool.input_schema,
                read_only=tool.read_only,
                supports_parallel=server.config.supports_parallel_tool_calls and tool.read_only,
            ))
        return bindings

    # 函数职责：异步完成 refresh_tools 对应的业务处理。
    # 参数关系：reconnect_failed 表示当前步骤使用的 reconnect_failed 值。
    # 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
    async def refresh_tools(self, *, reconnect_failed: bool = False) -> list[McpToolBinding]:
        """Return the current catalog, retrying failed clients without refreshing ready ones."""

        if reconnect_failed:
            for server in self.servers.values():
                if server.ready or server.dormant:
                    continue
                # 变量说明：previous_catalog 表示当前步骤使用的 previous_catalog 值。
                previous_catalog = server.cached_tools
                try:
                    await server.ensure_started(
                        lambda server=server, previous_catalog=previous_catalog: self._accept_started_server(
                            server,
                            previous_catalog,
                        )
                    )
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    # 变量说明：error 表示当前捕获或准备上报的错误。
                    server.error = str(exc) or type(exc).__name__
                    # 变量说明：error_kind 表示当前步骤使用的 error_kind 值。
                    server.error_kind = type(exc).__name__
                    if server.config.required:
                        raise RuntimeError(
                            f"required MCP server {server.name!r} could not reconnect: {server.error}"
                        ) from exc
        return self.bindings()

    # 函数职责：异步确保 server_started 对应的数据或流程。
    # 参数关系：server_name 表示当前步骤使用的 server_name 值。
    # 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
    async def ensure_server_started(self, server_name: str) -> ConnectedMcpServer | None:
        # 变量说明：server 表示当前步骤使用的 server 值。
        server = self.servers.get(server_name)
        if server is None:
            return None
        if server.ready and not server.closed:
            return server
        # 变量说明：previous_catalog 表示当前步骤使用的 previous_catalog 值。
        previous_catalog = server.cached_tools
        try:
            await server.ensure_started(
                lambda: self._accept_started_server(server, previous_catalog)
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # 变量说明：error 表示当前捕获或准备上报的错误。
            server.error = str(exc) or type(exc).__name__
            # 变量说明：error_kind 表示当前步骤使用的 error_kind 值。
            server.error_kind = type(exc).__name__
            return server
        return server

    # 函数职责：完成 accept_current_catalog 对应的业务处理。
    # 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
    def accept_current_catalog(self) -> None:
        """A new run may use the refreshed live catalog after an old snapshot was rejected."""

        for server in self.servers.values():
            # 变量说明：catalog_changed 表示当前步骤使用的 catalog_changed 值。
            server.catalog_changed = False

    # 函数职责：异步完成 call_tool 对应的业务处理。
    # 参数关系：server_name 表示当前步骤使用的 server_name 值；tool_name 表示当前步骤使用的 tool_name 值；arguments 表示当前流程使用的 arguments 集合。
    # 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
    async def call_tool(self, server_name: str, tool_name: str, arguments: dict[str, Any]) -> ToolResult:
        # 变量说明：server 表示当前步骤使用的 server 值。
        server = await self.ensure_server_started(server_name)
        if server is None or server.client is None or not server.ready or server.closed:
            # 变量说明：detail 表示当前步骤使用的 detail 值。
            detail = f": {server.error}" if server is not None and server.error else ""
            return ToolResult(tool_name, False, f"MCP server {server_name!r} could not start{detail}", error_code="mcp_not_connected")
        if server.catalog_changed:
            return ToolResult(
                tool_name,
                False,
                "MCP tool catalog changed while waking the server; start a new run to use the refreshed definitions",
                error_code="mcp_catalog_changed",
            )
        if not server.config.allows_tool(tool_name):
            return ToolResult(tool_name, False, f"MCP tool {server_name}/{tool_name} is disabled", error_code="mcp_tool_disabled")
        try:
            async with server.call_lock if not server.config.supports_parallel_tool_calls else _null_async_context():
                # 变量说明：result 表示本步骤产生的结果。
                result = await asyncio.wait_for(
                    server.client.call_tool(tool_name, arguments, read_timeout_seconds=None),
                    timeout=server.config.tool_timeout_sec,
                )
        except asyncio.TimeoutError:
            return ToolResult(tool_name, False, "MCP tool call timed out", error_code="mcp_timeout")
        except Exception as exc:
            return ToolResult(tool_name, False, f"MCP tool call failed: {type(exc).__name__}: {exc}", error_code="mcp_tool_error")

        # 变量说明：content 表示待处理或返回的正文内容。
        content = [_model_dump(item) for item in result.content]
        # 变量说明：structured 表示当前步骤使用的 structured 值。
        structured = _model_dump(getattr(result, "structured_content", None))
        # 变量说明：payload 表示跨层传递的数据载荷。
        payload: dict[str, Any] = {"content": content}
        if structured is not None:
            payload["structured_content"] = structured
        # 变量说明：text_parts 表示当前流程使用的 text_parts 集合。
        text_parts = [
            str(item.get("text"))
            for item in content
            if isinstance(item, dict) and item.get("type") == "text" and isinstance(item.get("text"), str)
        ]
        # 变量说明：rendered 表示当前步骤使用的 rendered 值。
        rendered = "\n".join(text_parts) if len(text_parts) == len(content) and text_parts else json.dumps(payload, ensure_ascii=False)
        # 变量说明：is_error 表示表示是否满足 error 条件的布尔标记。
        is_error = bool(getattr(result, "is_error", False))
        return ToolResult(
            tool_name,
            not is_error,
            rendered,
            error_code="mcp_result_error" if is_error else None,
            metadata={"server": server_name, "raw_tool_name": tool_name, "structured_content": structured},
        )

    # 函数职责：异步列出 resources 对应的数据或流程。
    # 参数关系：server_name 表示当前步骤使用的 server_name 值。
    # 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
    async def list_resources(self, server_name: str | None = None) -> ToolResult:
        # 变量说明：names 表示当前流程使用的 names 集合。
        names = [server_name] if server_name in self.servers else (
            list(self.servers) if server_name is None else []
        )
        # 变量说明：selected 表示当前步骤使用的 selected 值。
        selected = [server for name in names if (server := await self.ensure_server_started(str(name))) is not None]
        # 变量说明：resources 表示当前流程使用的 resources 集合。
        resources: list[dict[str, Any]] = []
        for server in selected:
            if server.client is None or not server.ready or server.closed:
                continue
            # 变量说明：cursor 表示当前步骤使用的 cursor 值。
            cursor: str | None = None
            while True:
                # 变量说明：result 表示本步骤产生的结果。
                result = await server.client.list_resources(cursor=cursor)
                for resource in result.resources:
                    # 变量说明：dumped 表示当前步骤使用的 dumped 值。
                    dumped = _model_dump(resource)
                    resources.append({"server": server.name, **(dumped if isinstance(dumped, dict) else {})})
                # 变量说明：cursor 表示当前步骤使用的 cursor 值。
                cursor = getattr(result, "next_cursor", None)
                if not cursor:
                    break
        return ToolResult("ListMcpResources", True, json.dumps(resources, ensure_ascii=False), metadata={"count": len(resources)})

    # 函数职责：异步完成 read_resource 对应的业务处理。
    # 参数关系：uri 表示当前步骤使用的 uri 值；server_name 表示当前步骤使用的 server_name 值。
    # 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
    async def read_resource(self, uri: str, server_name: str | None = None) -> ToolResult:
        if not server_name:
            return ToolResult("ReadMcpResource", False, "server is required for a live MCP resource", error_code="invalid_arguments")
        # 变量说明：server 表示当前步骤使用的 server 值。
        server = await self.ensure_server_started(server_name)
        if server is None or server.client is None or not server.ready or server.closed:
            return ToolResult("ReadMcpResource", False, f"MCP server {server_name!r} is not connected", error_code="mcp_not_connected")
        try:
            # 变量说明：result 表示本步骤产生的结果。
            result = await asyncio.wait_for(server.client.read_resource(uri), timeout=server.config.tool_timeout_sec)
        except asyncio.TimeoutError:
            return ToolResult("ReadMcpResource", False, "MCP resource read timed out", error_code="mcp_timeout")
        except Exception as exc:
            return ToolResult("ReadMcpResource", False, f"MCP resource read failed: {type(exc).__name__}: {exc}", error_code="mcp_resource_error")
        # 变量说明：content 表示待处理或返回的正文内容。
        content = [_model_dump(item) for item in result.contents]
        return ToolResult("ReadMcpResource", True, json.dumps(content, ensure_ascii=False), metadata={"server": server_name, "uri": uri})

    # 函数职责：完成 status 对应的业务处理。
    # 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
    def status(self) -> dict[str, Any]:
        return {
            "session_key": self.session_key,
            "runtime_scope": self.runtime_scope,
            "startup_policy": self.startup_policy,
            "servers": [
                {
                    "name": server.name,
                    "transport": server.config.transport,
                    "required": server.config.required,
                    "status": (
                        "dormant" if server.dormant
                        else "failed" if server.client is None or not server.ready or server.closed
                        else "degraded" if server.error
                        else "ready"
                    ),
                    "error_code": server.error_kind,
                    "protocol_version": server.protocol_version,
                    "server_info": _public_server_info(server.server_info),
                    "tool_count": len(server.current_catalog()),
                    "catalog_source": (
                        "live" if server.ready
                        else "cache" if server.cached_tools
                        else "unavailable"
                    ),
                }
                for server in self.servers.values()
            ],
        }

    # 函数职责：异步完成 close 对应的业务处理。
    # 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
    async def close(self) -> None:
        await asyncio.gather(*(server.close() for server in self.servers.values()), return_exceptions=True)
        self.servers.clear()


# 类职责：定义 _null_async_context 在本领域中的数据与行为。
class _null_async_context:
    # 函数职责：异步完成 aenter 对应的业务处理。
    # 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
    async def __aenter__(self) -> None:
        return None

    # 函数职责：异步完成 aexit 对应的业务处理。
    # 参数关系：_args 表示当前流程使用的 _args 集合。
    # 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
    async def __aexit__(self, *_args: object) -> None:
        return None


# 类职责：定义 McpRuntimePool 在本领域中的数据与行为。
class McpRuntimePool:
    """Application owner for session-scoped MCP runtimes."""

    # 函数职责：初始化实例依赖与初始状态。
    # 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
    def __init__(self) -> None:
        # 变量说明：_runtimes 表示当前流程使用的 _runtimes 集合。
        self._runtimes: dict[tuple[str, str, str, McpStartupPolicy, tuple[str, ...] | None], McpSessionRuntime] = {}
        # 变量说明：_locks 表示当前流程使用的 _locks 集合。
        self._locks: dict[tuple[str, str, str, McpStartupPolicy, tuple[str, ...] | None], asyncio.Lock] = {}
        # 变量说明：_configuration_lock 表示当前步骤使用的 _configuration_lock 值。
        self._configuration_lock = asyncio.Lock()
        # 变量说明：_configuration_generation 表示当前步骤使用的 _configuration_generation 值。
        self._configuration_generation = 0
        # 变量说明：tool_catalog_cache 表示当前步骤使用的 tool_catalog_cache 值。
        self.tool_catalog_cache = McpToolCatalogCache()

    # 函数职责：异步完成 get 对应的业务处理。
    # 参数关系：session_key 表示当前步骤使用的 session_key 值；workspace_root 表示当前步骤使用的 workspace_root 值；runtime_scope 表示当前步骤使用的 runtime_scope 值；startup_policy 表示当前步骤使用的 startup_policy 值；selected_server_names 表示当前流程使用的 selected_server_names 集合。
    # 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
    async def get(
        self,
        session_key: str,
        workspace_root: str,
        *,
        runtime_scope: str | None = None,
        startup_policy: McpStartupPolicy = "eager",
        selected_server_names: tuple[str, ...] | None = None,
    ) -> McpSessionRuntime | None:
        # 变量说明：runtime 表示当前步骤使用的 runtime 值；_created 表示当前步骤使用的 _created 值。
        runtime, _created = await self.acquire(
            session_key,
            workspace_root,
            runtime_scope=runtime_scope,
            startup_policy=startup_policy,
            selected_server_names=selected_server_names,
        )
        return runtime

    # 函数职责：异步完成 acquire 对应的业务处理。
    # 参数关系：session_key 表示当前步骤使用的 session_key 值；workspace_root 表示当前步骤使用的 workspace_root 值；runtime_scope 表示当前步骤使用的 runtime_scope 值；startup_policy 表示当前步骤使用的 startup_policy 值；selected_server_names 表示当前流程使用的 selected_server_names 集合。
    # 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
    async def acquire(
        self,
        session_key: str,
        workspace_root: str,
        *,
        runtime_scope: str | None = None,
        startup_policy: McpStartupPolicy = "eager",
        selected_server_names: tuple[str, ...] | None = None,
    ) -> tuple[McpSessionRuntime | None, bool]:
        """Return a session runtime and whether this call performed its initial startup."""

        # 变量说明：resolved_root 表示当前步骤使用的 resolved_root 值。
        resolved_root = str(Path(workspace_root).resolve())
        # 变量说明：resolved_scope 表示当前步骤使用的 resolved_scope 值。
        resolved_scope = runtime_scope or session_key
        # 变量说明：selection 表示当前步骤使用的 selection 值。
        selection = tuple(dict.fromkeys(selected_server_names)) if selected_server_names is not None else None
        # 变量说明：runtime_key 表示当前步骤使用的 runtime_key 值。
        runtime_key = (session_key, resolved_scope, resolved_root, startup_policy, selection)
        # 变量说明：lock 表示当前步骤使用的 lock 值。
        lock = self._locks.setdefault(runtime_key, asyncio.Lock())
        async with lock:
            while True:
                async with self._configuration_lock:
                    # 变量说明：current 表示当前步骤使用的 current 值。
                    current = self._runtimes.get(runtime_key)
                    if current is not None:
                        return current, False
                    # 变量说明：config 表示当前生效的配置。
                    config = load_mcp_config(settings.mcp_config_file)
                    if selection is not None:
                        # 变量说明：config 表示当前生效的配置。
                        config = McpConfig(servers={
                            name: config.servers[name]
                            for name in selection
                            if name in config.servers
                        })
                    if not any(server.enabled for server in config.servers.values()):
                        return None, False
                    # 变量说明：generation 表示当前步骤使用的 generation 值。
                    generation = self._configuration_generation
                # 变量说明：runtime 表示当前步骤使用的 runtime 值。
                runtime = McpSessionRuntime(
                    session_key,
                    Path(resolved_root),
                    config,
                    self.tool_catalog_cache,
                    startup_policy=startup_policy,
                    runtime_scope=resolved_scope,
                )
                try:
                    await runtime.start()
                except Exception:
                    async with self._configuration_lock:
                        # 变量说明：stale 表示当前步骤使用的 stale 值。
                        stale = generation != self._configuration_generation
                    await runtime.close()
                    if stale:
                        continue
                    raise
                async with self._configuration_lock:
                    if generation == self._configuration_generation:
                        # 变量说明：映射 的索引项 表示该语句创建或更新的目标数据。
                        self._runtimes[runtime_key] = runtime
                        return runtime, True
                await runtime.close()

    # 函数职责：异步应用 configuration 对应的数据或流程。
    # 参数关系：change 表示当前步骤使用的 change 值。
    # 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
    async def apply_configuration(self, change: Callable[[], _ConfigurationResult]) -> _ConfigurationResult:
        """Serialize persistence against startup and retire cached clients."""

        async with self._configuration_lock:
            # 变量说明：result 表示本步骤产生的结果。
            result = change()
            self._configuration_generation += 1
            # 变量说明：runtimes 表示当前流程使用的 runtimes 集合。
            runtimes = list(self._runtimes.values())
            self._runtimes.clear()
        await asyncio.gather(*(runtime.close() for runtime in runtimes), return_exceptions=True)
        return result

    # 函数职责：异步完成 close_session 对应的业务处理。
    # 参数关系：session_key 表示当前步骤使用的 session_key 值。
    # 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
    async def close_session(self, session_key: str) -> None:
        async with self._configuration_lock:
            # 变量说明：keys 表示当前流程使用的 keys 集合。
            keys = [key for key in self._runtimes if key[0] == session_key]
            # 变量说明：runtimes 表示当前流程使用的 runtimes 集合。
            runtimes = [self._runtimes.pop(key) for key in keys]
            for key in [key for key in self._locks if key[0] == session_key]:
                self._locks.pop(key, None)
        await asyncio.gather(*(runtime.close() for runtime in runtimes), return_exceptions=True)

    # 函数职责：异步完成 shutdown 对应的业务处理。
    # 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
    async def shutdown(self) -> None:
        async with self._configuration_lock:
            self._configuration_generation += 1
            # 变量说明：runtimes 表示当前流程使用的 runtimes 集合。
            runtimes = list(self._runtimes.values())
            self._runtimes.clear()
            self._locks.clear()
        await asyncio.gather(*(runtime.close() for runtime in runtimes), return_exceptions=True)

    # 函数职责：完成 statuses 对应的业务处理。
    # 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
    def statuses(self) -> list[dict[str, Any]]:
        return [runtime.status() for runtime in self._runtimes.values()]


# 变量说明：mcp_runtime_pool 表示当前步骤使用的 mcp_runtime_pool 值。
mcp_runtime_pool = McpRuntimePool()
