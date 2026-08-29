"""Immutable tool view captured for one model sampling request."""

from __future__ import annotations

from dataclasses import dataclass

from src.tools.plan import ToolPlan


@dataclass(frozen=True, slots=True)
class AgentStepContext:
    step_number: int
    tool_plan: ToolPlan
