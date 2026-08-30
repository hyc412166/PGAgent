"""Compatibility imports for the original coding-profile module."""

from __future__ import annotations

from typing import Iterable

from .profiles import CODING_PROFILE, WorkflowProfile, resolve_workflow_profile


CodingProfile = WorkflowProfile


def resolve_coding_profile(enabled_tool_names: Iterable[str]) -> CodingProfile | None:
    """Select the legacy auto-detected coding workflow."""

    return resolve_workflow_profile("auto", enabled_tool_names)
