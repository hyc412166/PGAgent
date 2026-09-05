"""Debugging workflow policy."""

from __future__ import annotations

from .base import WorkflowProfile


DEBUG_PROFILE = WorkflowProfile(
    id="debug",
    direct_tool_names=frozenset({
        "tool_search",
        "apply_patch",
        "background_run",
        "shell",
        "check_background",
        "debug_evidence",
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
        "update_plan",
        "validate",
        "validate_baseline",
        "write",
        "write_stdin",
        "web.run",
    }),
    instructions=(
        "## Debug workflow\n"
        "Diagnose from observable evidence before changing code. Reproduce the symptom when possible, "
        "inspect the owning code path and recent diff, and, when available, use debug_evidence to record reproductions, "
        "observations, hypotheses, and the confirmed root cause. Keep hypotheses distinct from confirmed "
        "facts and actively disprove alternatives. Once the root cause is supported, make the smallest "
        "coherent fix, rerun the original reproducer, and add or run a regression check with validate. "
        "Keep the reproducer faithful to the original data shape, ownership layer, and dispatch path, and trace "
        "where the affected value is collected, filtered, transformed, and emitted before editing. "
        "Use validate_baseline for pristine-HEAD comparisons so candidate files are never temporarily reverted. "
        "Report the root cause, fix, reproduction result, regression result, and any unresolved uncertainty."
    ),
)
