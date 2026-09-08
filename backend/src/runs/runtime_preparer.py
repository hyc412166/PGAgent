"""Attach dynamic tool sources after a durable runtime has been assembled."""
# 文件职责：负责运行创建、恢复、流式传输和生命周期中的 runtime_preparer 子模块。
# 逻辑关系：上层通过 runs/runtime_preparer.py 使用本模块；本模块把处理结果交给同领域服务、持久化层或 API 响应层。

from __future__ import annotations

from typing import Any, Mapping

from src.agent import AgentRuntime
from src.mcp import attach_mcp_tools
from src.tools.registry import ToolRegistry


# 类职责：定义 RunRuntimePreparer 在本领域中的数据与行为。
class RunRuntimePreparer:
    # 函数职责：异步完成 prepare 对应的业务处理。
    # 参数关系：run_id 表示当前运行标识；runtime 表示当前步骤使用的 runtime 值；context 表示当前步骤使用的 context 值；progress_sink 表示当前步骤使用的 progress_sink 值；registry 表示当前步骤使用的 registry 值。
    # 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
    async def prepare(
        self,
        *,
        run_id: str,
        runtime: AgentRuntime,
        context: Mapping[str, Any],
        progress_sink: Any,
        registry: ToolRegistry | None = None,
    ) -> None:
        # 变量说明：binding 表示当前步骤使用的 binding 值。
        binding = dict(context["runtime_binding"])
        # 变量说明：delegated 表示当前步骤使用的 delegated 值。
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
