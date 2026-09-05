"""Code-review workflow policy."""

from __future__ import annotations

from .base import WorkflowProfile


REVIEW_PROFILE = WorkflowProfile(
    id="review",
    direct_tool_names=frozenset({
        "tool_search",
        "file_info",
        "git_diff",
        "git_status",
        "glob",
        "grep",
        "question",
        "read",
        "read_artifact",
        "review_finding",
        "rg",
        "skill",
        "validate",
        "web.run",
    }),
    instructions=(
        "## Code review workflow\n"
        "Review the requested diff or code path without modifying repository files. First establish the "
        "change intent and review scope, then inspect the diff, surrounding implementation, callers, and "
        "tests. Run a targeted validation only when it can prove or disprove a concrete concern. When the "
        "tool is available, record each actionable issue with review_finding, including severity, exact location, evidence, and a "
        "reproducible failure scenario. Avoid style-only comments unless they create a real maintenance or "
        "correctness cost. In the final response, list findings by severity before the overall assessment; "
        "if there are no findings, say so explicitly and name any remaining test risk."
    ),
    read_only_tool_ceiling=True,
    allowed_non_read_only_tool_names=frozenset({
        "question",
        "review_finding",
        "validate",
    }),
)
