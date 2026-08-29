"""Adapt discovered MCP operations into PGAgent model tools."""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping
from typing import Any, Literal

from src.config import settings
from src.tools.registry import ToolRegistry
from src.tools.types import ToolResult

from .config import load_mcp_config
from .runtime import McpToolBinding, mcp_runtime_pool


_SEARCH_TERMS = re.compile(r"[A-Za-z0-9_\-]+|[\u4e00-\u9fff]+")
_MCP_TOOL_SEARCH_SCHEMA = {
    "description": (
        "Search available MCP tools by capability, server, or tool name. "
        "Matching tools are loaded into the next model tool list; call this before using MCP capabilities."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "minLength": 1, "description": "Capability or MCP tool to find."},
            "server": {"type": "string", "description": "Optional MCP server name filter."},
            "max_results": {"type": "integer", "minimum": 1, "maximum": 20},
        },
        "required": ["query"],
        "additionalProperties": False,
    },
}


def _search_mcp_tools(
    bindings: list[McpToolBinding],
    query: str,
    *,
    server: str = "",
    max_results: int = 8,
) -> list[McpToolBinding]:
    terms = [item.casefold() for item in _SEARCH_TERMS.findall(query) if item.strip()]
    server_filter = server.strip().casefold()
    ranked: list[tuple[int, str, McpToolBinding]] = []
    for binding in bindings:
        if server_filter and binding.server_name.casefold() != server_filter:
            continue
        raw_name = binding.raw_name.casefold()
        model_name = binding.model_name.casefold()
        server_name = binding.server_name.casefold()
        description = binding.description.casefold()
        if not terms:
            score = 1
        else:
            score = sum(
                8 if term == raw_name
                else 5 if term in raw_name
                else 3 if term in model_name
                else 2 if term in description
                else 1 if term in server_name
                else 0
                for term in terms
            )
            if score == 0:
                continue
        ranked.append((score, binding.model_name, binding))
    ranked.sort(key=lambda item: (-item[0], item[1]))
    if not ranked:
        return []
    minimum_score = max(1, ranked[0][0] - 3)
    return [item[2] for item in ranked if item[0] >= minimum_score][:max_results]


async def attach_mcp_tools(
    registry: ToolRegistry,
    *,
    session_key: str,
    workspace_root: str,
    agent_kind: Literal["main", "subagent"] = "main",
    runtime_scope: str | None = None,
    selected_server_names: list[str] | tuple[str, ...] | None = None,
    frozen_tools: object = None,
    frozen_active_tools: object = None,
    progress_sink: Callable[[dict[str, Any]], None] | None = None,
) -> None:
    """Discover and freeze MCP tools for one AgentRuntime invocation."""

    if "MCP" not in registry.enabled_tool_names:
        return
    registry.hide_model_tool("MCP")
    configured = load_mcp_config(settings.mcp_config_file)
    selection = (
        tuple(dict.fromkeys(str(name).strip() for name in selected_server_names if str(name).strip()))
        if selected_server_names is not None else None
    )
    enabled_servers = sorted(
        name for name, server in configured.servers.items()
        if server.enabled and (selection is None or name in selection)
    )
    if not enabled_servers:
        return
    if progress_sink is not None:
        progress_sink({"type": "mcp_catalog_loading", "servers": enabled_servers})
    runtime, created = await mcp_runtime_pool.acquire(
        session_key,
        workspace_root,
        runtime_scope=runtime_scope,
        startup_policy="lazy_when_cached" if agent_kind == "subagent" else "eager",
        selected_server_names=selection,
    )
    if runtime is None:
        return
    bindings = await runtime.refresh_tools(reconnect_failed=not created)
    if progress_sink is not None:
        status = runtime.status()
        failed_servers = [
            {"name": item["name"], "error_code": item["error_code"]}
            for item in status["servers"]
            if item["status"] not in {"ready", "dormant"}
        ]
        dormant_servers = [
            item["name"] for item in status["servers"] if item["status"] == "dormant"
        ]
        progress_sink({
            "type": "mcp_degraded" if failed_servers else "mcp_ready",
            "tool_count": len(bindings),
            "failed_servers": failed_servers,
            "dormant_servers": dormant_servers,
            "connected_servers": [
                item["name"] for item in status["servers"] if item["status"] == "ready"
            ],
        })
    snapshots = [binding.snapshot() for binding in bindings]
    if frozen_tools is not None:
        expected = [dict(item) for item in frozen_tools if isinstance(item, Mapping)] if isinstance(frozen_tools, list) else []
        if snapshots != expected:
            raise RuntimeError("MCP tool catalog changed after this run was frozen; start a new run before calling MCP tools")
    else:
        runtime.accept_current_catalog()

    async def generic_call(arguments: dict[str, Any]):
        server_name = str(arguments.get("server") or "")
        waking = announce_wakeup(server_name)
        result = await runtime.call_tool(
            server_name,
            str(arguments.get("tool") or ""),
            dict(arguments.get("arguments") or {}),
        )
        if waking:
            announce_wakeup_result(server_name)
        return result

    def announce_wakeup(server_name: str) -> bool:
        server = runtime.servers.get(server_name)
        waking = server is not None and server.dormant
        if progress_sink is not None and waking:
            progress_sink({"type": "mcp_connecting", "servers": [server_name], "trigger": "tool_call"})
        return waking

    def announce_wakeup_result(server_name: str) -> None:
        if progress_sink is None:
            return
        server = runtime.servers.get(server_name)
        if server is None:
            return
        progress_sink({
            "type": "mcp_server_ready" if server.client is not None else "mcp_degraded",
            "server": server_name,
            "tool_count": len(server.current_catalog()),
            "error_code": server.error_kind,
        })

    async def search_tools(arguments: dict[str, Any]):
        query = str(arguments.get("query") or "").strip()
        if not query or _SEARCH_TERMS.search(query) is None:
            return ToolResult(
                "McpToolSearch",
                False,
                "query must not be empty",
                error_code="invalid_arguments",
            )
        server = str(arguments.get("server") or "").strip()
        raw_limit = arguments.get("max_results", 8)
        limit = raw_limit if isinstance(raw_limit, int) and not isinstance(raw_limit, bool) else 8
        matches = _search_mcp_tools(bindings, query, server=server, max_results=max(1, min(limit, 20)))
        activated = registry.activate_deferred_tools(binding.model_name for binding in matches)
        payload = {
            "query": query,
            "activated_tools": [
                {
                    "server": binding.server_name,
                    "tool": binding.raw_name,
                    "name": binding.model_name,
                    "description": binding.description,
                    "parameters": binding.input_schema,
                }
                for binding in matches
            ],
            "next_step": "Call one activated tool by its exact name." if activated else "Refine the search query.",
        }
        return ToolResult(
            "McpToolSearch",
            True,
            json.dumps(payload, ensure_ascii=False),
            metadata={"activated_count": len(activated)},
        )

    async def list_resources(arguments: dict[str, Any]):
        server_name = str(arguments.get("server") or "") or None
        waking = False
        if server_name:
            waking = announce_wakeup(server_name)
        result = await runtime.list_resources(server_name)
        if server_name and waking:
            announce_wakeup_result(server_name)
        return result

    async def read_resource(arguments: dict[str, Any]):
        server_name = str(arguments.get("server") or "")
        waking = announce_wakeup(server_name)
        result = await runtime.read_resource(
            str(arguments.get("uri") or ""),
            server_name or None,
        )
        if waking:
            announce_wakeup_result(server_name)
        return result

    registry.register_external(
        "MCP",
        registry.schema_for("MCP"),
        generic_call,
        read_only=False,
        parallel=False,
        exposure="hidden",
        owner="pgagent-mcp",
        raw_name="call",
        trusted=True,
        replace_existing=True,
    )
    registry.register_external(
        "McpToolSearch",
        _MCP_TOOL_SEARCH_SCHEMA,
        search_tools,
        read_only=True,
        parallel=False,
        approval_exempt=True,
        owner="pgagent-mcp",
        raw_name="search",
        trusted=True,
        replace_existing=True,
    )
    if "ListMcpResources" in registry.enabled_tool_names:
        registry.register_external(
            "ListMcpResources",
            registry.schema_for("ListMcpResources"),
            list_resources,
            read_only=True,
            parallel=False,
            owner="pgagent-mcp",
            raw_name="list_resources",
            trusted=True,
            replace_existing=True,
        )
    if "ReadMcpResource" in registry.enabled_tool_names:
        resource_schema = registry.schema_for("ReadMcpResource")
        resource_parameters = dict(resource_schema.get("parameters") or {})
        resource_parameters["required"] = ["uri", "server"]
        resource_schema["parameters"] = resource_parameters
        registry.register_external(
            "ReadMcpResource",
            resource_schema,
            read_resource,
            read_only=True,
            parallel=False,
            owner="pgagent-mcp",
            raw_name="read_resource",
            trusted=True,
            replace_existing=True,
        )

    for binding in bindings:
        async def invoke(arguments: dict[str, Any], selected: McpToolBinding = binding):
            waking = announce_wakeup(selected.server_name)
            result = await runtime.call_tool(selected.server_name, selected.raw_name, arguments)
            if waking:
                announce_wakeup_result(selected.server_name)
            result.tool_name = selected.model_name
            result.metadata.update({"mcp_server": selected.server_name, "mcp_tool": selected.raw_name})
            if result.ok and not selected.read_only:
                result.changed = True
            return result

        registry.register_external(
            binding.model_name,
            {"description": binding.description, "parameters": binding.input_schema},
            invoke,
            read_only=binding.read_only,
            parallel=binding.supports_parallel,
            exposure="deferred",
            owner=binding.server_name,
            raw_name=binding.raw_name,
        )
    active_tools = [str(item) for item in frozen_active_tools] if isinstance(frozen_active_tools, list) else []
    if frozen_tools is not None and frozen_active_tools is None:
        # Snapshots from the eager-exposure implementation predate
        # mcp_active_tools. Preserve their model-visible catalog on resume.
        active_tools = [binding.model_name for binding in bindings]
    unknown_active = sorted(set(active_tools).difference(binding.model_name for binding in bindings))
    if unknown_active:
        raise RuntimeError("MCP active tool set no longer exists in the frozen catalog")
    registry.activate_deferred_tools(active_tools)
    registry.set_external_state("mcp_tools", snapshots)
    registry.set_external_state("mcp_server_names", enabled_servers)
    namespaces = sorted({item.model_name.rsplit("__", 1)[0] for item in bindings})
    registry.set_external_state("mcp_namespaces", [
        {
            "namespace": namespace,
            "tool_count": sum(1 for item in bindings if item.model_name.startswith(f"{namespace}__")),
        }
        for namespace in namespaces
    ])
