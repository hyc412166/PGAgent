"""Batch scheduling policy derived from ToolRuntime execution metadata."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .registry import ToolRegistry


@dataclass(frozen=True, slots=True)
class InvocationBatchPlan:
    parallel: bool
    reason: str


class InvocationScheduler:
    def plan(
        self,
        names: Iterable[str],
        *,
        registry: "ToolRegistry",
        permission_mode: str,
    ) -> InvocationBatchPlan:
        wire_names = tuple(str(name) for name in names)
        if len(wire_names) < 2:
            return InvocationBatchPlan(False, "single_invocation")
        if permission_mode == "ask":
            return InvocationBatchPlan(False, "approval_must_be_ordered")

        runtimes = tuple(registry.resolve_wire_name(name) for name in wire_names)
        if any(runtime is None for runtime in runtimes):
            return InvocationBatchPlan(False, "unresolved_tool")
        if all(runtime.execution.supports_parallel for runtime in runtimes if runtime is not None):
            return InvocationBatchPlan(True, "runtime_declared_parallel")

        delegated = all(name in {"task", "Agent"} for name in wire_names)
        if delegated and permission_mode == "full":
            return InvocationBatchPlan(True, "approved_delegation_batch")
        return InvocationBatchPlan(False, "ordered_side_effects")
