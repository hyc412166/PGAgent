"""Coding workflow policy."""

from __future__ import annotations

from .base import WorkflowProfile


CODING_PROFILE = WorkflowProfile(
    id="coding",
    direct_tool_names=frozenset({
        "tool_search",
        "apply_patch",
        "background_run",
        "shell",
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
        "update_plan",
        "validate",
        "validate_baseline",
        "write",
        "write_stdin",
        "web.run",
    }),
    instructions=(
        "## Coding workflow\n"
        "Work toward a completed, verified repository change. Read project instructions and inspect the "
        "owning implementation, callers, and relevant tests before editing. Use rg when available for fast repository "
        "navigation and use tool_search for lower-frequency capabilities. Keep the plan proportional to "
        "the task, preserve unrelated user changes, and prefer apply_patch for existing source files. "
        "After editing, inspect git status/diff and run the narrowest relevant checks with validate. "
        "A regression reproducer must preserve the issue's original data shape, ownership layer, and dispatch path; "
        "do not replace an instance attribute, inherited member, async boundary, or other failing form with an easier "
        "analogue. Trace where the affected value is collected, filtered, transformed, and emitted before choosing "
        "the implementation layer to modify. "
        "When proving that a failure is pre-existing on pristine HEAD, use validate_baseline instead of "
        "temporarily reverting candidate files in the main worktree. "
        "Do not claim success from code inspection alone when a runnable check is available. Report the "
        "changed files, observed behavior, and whether validation passed, failed, or was not run."
    ),
)
