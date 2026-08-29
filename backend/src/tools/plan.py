"""Request-scoped tool plan advertised to and executed for one model step."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from .name import ToolName

if TYPE_CHECKING:
    from .registry import ToolRegistry


@dataclass(frozen=True, slots=True)
class ToolPlan:
    registry: "ToolRegistry"
    model_specs: tuple[dict, ...]
    wire_names: dict[str, ToolName]
    discoverable_tools: tuple[ToolName, ...]
    activated_tools: tuple[ToolName, ...]


class ToolPlanBuilder:
    def build(self, registry: "ToolRegistry") -> ToolPlan:
        runtimes = tuple(registry.runtimes)
        active = set(registry.activated_tool_names)
        return ToolPlan(
            registry=registry,
            model_specs=tuple(registry.schemas),
            wire_names={runtime.identity.wire_name: runtime.identity.canonical_name for runtime in runtimes},
            discoverable_tools=tuple(
                runtime.identity.canonical_name
                for runtime in runtimes
                if runtime.presentation.discoverable
            ),
            activated_tools=tuple(
                runtime.identity.canonical_name
                for runtime in runtimes
                if runtime.identity.wire_name in active
            ),
        )
