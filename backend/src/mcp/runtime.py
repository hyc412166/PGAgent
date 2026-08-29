"""MCP connections, tool discovery, routing, and session ownership."""

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


_INVALID_TOOL_NAME = re.compile(r"[^A-Za-z0-9_-]+")
_MAX_MODEL_TOOL_NAME = 64
_ConfigurationResult = TypeVar("_ConfigurationResult")
McpStartupPolicy = Literal["eager", "lazy_when_cached"]


def _model_dump(value: object) -> object:
    method = getattr(value, "model_dump", None)
    if callable(method):
        return method(mode="json", by_alias=True, exclude_none=True)
    return value


def _public_server_info(value: object) -> dict[str, str] | None:
    dumped = _model_dump(value)
    if not isinstance(dumped, dict):
        return None
    public = {
        key: str(dumped[key])
        for key in ("name", "version")
        if isinstance(dumped.get(key), str)
    }
    return public or None


def _sanitize_tool_part(value: str, fallback: str) -> str:
    normalized = _INVALID_TOOL_NAME.sub("_", value).strip("_")
    return normalized or fallback


@dataclass(frozen=True, slots=True)
class McpToolBinding:
    server_name: str
    raw_name: str
    model_name: str
    description: str
    input_schema: dict[str, Any]
    read_only: bool
    supports_parallel: bool

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


@dataclass(slots=True)
class ConnectedMcpServer:
    name: str
    config: McpServerConfig
    workspace_root: Path
    client: Client | None = None
    error: str | None = None
    error_kind: str | None = None
    protocol_version: str | None = None
    server_info: dict[str, Any] | None = None
    tools: list[object] = field(default_factory=list)
    call_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    startup_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    catalog_identity: ToolCatalogIdentity | None = None
    cached_tools: tuple[CachedMcpTool, ...] = ()
    dormant: bool = False
    catalog_changed: bool = False
    catalog_cacheable: bool = True
    ready: bool = False
    closed: bool = False
    _close_event: asyncio.Event | None = None
    _owner_task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        if self.closed:
            raise RuntimeError(f"MCP server {self.name!r} is closed")
        self.dormant = False
        self.ready = False
        self.error = None
        self.error_kind = None
        ready: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        close_event = asyncio.Event()
        self._close_event = close_event
        self._owner_task = asyncio.create_task(
            self._run_connection(ready, close_event),
            name=f"mcp-{self.name}",
        )
        try:
            await ready
        except asyncio.CancelledError:
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
            task, self._owner_task = self._owner_task, None
            if task is not None:
                # The owner already began unwinding its transport after
                # publishing this startup error. A second cancellation here
                # could interrupt AsyncExitStack.aclose() and leak stdio.
                await asyncio.gather(task, return_exceptions=True)
            raise

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
            self.ready = True

    async def _run_connection(
        self,
        ready: asyncio.Future[None],
        close_event: asyncio.Event,
    ) -> None:
        """Enter and exit SDK transports in their single owning task."""

        stack = AsyncExitStack()
        try:
            if self.config.command:
                cwd = Path(self.config.cwd) if self.config.cwd else self.workspace_root
                if not cwd.is_absolute():
                    cwd = self.workspace_root / cwd
                target: object = StdioServerParameters(
                    command=self.config.command,
                    args=list(self.config.args),
                    env=dict(self.config.env) or None,
                    cwd=cwd,
                )
            else:
                http_client = await stack.enter_async_context(
                    httpx2.AsyncClient(headers=dict(self.config.headers), follow_redirects=False)
                )
                target = streamable_http_client(str(self.config.url), http_client=http_client)
            client = Client(target, read_timeout_seconds=self.config.tool_timeout_sec)
            self.client = await stack.enter_async_context(client)
            self.protocol_version = str(client.protocol_version)
            info = client.server_info
            dumped_info = _model_dump(info) if info is not None else None
            self.server_info = dumped_info if isinstance(dumped_info, dict) else None
            capabilities = getattr(client, "server_capabilities", None)
            experimental = getattr(capabilities, "experimental", None) or {}
            cache_capability = experimental.get("codex/tool-catalog-cache", {})
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
                self.error = str(exc) or type(exc).__name__
                self.error_kind = type(exc).__name__
            raise
        finally:
            self.ready = False
            self.client = None
            await stack.aclose()

    async def refresh_tools(self) -> None:
        if self.client is None:
            return
        cursor: str | None = None
        discovered: list[object] = []
        while True:
            result = await self.client.list_tools(cursor=cursor)
            discovered.extend(result.tools)
            cursor = getattr(result, "next_cursor", None)
            if not cursor:
                break
        self.tools = [tool for tool in discovered if self.config.allows_tool(str(getattr(tool, "name", "")))]

    def current_catalog(self) -> tuple[CachedMcpTool, ...]:
        if self.client is None:
            return self.cached_tools
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

    async def close(self) -> None:
        self.closed = True
        async with self.startup_lock:
            event, self._close_event = self._close_event, None
            task, self._owner_task = self._owner_task, None
            if event is not None:
                event.set()
            if task is not None:
                await asyncio.gather(task, return_exceptions=True)
            self.ready = False
            self.client = None


class McpSessionRuntime:
    """Connections and the latest catalog owned by one PGAgent session."""

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
        self.session_key = session_key
        self.runtime_scope = runtime_scope or session_key
        self.workspace_root = workspace_root
        self.config = config
        self.startup_policy = startup_policy
        self.tool_catalog_cache = tool_catalog_cache if tool_catalog_cache is not None else McpToolCatalogCache()
        self.servers: dict[str, ConnectedMcpServer] = {}

    async def start(self) -> None:
        enabled = [
            (name, config)
            for name, config in sorted(self.config.servers.items())
            if config.enabled
        ]
        servers: list[ConnectedMcpServer] = []
        eager_servers: list[ConnectedMcpServer] = []
        for name, server_config in enabled:
            identity = ToolCatalogIdentity.create(name, server_config, self.workspace_root)
            cached_tools = self.tool_catalog_cache.get(identity)
            server = ConnectedMcpServer(
                name,
                server_config,
                self.workspace_root,
                catalog_identity=identity,
                cached_tools=cached_tools or (),
            )
            can_defer = (
                self.startup_policy == "lazy_when_cached"
                and bool(cached_tools)
            )
            server.dormant = can_defer
            servers.append(server)
            if not can_defer:
                eager_servers.append(server)
        results = await asyncio.gather(
            *(asyncio.wait_for(server.start(), timeout=server.config.startup_timeout_sec) for server in eager_servers),
            return_exceptions=True,
        )
        required_errors: list[str] = []
        for server in servers:
            self.servers[server.name] = server
        for server, result in zip(eager_servers, results, strict=True):
            if isinstance(result, BaseException):
                server.error = str(result) or type(result).__name__
                server.error_kind = type(result).__name__
                if server.config.required:
                    required_errors.append(f"{server.name}: {server.error}")
            else:
                self._accept_started_server(server, server.cached_tools)
                server.ready = server.client is not None
        if required_errors:
            await self.close()
            raise RuntimeError("required MCP servers failed to initialize: " + "; ".join(required_errors))

    def _publish_server_catalog(self, server: ConnectedMcpServer) -> None:
        catalog = server.current_catalog()
        server.cached_tools = catalog
        if server.catalog_identity is not None:
            if server.catalog_cacheable:
                self.tool_catalog_cache.put(server.catalog_identity, catalog)
            else:
                self.tool_catalog_cache.remove(server.catalog_identity)

    def _accept_started_server(
        self,
        server: ConnectedMcpServer,
        previous_catalog: tuple[CachedMcpTool, ...],
    ) -> None:
        live_catalog = server.current_catalog()
        server.error = None
        server.error_kind = None
        server.catalog_changed = bool(previous_catalog) and live_catalog != previous_catalog
        self._publish_server_catalog(server)

    def bindings(self) -> list[McpToolBinding]:
        candidates: list[tuple[ConnectedMcpServer, CachedMcpTool]] = []
        for server in self.servers.values():
            candidates.extend((server, tool) for tool in server.current_catalog())
        candidates.sort(key=lambda item: (item[0].name, item[1].raw_name))

        used_names: set[str] = set()
        bindings: list[McpToolBinding] = []
        for server, tool in candidates:
            raw_name = tool.raw_name
            server_part = _sanitize_tool_part(server.name, "server")
            tool_part = _sanitize_tool_part(raw_name, "tool")
            base = f"mcp__{server_part}__{tool_part}"[:_MAX_MODEL_TOOL_NAME]
            model_name = base
            suffix = 2
            while model_name in used_names:
                marker = f"_{suffix}"
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

    async def refresh_tools(self, *, reconnect_failed: bool = False) -> list[McpToolBinding]:
        """Return the current catalog, retrying failed clients without refreshing ready ones."""

        if reconnect_failed:
            for server in self.servers.values():
                if server.ready or server.dormant:
                    continue
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
                    server.error = str(exc) or type(exc).__name__
                    server.error_kind = type(exc).__name__
                    if server.config.required:
                        raise RuntimeError(
                            f"required MCP server {server.name!r} could not reconnect: {server.error}"
                        ) from exc
        return self.bindings()

    async def ensure_server_started(self, server_name: str) -> ConnectedMcpServer | None:
        server = self.servers.get(server_name)
        if server is None:
            return None
        if server.ready and not server.closed:
            return server
        previous_catalog = server.cached_tools
        try:
            await server.ensure_started(
                lambda: self._accept_started_server(server, previous_catalog)
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            server.error = str(exc) or type(exc).__name__
            server.error_kind = type(exc).__name__
            return server
        return server

    def accept_current_catalog(self) -> None:
        """A new run may use the refreshed live catalog after an old snapshot was rejected."""

        for server in self.servers.values():
            server.catalog_changed = False

    async def call_tool(self, server_name: str, tool_name: str, arguments: dict[str, Any]) -> ToolResult:
        server = await self.ensure_server_started(server_name)
        if server is None or server.client is None or not server.ready or server.closed:
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
                result = await asyncio.wait_for(
                    server.client.call_tool(tool_name, arguments, read_timeout_seconds=server.config.tool_timeout_sec),
                    timeout=server.config.tool_timeout_sec,
                )
        except asyncio.TimeoutError:
            return ToolResult(tool_name, False, "MCP tool call timed out", error_code="mcp_timeout")
        except Exception as exc:
            return ToolResult(tool_name, False, f"MCP tool call failed: {type(exc).__name__}: {exc}", error_code="mcp_tool_error")

        content = [_model_dump(item) for item in result.content]
        structured = _model_dump(getattr(result, "structured_content", None))
        payload: dict[str, Any] = {"content": content}
        if structured is not None:
            payload["structured_content"] = structured
        text_parts = [
            str(item.get("text"))
            for item in content
            if isinstance(item, dict) and item.get("type") == "text" and isinstance(item.get("text"), str)
        ]
        rendered = "\n".join(text_parts) if len(text_parts) == len(content) and text_parts else json.dumps(payload, ensure_ascii=False)
        is_error = bool(getattr(result, "is_error", False))
        return ToolResult(
            tool_name,
            not is_error,
            rendered,
            error_code="mcp_result_error" if is_error else None,
            metadata={"server": server_name, "raw_tool_name": tool_name, "structured_content": structured},
        )

    async def list_resources(self, server_name: str | None = None) -> ToolResult:
        names = [server_name] if server_name in self.servers else (
            list(self.servers) if server_name is None else []
        )
        selected = [server for name in names if (server := await self.ensure_server_started(str(name))) is not None]
        resources: list[dict[str, Any]] = []
        for server in selected:
            if server.client is None or not server.ready or server.closed:
                continue
            cursor: str | None = None
            while True:
                result = await server.client.list_resources(cursor=cursor)
                for resource in result.resources:
                    dumped = _model_dump(resource)
                    resources.append({"server": server.name, **(dumped if isinstance(dumped, dict) else {})})
                cursor = getattr(result, "next_cursor", None)
                if not cursor:
                    break
        return ToolResult("ListMcpResources", True, json.dumps(resources, ensure_ascii=False), metadata={"count": len(resources)})

    async def read_resource(self, uri: str, server_name: str | None = None) -> ToolResult:
        if not server_name:
            return ToolResult("ReadMcpResource", False, "server is required for a live MCP resource", error_code="invalid_arguments")
        server = await self.ensure_server_started(server_name)
        if server is None or server.client is None or not server.ready or server.closed:
            return ToolResult("ReadMcpResource", False, f"MCP server {server_name!r} is not connected", error_code="mcp_not_connected")
        try:
            result = await asyncio.wait_for(server.client.read_resource(uri), timeout=server.config.tool_timeout_sec)
        except Exception as exc:
            return ToolResult("ReadMcpResource", False, f"MCP resource read failed: {type(exc).__name__}: {exc}", error_code="mcp_resource_error")
        content = [_model_dump(item) for item in result.contents]
        return ToolResult("ReadMcpResource", True, json.dumps(content, ensure_ascii=False), metadata={"server": server_name, "uri": uri})

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

    async def close(self) -> None:
        await asyncio.gather(*(server.close() for server in self.servers.values()), return_exceptions=True)
        self.servers.clear()


class _null_async_context:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *_args: object) -> None:
        return None


class McpRuntimePool:
    """Application owner for session-scoped MCP runtimes."""

    def __init__(self) -> None:
        self._runtimes: dict[tuple[str, str, str, McpStartupPolicy, tuple[str, ...] | None], McpSessionRuntime] = {}
        self._locks: dict[tuple[str, str, str, McpStartupPolicy, tuple[str, ...] | None], asyncio.Lock] = {}
        self._configuration_lock = asyncio.Lock()
        self._configuration_generation = 0
        self.tool_catalog_cache = McpToolCatalogCache()

    async def get(
        self,
        session_key: str,
        workspace_root: str,
        *,
        runtime_scope: str | None = None,
        startup_policy: McpStartupPolicy = "eager",
        selected_server_names: tuple[str, ...] | None = None,
    ) -> McpSessionRuntime | None:
        runtime, _created = await self.acquire(
            session_key,
            workspace_root,
            runtime_scope=runtime_scope,
            startup_policy=startup_policy,
            selected_server_names=selected_server_names,
        )
        return runtime

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

        resolved_root = str(Path(workspace_root).resolve())
        resolved_scope = runtime_scope or session_key
        selection = tuple(dict.fromkeys(selected_server_names)) if selected_server_names is not None else None
        runtime_key = (session_key, resolved_scope, resolved_root, startup_policy, selection)
        lock = self._locks.setdefault(runtime_key, asyncio.Lock())
        async with lock:
            while True:
                async with self._configuration_lock:
                    current = self._runtimes.get(runtime_key)
                    if current is not None:
                        return current, False
                    config = load_mcp_config(settings.mcp_config_file)
                    if selection is not None:
                        config = McpConfig(servers={
                            name: config.servers[name]
                            for name in selection
                            if name in config.servers
                        })
                    if not any(server.enabled for server in config.servers.values()):
                        return None, False
                    generation = self._configuration_generation
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
                        stale = generation != self._configuration_generation
                    await runtime.close()
                    if stale:
                        continue
                    raise
                async with self._configuration_lock:
                    if generation == self._configuration_generation:
                        self._runtimes[runtime_key] = runtime
                        return runtime, True
                await runtime.close()

    async def apply_configuration(self, change: Callable[[], _ConfigurationResult]) -> _ConfigurationResult:
        """Serialize persistence against startup and retire cached clients."""

        async with self._configuration_lock:
            result = change()
            self._configuration_generation += 1
            runtimes = list(self._runtimes.values())
            self._runtimes.clear()
        await asyncio.gather(*(runtime.close() for runtime in runtimes), return_exceptions=True)
        return result

    async def close_session(self, session_key: str) -> None:
        async with self._configuration_lock:
            keys = [key for key in self._runtimes if key[0] == session_key]
            runtimes = [self._runtimes.pop(key) for key in keys]
            for key in [key for key in self._locks if key[0] == session_key]:
                self._locks.pop(key, None)
        await asyncio.gather(*(runtime.close() for runtime in runtimes), return_exceptions=True)

    async def shutdown(self) -> None:
        async with self._configuration_lock:
            self._configuration_generation += 1
            runtimes = list(self._runtimes.values())
            self._runtimes.clear()
            self._locks.clear()
        await asyncio.gather(*(runtime.close() for runtime in runtimes), return_exceptions=True)

    def statuses(self) -> list[dict[str, Any]]:
        return [runtime.status() for runtime in self._runtimes.values()]


mcp_runtime_pool = McpRuntimePool()
