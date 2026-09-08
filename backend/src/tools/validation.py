"""Invocation preparation checks that must run before approval is requested."""
# 文件职责：负责工具定义、授权、注册、调度与执行中的 validation 子模块。
# 逻辑关系：上层通过 tools/validation.py 使用本模块；本模块把处理结果交给同领域服务、持久化层或 API 响应层。

from __future__ import annotations

from . import builtins
from .hooks import Continue, HookContext, HookDecision, Reject
from .invocation import ToolInvocation


# 类职责：定义 InvocationValidationHook 在本领域中的数据与行为。
class InvocationValidationHook:
    # 函数职责：异步完成 before_invoke 对应的业务处理。
    # 参数关系：invocation 表示当前步骤使用的 invocation 值；context 表示当前步骤使用的 context 值。
    # 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
    async def before_invoke(
        self,
        invocation: ToolInvocation,
        context: HookContext,
    ) -> HookDecision:
        # 变量说明：arguments 表示当前流程使用的 arguments 集合。
        arguments = invocation.arguments
        if "_raw" in arguments or "_invalid_json" in arguments:
            return Reject(
                "工具参数不是有效的 JSON 对象，已拒绝执行",
                "invalid_tool_arguments",
            )

        if invocation.wire_name in {"task", "Agent"}:
            # 变量说明：task 表示当前步骤使用的 task 值。
            task = arguments.get("task")
            # 变量说明：agent_id 表示智能体标识。
            agent_id = arguments.get("agent_id")
            if invocation.wire_name == "Agent":
                # 变量说明：task 表示当前步骤使用的 task 值。
                task = arguments.get("prompt")
                # 变量说明：agent_id 表示智能体标识。
                agent_id = arguments.get("subagent_type") or arguments.get("name")
            # 变量说明：_ 表示当前步骤使用的 _ 值；error_code 表示当前步骤使用的 error_code 值；error 表示当前捕获或准备上报的错误。
            _, error_code, error = builtins.normalize_delegate_requests(
                task,
                agent_id,
                arguments.get("tasks"),
            )
            if error_code and error:
                return Reject(error, error_code)

        return Continue()
