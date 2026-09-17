"""Adapt discovered MCP operations into PGAgent model tools."""
# 文件职责：负责MCP 外部工具接入中的 integration 子模块。
# 逻辑关系：上层通过 mcp/integration.py 使用本模块；本模块把处理结果交给同领域服务、持久化层或 API 响应层。

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


# 变量说明：_SEARCH_TERMS 表示当前流程使用的 _SEARCH_TERMS 集合。
_SEARCH_TERMS = re.compile(r"[A-Za-z0-9_\-]+|[\u4e00-\u9fff]+")
# 变量说明：_MCP_TOOL_SEARCH_SCHEMA 表示当前步骤使用的 _MCP_TOOL_SEARCH_SCHEMA 值。
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


# 函数职责：完成 search_mcp_tools 对应的业务处理。
# 参数关系：bindings 表示当前流程使用的 bindings 集合；query 表示当前步骤使用的 query 值；server 表示当前步骤使用的 server 值；max_results 表示当前流程使用的 max_results 集合。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def _search_mcp_tools(
    bindings: list[McpToolBinding],
    query: str,
    *,
    server: str = "",
    max_results: int = 8,
) -> list[McpToolBinding]:
    # 变量说明：terms 表示当前流程使用的 terms 集合。
    terms = [item.casefold() for item in _SEARCH_TERMS.findall(query) if item.strip()]
    # 变量说明：server_filter 表示当前步骤使用的 server_filter 值。
    server_filter = server.strip().casefold()
    # 变量说明：ranked 表示当前步骤使用的 ranked 值。
    ranked: list[tuple[int, str, McpToolBinding]] = []
    for binding in bindings:
        if server_filter and binding.server_name.casefold() != server_filter:
            continue
        # 变量说明：raw_name 表示当前步骤使用的 raw_name 值。
        raw_name = binding.raw_name.casefold()
        # 变量说明：model_name 表示当前步骤使用的 model_name 值。
        model_name = binding.model_name.casefold()
        # 变量说明：server_name 表示当前步骤使用的 server_name 值。
        server_name = binding.server_name.casefold()
        # 变量说明：description 表示当前步骤使用的 description 值。
        description = binding.description.casefold()
        if not terms:
            # 变量说明：score 表示当前步骤使用的 score 值。
            score = 1
        else:
            # 变量说明：score 表示当前步骤使用的 score 值。
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
    # 变量说明：minimum_score 表示当前步骤使用的 minimum_score 值。
    minimum_score = max(1, ranked[0][0] - 3)
    matches = [item[2] for item in ranked if item[0] >= minimum_score][:max_results]
    # Playwright 的 click/navigate 可能把目标打开到新标签页；搜索命中交互工具时同步暴露标签页切换能力。
    interactive_servers = {
        binding.server_name
        for binding in matches
        if binding.raw_name in {"browser_click", "browser_navigate"}
    }
    for server_name in interactive_servers:
        companion = next((
            binding for binding in bindings
            if binding.server_name == server_name and binding.raw_name == "browser_tabs"
        ), None)
        if companion is None or companion in matches or max_results < 2:
            continue
        if len(matches) >= max_results:
            matches[-1] = companion
        else:
            matches.append(companion)
    return matches


# 函数职责：异步完成 attach_mcp_tools 对应的业务处理。
# 参数关系：registry 表示当前步骤使用的 registry 值；session_key 表示当前步骤使用的 session_key 值；workspace_root 表示当前步骤使用的 workspace_root 值；agent_kind 表示当前步骤使用的 agent_kind 值；runtime_scope 表示当前步骤使用的 runtime_scope 值；selected_server_names 表示当前流程使用的 selected_server_names 集合；frozen_tools 表示当前流程使用的 frozen_tools 集合；frozen_active_tools 表示当前流程使用的 frozen_active_tools 集合；其余参数沿用调用方提供的扩展选项。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
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
    # 变量说明：configured 表示当前步骤使用的 configured 值。
    configured = load_mcp_config(settings.mcp_config_file)
    # 变量说明：selection 表示当前步骤使用的 selection 值。
    selection = (
        tuple(dict.fromkeys(str(name).strip() for name in selected_server_names if str(name).strip()))
        if selected_server_names is not None else None
    )
    # 变量说明：enabled_servers 表示当前流程使用的 enabled_servers 集合。
    enabled_servers = sorted(
        name for name, server in configured.servers.items()
        if server.enabled and (selection is None or name in selection)
    )
    if not enabled_servers:
        return
    if progress_sink is not None:
        progress_sink({"type": "mcp_catalog_loading", "servers": enabled_servers})
    # 变量说明：runtime 表示当前步骤使用的 runtime 值；created 表示当前步骤使用的 created 值。
    runtime, created = await mcp_runtime_pool.acquire(
        session_key,
        workspace_root,
        runtime_scope=runtime_scope,
        startup_policy="lazy_when_cached" if agent_kind == "subagent" else "eager",
        selected_server_names=selection,
    )
    if runtime is None:
        return
    # 变量说明：bindings 表示当前流程使用的 bindings 集合。
    bindings = await runtime.refresh_tools(reconnect_failed=not created)
    if progress_sink is not None:
        # 变量说明：status 表示当前对象或运行的状态。
        status = runtime.status()
        # 变量说明：failed_servers 表示当前流程使用的 failed_servers 集合。
        failed_servers = [
            {"name": item["name"], "error_code": item["error_code"]}
            for item in status["servers"]
            if item["status"] not in {"ready", "dormant"}
        ]
        # 变量说明：dormant_servers 表示当前流程使用的 dormant_servers 集合。
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
    # 变量说明：snapshots 表示当前流程使用的 snapshots 集合。
    snapshots = [binding.snapshot() for binding in bindings]
    if frozen_tools is not None:
        # 变量说明：expected 表示当前步骤使用的 expected 值。
        expected = [dict(item) for item in frozen_tools if isinstance(item, Mapping)] if isinstance(frozen_tools, list) else []
        if snapshots != expected:
            raise RuntimeError("MCP tool catalog changed after this run was frozen; start a new run before calling MCP tools")
    else:
        runtime.accept_current_catalog()

    # 函数职责：异步完成 generic_call 对应的业务处理。
    # 参数关系：arguments 表示当前流程使用的 arguments 集合。
    # 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
    async def generic_call(arguments: dict[str, Any]):
        # 变量说明：server_name 表示当前步骤使用的 server_name 值。
        server_name = str(arguments.get("server") or "")
        # 变量说明：waking 表示当前步骤使用的 waking 值。
        waking = announce_wakeup(server_name)
        # 变量说明：result 表示本步骤产生的结果。
        result = await runtime.call_tool(
            server_name,
            str(arguments.get("tool") or ""),
            dict(arguments.get("arguments") or {}),
        )
        if waking:
            announce_wakeup_result(server_name)
        return result

    # 函数职责：完成 announce_wakeup 对应的业务处理。
    # 参数关系：server_name 表示当前步骤使用的 server_name 值。
    # 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
    def announce_wakeup(server_name: str) -> bool:
        # 变量说明：server 表示当前步骤使用的 server 值。
        server = runtime.servers.get(server_name)
        # 变量说明：waking 表示当前步骤使用的 waking 值。
        waking = server is not None and server.dormant
        if progress_sink is not None and waking:
            progress_sink({"type": "mcp_connecting", "servers": [server_name], "trigger": "tool_call"})
        return waking

    # 函数职责：完成 announce_wakeup_result 对应的业务处理。
    # 参数关系：server_name 表示当前步骤使用的 server_name 值。
    # 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
    def announce_wakeup_result(server_name: str) -> None:
        if progress_sink is None:
            return
        # 变量说明：server 表示当前步骤使用的 server 值。
        server = runtime.servers.get(server_name)
        if server is None:
            return
        progress_sink({
            "type": "mcp_server_ready" if server.client is not None else "mcp_degraded",
            "server": server_name,
            "tool_count": len(server.current_catalog()),
            "error_code": server.error_kind,
        })

    # 函数职责：异步完成 search_tools 对应的业务处理。
    # 参数关系：arguments 表示当前流程使用的 arguments 集合。
    # 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
    async def search_tools(arguments: dict[str, Any]):
        # 变量说明：query 表示当前步骤使用的 query 值。
        query = str(arguments.get("query") or "").strip()
        if not query or _SEARCH_TERMS.search(query) is None:
            return ToolResult(
                "McpToolSearch",
                False,
                "query must not be empty",
                error_code="invalid_arguments",
            )
        # 变量说明：server 表示当前步骤使用的 server 值。
        server = str(arguments.get("server") or "").strip()
        # 变量说明：raw_limit 表示当前步骤使用的 raw_limit 值。
        raw_limit = arguments.get("max_results", 8)
        # 变量说明：limit 表示当前步骤使用的 limit 值。
        limit = raw_limit if isinstance(raw_limit, int) and not isinstance(raw_limit, bool) else 8
        # 变量说明：matches 表示当前流程使用的 matches 集合。
        matches = _search_mcp_tools(bindings, query, server=server, max_results=max(1, min(limit, 20)))
        # 变量说明：activated 表示当前步骤使用的 activated 值。
        activated = registry.activate_deferred_tools(binding.model_name for binding in matches)
        # 变量说明：payload 表示跨层传递的数据载荷。
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

    # 函数职责：异步列出 resources 对应的数据或流程。
    # 参数关系：arguments 表示当前流程使用的 arguments 集合。
    # 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
    async def list_resources(arguments: dict[str, Any]):
        # 变量说明：server_name 表示当前步骤使用的 server_name 值。
        server_name = str(arguments.get("server") or "") or None
        # 变量说明：waking 表示当前步骤使用的 waking 值。
        waking = False
        if server_name:
            # 变量说明：waking 表示当前步骤使用的 waking 值。
            waking = announce_wakeup(server_name)
        # 变量说明：result 表示本步骤产生的结果。
        result = await runtime.list_resources(server_name)
        if server_name and waking:
            announce_wakeup_result(server_name)
        return result

    # 函数职责：异步完成 read_resource 对应的业务处理。
    # 参数关系：arguments 表示当前流程使用的 arguments 集合。
    # 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
    async def read_resource(arguments: dict[str, Any]):
        # 变量说明：server_name 表示当前步骤使用的 server_name 值。
        server_name = str(arguments.get("server") or "")
        # 变量说明：waking 表示当前步骤使用的 waking 值。
        waking = announce_wakeup(server_name)
        # 变量说明：result 表示本步骤产生的结果。
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
        # 变量说明：resource_schema 表示当前步骤使用的 resource_schema 值。
        resource_schema = registry.schema_for("ReadMcpResource")
        # 变量说明：resource_parameters 表示当前流程使用的 resource_parameters 集合。
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
        # 函数职责：异步完成 invoke 对应的业务处理。
        # 参数关系：arguments 表示当前流程使用的 arguments 集合；selected 表示当前步骤使用的 selected 值。
        # 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
        async def invoke(arguments: dict[str, Any], selected: McpToolBinding = binding):
            # 变量说明：waking 表示当前步骤使用的 waking 值。
            waking = announce_wakeup(selected.server_name)
            # 变量说明：result 表示本步骤产生的结果。
            result = await runtime.call_tool(selected.server_name, selected.raw_name, arguments)
            if waking:
                announce_wakeup_result(selected.server_name)
            # 变量说明：tool_name 表示当前步骤使用的 tool_name 值。
            result.tool_name = selected.model_name
            result.metadata.update({"mcp_server": selected.server_name, "mcp_tool": selected.raw_name})
            if result.ok and not selected.read_only:
                # 变量说明：changed 表示当前步骤使用的 changed 值。
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
    # 变量说明：active_tools 表示当前流程使用的 active_tools 集合。
    active_tools = [str(item) for item in frozen_active_tools] if isinstance(frozen_active_tools, list) else []
    if frozen_tools is not None and frozen_active_tools is None:
        # Snapshots from the eager-exposure implementation predate
        # mcp_active_tools. Preserve their model-visible catalog on resume.
        # 变量说明：active_tools 表示当前流程使用的 active_tools 集合。
        active_tools = [binding.model_name for binding in bindings]
    # 变量说明：unknown_active 表示当前步骤使用的 unknown_active 值。
    unknown_active = sorted(set(active_tools).difference(binding.model_name for binding in bindings))
    if unknown_active:
        raise RuntimeError("MCP active tool set no longer exists in the frozen catalog")
    registry.activate_deferred_tools(active_tools)
    registry.set_external_state("mcp_tools", snapshots)
    registry.set_external_state("mcp_server_names", enabled_servers)
    # 变量说明：namespaces 表示当前流程使用的 namespaces 集合。
    namespaces = sorted({item.model_name.rsplit("__", 1)[0] for item in bindings})
    registry.set_external_state("mcp_namespaces", [
        {
            "namespace": namespace,
            "tool_count": sum(1 for item in bindings if item.model_name.startswith(f"{namespace}__")),
        }
        for namespace in namespaces
    ])
