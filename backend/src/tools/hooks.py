"""Extensible invocation hooks with an explicit prepare/authorize/post order."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from .invocation import ToolInvocation
from .runtime import ToolRuntime
from .types import ToolResult


@dataclass(slots=True)
class HookContext:
    runtime: ToolRuntime
    workspace_root: str
    permission_mode: str
    values: dict[str, Any] = field(default_factory=dict)


class HookDecision:
    pass


@dataclass(frozen=True, slots=True)
class Continue(HookDecision):
    pass


@dataclass(frozen=True, slots=True)
class Rewrite(HookDecision):
    arguments: dict[str, Any]


@dataclass(frozen=True, slots=True)
class RequireApproval(HookDecision):
    reason: str


@dataclass(frozen=True, slots=True)
class Reject(HookDecision):
    reason: str
    error_code: str = "permission_denied"


class PrepareHook(Protocol):
    async def before_invoke(
        self,
        invocation: ToolInvocation,
        context: HookContext,
    ) -> HookDecision: ...


class PostHook(Protocol):
    async def after_invoke(
        self,
        invocation: ToolInvocation,
        result: ToolResult,
        context: HookContext,
    ) -> ToolResult: ...


class LifecycleHook(Protocol):
    async def tool_started(self, invocation: ToolInvocation, context: HookContext) -> None: ...

    async def tool_finished(
        self,
        invocation: ToolInvocation,
        result: ToolResult,
        context: HookContext,
    ) -> None: ...
