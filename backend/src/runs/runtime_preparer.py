"""Attach dynamic tool sources after a durable runtime has been assembled."""

from __future__ import annotations

from typing import Any, Mapping

from src.agent import AgentRuntime
from src.mcp import attach_mcp_tools
from src.tools.registry import ToolRegistry


class RunRuntimePreparer:
    async def prepare(
        self,
        *,
        run_id: str,
        runtime: AgentRuntime,
        context: Mapping[str, Any],
        progress_sink: Any,
        registry: ToolRegistry | None = None,
    ) -> None:
        binding = dict(context["runtime_binding"])
        delegated = bool(binding.get("delegation_version"))
        await attach_mcp_tools(
            registry or runtime.tool_registry,
            session_key=str(context.get("session_id") or run_id),
            workspace_root=str(context["workspace_root"]),
            agent_kind="subagent" if delegated else "main",
            runtime_scope=run_id if delegated else None,
            selected_server_names=binding.get("mcp_server_names"),
            frozen_tools=binding.get("mcp_tools"),
            frozen_active_tools=binding.get("mcp_active_tools"),
            progress_sink=progress_sink,
        )
