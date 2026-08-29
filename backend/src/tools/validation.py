"""Invocation preparation checks that must run before approval is requested."""

from __future__ import annotations

from . import builtins
from .hooks import Continue, HookContext, HookDecision, Reject
from .invocation import ToolInvocation


class InvocationValidationHook:
    async def before_invoke(
        self,
        invocation: ToolInvocation,
        context: HookContext,
    ) -> HookDecision:
        arguments = invocation.arguments
        if "_raw" in arguments or "_invalid_json" in arguments:
            return Reject(
                "工具参数不是有效的 JSON 对象，已拒绝执行",
                "invalid_tool_arguments",
            )

        if invocation.wire_name in {"task", "Agent"}:
            task = arguments.get("task")
            agent_id = arguments.get("agent_id")
            if invocation.wire_name == "Agent":
                task = arguments.get("prompt")
                agent_id = arguments.get("subagent_type") or arguments.get("name")
            _, error_code, error = builtins.normalize_delegate_requests(
                task,
                agent_id,
                arguments.get("tasks"),
            )
            if error_code and error:
                return Reject(error, error_code)

        return Continue()
