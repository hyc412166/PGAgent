"""Mandatory trusted authorization hook for invocation-time approval decisions."""

from __future__ import annotations

from .hooks import Continue, HookContext, HookDecision, RequireApproval
from .invocation import ToolInvocation
from .policy import assess_tool_call


class AuthorizationHook:
    async def before_invoke(
        self,
        invocation: ToolInvocation,
        context: HookContext,
    ) -> HookDecision:
        runtime = context.runtime
        if runtime.execution.approval_exempt:
            return Continue()

        if invocation.approval is not None and invocation.approval.matches(invocation):
            return Continue()

        if runtime.origin.source == "builtin":
            decision = assess_tool_call(
                invocation.wire_name,
                context.permission_mode,
                arguments=invocation.arguments,
                approved=False,
                workspace_root=context.workspace_root,
            )
            if decision.requires_approval:
                return RequireApproval(decision.reason)
            return Continue()

        mode = context.permission_mode
        if mode == "ask":
            return RequireApproval(
                "请求批准模式：外部工具调用会越过本地 Agent 边界，执行前需要确认。"
            )
        if mode == "smart" and not runtime.execution.read_only:
            return RequireApproval(
                "该外部工具未声明为只读，可能修改外部或本地状态，需要确认。"
            )
        return Continue()
