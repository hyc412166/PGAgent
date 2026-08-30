"""Coding workflow policy."""

from __future__ import annotations

from .base import WorkflowProfile


CODING_PROFILE = WorkflowProfile(
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
        "Work toward a completed, verified repository change. Read project instructions and inspect the "
        "owning implementation, callers, and relevant tests before editing. Use rg when available for fast repository "
        "navigation and use ToolSearch for lower-frequency capabilities. Keep the plan proportional to "
        "the task, preserve unrelated user changes, and prefer apply_patch for existing source files. "
        "After editing, inspect git status/diff and run the narrowest relevant checks with validate. "
        "Do not claim success from code inspection alone when a runnable check is available. Report the "
        "changed files, observed behavior, and whether validation passed, failed, or was not run."
    ),
)
