"""Protocol adapter that routes model calls through the invocation pipeline."""

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


class ToolRouter:
    def __init__(self, registry: Any, pipeline: InvocationPipeline) -> None:
        self.registry = registry
        self.pipeline = pipeline
        self._plan_builder = ToolPlanBuilder()
        self.scheduler = InvocationScheduler()

    def capture_plan(self) -> ToolPlan:
        return self._plan_builder.build(self.registry)

    def build_invocation(
        self,
        wire_name: str,
        arguments: dict[str, Any] | None,
        *,
        call_id: str,
        approved: bool = False,
        source: str = "model",
    ) -> ToolInvocation | ToolResult:
        runtime = self.registry.resolve_wire_name(wire_name)
        if runtime is None:
            error_code = "tool_not_enabled" if self.registry.knows_wire_name(wire_name) else "unknown_tool"
            return ToolResult(
                wire_name,
                False,
                f"当前会话未启用工具: {wire_name}",
                error_code=error_code,
            )
        invocation = ToolInvocation(
            call_id=call_id,
            tool_name=runtime.identity.canonical_name,
            wire_name=wire_name,
            arguments=dict(arguments or {}),
            source=source,
        )
        if approved:
            invocation.approval = ApprovalGrant(
                approval_id=call_id,
                call_id=call_id,
                tool_name=runtime.identity.canonical_name,
                arguments=dict(invocation.arguments),
            )
        return invocation

    async def dispatch(
        self,
        wire_name: str,
        arguments: dict[str, Any] | None = None,
        *,
        call_id: str,
        approved: bool = False,
        source: str = "model",
    ) -> ToolDispatchOutcome:
        built = self.build_invocation(
            wire_name,
            arguments,
            call_id=call_id,
            approved=approved,
            source=source,
        )
        if isinstance(built, ToolResult):
            return Executed(built)
        runtime = self.registry.resolve(built.tool_name)
        if runtime is None:
            return Executed(ToolResult(
                wire_name,
                False,
                f"当前会话未启用工具: {wire_name}",
                error_code="tool_not_enabled",
            ))
        return await self.pipeline.dispatch(built, runtime)

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
