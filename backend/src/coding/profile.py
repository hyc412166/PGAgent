"""Task-profile rules for repository coding work on the shared AgentRuntime."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable


@dataclass(frozen=True, slots=True)
class CodingProfile:
    """A workflow policy layered on the common AgentRuntime."""

    id: str
    direct_tool_names: frozenset[str]
    instructions: str


CODING_PROFILE = CodingProfile(
    id="coding",
    direct_tool_names=frozenset({
        "ToolSearch",
        "apply_patch",
        "background_run",
        "bash",
        "check_background",
        "delete",
        "edit",
        "file_info",
        "git_diff",
        "git_status",
        "glob",
        "grep",
        "question",
        "read",
        "read_artifact",
        "rg",
        "skill",
        "task",
        "todowrite",
        "validate",
        "webfetch",
        "websearch",
        "write",
        "write_stdin",
    }),
    instructions=(
        "## Coding workflow\n"
        "For repository changes, inspect the relevant files and project rules before editing. "
        "Use apply_patch for existing source files and keep each patch scoped to the requested change. "
        "After editing, inspect git status/diff and run the narrowest relevant checks with validate. "
        "Do not overwrite unrelated user changes. Report changed files and whether validation passed, failed, "
        "or was not run. Additional low-frequency tools are discoverable through ToolSearch; use "
        "`select:<tool-name>` to activate a deferred tool before calling it."
    ),
)


def resolve_coding_profile(enabled_tool_names: Iterable[str]) -> CodingProfile | None:
    """Select the coding workflow only when its first-class tools are enabled."""

    enabled = {str(name) for name in enabled_tool_names}
    return CODING_PROFILE if {"apply_patch", "validate"}.issubset(enabled) else None
