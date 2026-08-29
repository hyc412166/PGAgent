"""Unified metadata and executor object for built-in and external tools."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any

from .invocation import ToolInvocation
from .name import ToolName
from .types import ToolResult


ToolExecutor = Callable[[ToolInvocation], Awaitable[ToolResult]]


@dataclass(frozen=True, slots=True)
class ToolIdentity:
    canonical_name: ToolName
    wire_name: str
    aliases: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ToolOrigin:
    owner: str
    trusted: bool
    source: str


@dataclass(frozen=True, slots=True)
class ToolPresentation:
    advertise_by_default: bool = True
    discoverable: bool = True
    model_callable: bool = True

    @classmethod
    def direct(cls) -> "ToolPresentation":
        return cls()

    @classmethod
    def deferred(cls) -> "ToolPresentation":
        return cls(advertise_by_default=False, discoverable=True, model_callable=True)

    @classmethod
    def hidden(cls) -> "ToolPresentation":
        return cls(advertise_by_default=False, discoverable=False, model_callable=False)


@dataclass(frozen=True, slots=True)
class ToolExecutionMetadata:
    read_only: bool = False
    supports_parallel: bool = False
    approval_exempt: bool = False
    side_effect_scope: str | None = None


@dataclass(slots=True)
class ToolRuntime:
    identity: ToolIdentity
    origin: ToolOrigin
    schema: Mapping[str, Any]
    presentation: ToolPresentation
    execution: ToolExecutionMetadata
    executor: ToolExecutor

    async def invoke(self, invocation: ToolInvocation) -> ToolResult:
        result = await self.executor(invocation)
        result.tool_name = self.identity.wire_name
        if result.approval_request is not None:
            result.approval_request.tool_name = self.identity.wire_name
        return result
