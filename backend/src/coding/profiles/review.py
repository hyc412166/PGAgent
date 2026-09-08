"""Code-review workflow policy."""
# 文件职责：负责代码任务状态、补丁、工作树及验证中的 review 子模块。
# 逻辑关系：上层通过 coding/profiles/review.py 使用本模块；本模块把处理结果交给同领域服务、持久化层或 API 响应层。

from __future__ import annotations

from .base import WorkflowProfile


# 变量说明：REVIEW_PROFILE 表示当前步骤使用的 REVIEW_PROFILE 值。
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
