"""Shared workflow-profile contract layered on the common AgentRuntime."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class WorkflowProfile:
    """Prompt and tool-presentation policy for one engineering workflow."""

    id: str
    direct_tool_names: frozenset[str]
    instructions: str
    read_only_tool_ceiling: bool = False
    allowed_non_read_only_tool_names: frozenset[str] = frozenset()
