"""Canonical invocation and dispatch outcomes for every tool source."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .name import ToolName
from .types import ApprovalRequest, ToolResult


@dataclass(frozen=True, slots=True)
class ApprovalGrant:
    """A grant bound to the exact persisted invocation, without a derived hash."""

    approval_id: str
    call_id: str
    tool_name: ToolName
    arguments: dict[str, Any]

    def matches(self, invocation: "ToolInvocation") -> bool:
        return (
            self.call_id == invocation.call_id
            and self.tool_name == invocation.tool_name
            and self.arguments == invocation.arguments
        )


@dataclass(slots=True)
class ToolInvocation:
    call_id: str
    tool_name: ToolName
    wire_name: str
    arguments: dict[str, Any]
    source: str = "model"
    run_id: str | None = None
    session_id: str | None = None
    step_number: int | None = None
    approval: ApprovalGrant | None = None
    parent_invocation_id: str | None = None
    context: dict[str, Any] = field(default_factory=dict)


class ToolDispatchOutcome:
    """Marker base for pipeline outcomes consumed by AgentRuntime."""


@dataclass(slots=True)
class Executed(ToolDispatchOutcome):
    result: ToolResult


@dataclass(slots=True)
class AwaitingApproval(ToolDispatchOutcome):
    request: ApprovalRequest
    invocation: ToolInvocation


@dataclass(slots=True)
class Rejected(ToolDispatchOutcome):
    result: ToolResult


@dataclass(slots=True)
class Cancelled(ToolDispatchOutcome):
    result: ToolResult


@dataclass(slots=True)
class DispatchFailed(ToolDispatchOutcome):
    result: ToolResult
