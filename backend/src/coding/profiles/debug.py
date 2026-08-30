"""Debugging workflow policy."""

from __future__ import annotations

from .base import WorkflowProfile


DEBUG_PROFILE = WorkflowProfile(
    id="debug",
    direct_tool_names=frozenset({
        "ToolSearch",
        "apply_patch",
        "background_run",
        "bash",
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
        "todowrite",
        "validate",
        "webfetch",
        "websearch",
        "write",
        "write_stdin",
    }),
    instructions=(
        "## Debug workflow\n"
        "Diagnose from observable evidence before changing code. Reproduce the symptom when possible, "
        "inspect the owning code path and recent diff, and, when available, use debug_evidence to record reproductions, "
        "observations, hypotheses, and the confirmed root cause. Keep hypotheses distinct from confirmed "
        "facts and actively disprove alternatives. Once the root cause is supported, make the smallest "
        "coherent fix, rerun the original reproducer, and add or run a regression check with validate. "
        "Report the root cause, fix, reproduction result, regression result, and any unresolved uncertainty."
    ),
)
