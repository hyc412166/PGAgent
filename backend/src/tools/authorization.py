"""Mandatory trusted authorization hook for invocation-time approval decisions."""
# 文件职责：负责工具定义、授权、注册、调度与执行中的 authorization 子模块。
# 逻辑关系：上层通过 tools/authorization.py 使用本模块；本模块把处理结果交给同领域服务、持久化层或 API 响应层。

from __future__ import annotations

from .hooks import Continue, HookContext, HookDecision, RequireApproval
from .invocation import ToolInvocation
from .policy import assess_tool_call


# 类职责：定义 AuthorizationHook 在本领域中的数据与行为。
class AuthorizationHook:
    # 函数职责：异步完成 before_invoke 对应的业务处理。
    # 参数关系：invocation 表示当前步骤使用的 invocation 值；context 表示当前步骤使用的 context 值。
    # 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
    async def before_invoke(
        self,
        invocation: ToolInvocation,
        context: HookContext,
    ) -> HookDecision:
        # 变量说明：runtime 表示当前步骤使用的 runtime 值。
        runtime = context.runtime
        if runtime.execution.approval_exempt:
            return Continue()

        if invocation.approval is not None and invocation.approval.matches(invocation):
            return Continue()

        if runtime.origin.source == "builtin":
            # 变量说明：decision 表示当前步骤使用的 decision 值。
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

        # 变量说明：mode 表示当前步骤使用的 mode 值。
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
