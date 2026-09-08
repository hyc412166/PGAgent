"""Protocol adapter that routes model calls through the invocation pipeline."""
# 文件职责：负责工具定义、授权、注册、调度与执行中的 router 子模块。
# 逻辑关系：上层通过 tools/router.py 使用本模块；本模块把处理结果交给同领域服务、持久化层或 API 响应层。

from __future__ import annotations

from typing import Any

from .invocation import (
    ApprovalGrant,
    AwaitingApproval,
    Executed,
    ToolDispatchOutcome,
    ToolInvocation,
)
from .name import ToolName
from .pipeline import InvocationPipeline
from .plan import ToolPlan, ToolPlanBuilder
from .types import ToolResult
from .scheduler import InvocationScheduler


# 类职责：定义 ToolRouter 在本领域中的数据与行为。
class ToolRouter:
    # 函数职责：初始化实例依赖与初始状态。
    # 参数关系：registry 表示当前步骤使用的 registry 值；pipeline 表示当前步骤使用的 pipeline 值。
    # 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
    def __init__(self, registry: Any, pipeline: InvocationPipeline) -> None:
        # 变量说明：registry 表示当前步骤使用的 registry 值。
        self.registry = registry
        # 变量说明：pipeline 表示当前步骤使用的 pipeline 值。
        self.pipeline = pipeline
        # 变量说明：_plan_builder 表示当前步骤使用的 _plan_builder 值。
        self._plan_builder = ToolPlanBuilder()
        # 变量说明：scheduler 表示当前步骤使用的 scheduler 值。
        self.scheduler = InvocationScheduler()

    # 函数职责：完成 capture_plan 对应的业务处理。
    # 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
    def capture_plan(self) -> ToolPlan:
        return self._plan_builder.build(self.registry)

    # 函数职责：构建 invocation 对应的数据或流程。
    # 参数关系：wire_name 表示当前步骤使用的 wire_name 值；arguments 表示当前流程使用的 arguments 集合；call_id 表示call 对象的唯一标识；approved 表示当前步骤使用的 approved 值；source 表示当前步骤使用的 source 值。
    # 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
    def build_invocation(
        self,
        wire_name: str,
        arguments: dict[str, Any] | None,
        *,
        call_id: str,
        approved: bool = False,
        source: str = "model",
    ) -> ToolInvocation | ToolResult:
        # 变量说明：runtime 表示当前步骤使用的 runtime 值。
        runtime = self.registry.resolve_wire_name(wire_name)
        if runtime is None:
            # 变量说明：error_code 表示当前步骤使用的 error_code 值。
            error_code = "tool_not_enabled" if self.registry.knows_wire_name(wire_name) else "unknown_tool"
            return ToolResult(
                wire_name,
                False,
                f"当前会话未启用工具: {wire_name}",
                error_code=error_code,
            )
        # 变量说明：invocation 表示当前步骤使用的 invocation 值。
        invocation = ToolInvocation(
            call_id=call_id,
            tool_name=runtime.identity.canonical_name,
            wire_name=wire_name,
            arguments=dict(arguments or {}),
            source=source,
        )
        if approved:
            # 变量说明：approval 表示当前步骤使用的 approval 值。
            invocation.approval = ApprovalGrant(
                approval_id=call_id,
                call_id=call_id,
                tool_name=runtime.identity.canonical_name,
                arguments=dict(invocation.arguments),
            )
        return invocation

    # 函数职责：异步完成 dispatch 对应的业务处理。
    # 参数关系：wire_name 表示当前步骤使用的 wire_name 值；arguments 表示当前流程使用的 arguments 集合；call_id 表示call 对象的唯一标识；approved 表示当前步骤使用的 approved 值；source 表示当前步骤使用的 source 值。
    # 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
    async def dispatch(
        self,
        wire_name: str,
        arguments: dict[str, Any] | None = None,
        *,
        call_id: str,
        approved: bool = False,
        source: str = "model",
    ) -> ToolDispatchOutcome:
        # 变量说明：built 表示当前步骤使用的 built 值。
        built = self.build_invocation(
            wire_name,
            arguments,
            call_id=call_id,
            approved=approved,
            source=source,
        )
        if isinstance(built, ToolResult):
            return Executed(built)
        # 变量说明：runtime 表示当前步骤使用的 runtime 值。
        runtime = self.registry.resolve(built.tool_name)
        if runtime is None:
            return Executed(ToolResult(
                wire_name,
                False,
                f"当前会话未启用工具: {wire_name}",
                error_code="tool_not_enabled",
            ))
        return await self.pipeline.dispatch(built, runtime)

    # 函数职责：完成 result 对应的业务处理。
    # 参数关系：outcome 表示当前步骤使用的 outcome 值。
    # 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
    @staticmethod
    def result(outcome: ToolDispatchOutcome) -> ToolResult:
        if isinstance(outcome, AwaitingApproval):
            return ToolResult(
                outcome.invocation.wire_name,
                False,
                outcome.request.reason,
                approval_required=True,
                approval_request=outcome.request,
                error_code="approval_required",
            )
        return outcome.result
