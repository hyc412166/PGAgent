"""Adapt discovered MCP operations into PGAgent model tools."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from src.config import settings
from src.tools.registry import ToolRegistry

from .config import load_mcp_config
from .runtime import McpToolBinding, mcp_runtime_pool


async def attach_mcp_tools(
    registry: ToolRegistry,
    *,
    session_key: str,
    workspace_root: str,
    frozen_tools: object = None,
    progress_sink: Callable[[dict[str, Any]], None] | None = None,
) -> None:
    """Discover and freeze MCP tools for one AgentRuntime invocation."""

    if "MCP" not in registry.enabled_tool_names:
        return
    configured = load_mcp_config(settings.mcp_config_file)
    enabled_servers = sorted(name for name, server in configured.servers.items() if server.enabled)
    if not enabled_servers:
        return
    if progress_sink is not None:
        progress_sink({"type": "mcp_connecting", "servers": enabled_servers})
    runtime, created = await mcp_runtime_pool.acquire(session_key, workspace_root)
    if runtime is None:
        return
    bindings = await runtime.refresh_tools(reconnect_failed=not created)
    if progress_sink is not None:
        status = runtime.status()
        failed_servers = [
            {"name": item["name"], "error_code": item["error_code"]}
            for item in status["servers"]
            if item["status"] != "ready"
        ]
        progress_sink({
            "type": "mcp_degraded" if failed_servers else "mcp_ready",
            "tool_count": len(bindings),
            "failed_servers": failed_servers,
        })
    snapshots = [binding.snapshot() for binding in bindings]
    if frozen_tools is not None:
        expected = [dict(item) for item in frozen_tools if isinstance(item, Mapping)] if isinstance(frozen_tools, list) else []
        if snapshots != expected:
            raise RuntimeError("MCP tool catalog changed after this run was frozen; start a new run before calling MCP tools")

    async def generic_call(arguments: dict[str, Any]):
        return await runtime.call_tool(
            str(arguments.get("server") or ""),
            str(arguments.get("tool") or ""),
            dict(arguments.get("arguments") or {}),
        )

    async def list_resources(arguments: dict[str, Any]):
        return await runtime.list_resources(str(arguments.get("server") or "") or None)

    async def read_resource(arguments: dict[str, Any]):
        return await runtime.read_resource(
            str(arguments.get("uri") or ""),
            str(arguments.get("server") or "") or None,
        )

    registry.register_external(
        "MCP",
        registry.schema_for("MCP"),
        generic_call,
        read_only=False,
        parallel=False,
    )
    if "ListMcpResources" in registry.enabled_tool_names:
        registry.register_external(
            "ListMcpResources",
            registry.schema_for("ListMcpResources"),
            list_resources,
            read_only=True,
            parallel=False,
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
        )

    for binding in bindings:
        async def invoke(arguments: dict[str, Any], selected: McpToolBinding = binding):
            result = await runtime.call_tool(selected.server_name, selected.raw_name, arguments)
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
        )
    registry.set_external_state("mcp_tools", snapshots)
