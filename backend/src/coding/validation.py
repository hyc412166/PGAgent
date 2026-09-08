"""Project validation tool built on the existing bounded command primitive."""
# 文件职责：负责代码任务状态、补丁、工作树及验证中的 validation 子模块。
# 逻辑关系：上层通过 coding/validation.py 使用本模块；本模块把处理结果交给同领域服务、持久化层或 API 响应层。

from __future__ import annotations

import threading
import time
from typing import Any

from src.tools.sandbox import WorkspaceSandbox
from src.tools.types import ToolResult

from .worktree import annotate_command_changes, capture_worktree_state
from .validation_runtime import run_in_validation_runtime


# 变量说明：VALIDATION_KINDS 表示当前流程使用的 VALIDATION_KINDS 集合。
VALIDATION_KINDS = frozenset({"test", "lint", "typecheck", "build", "format_check", "other"})


# 函数职责：执行 validation 对应的数据或流程。
# 参数关系：sandbox 表示当前步骤使用的 sandbox 值；command 表示当前步骤使用的 command 值；kind 表示当前步骤使用的 kind 值；cwd 表示当前步骤使用的 cwd 值；timeout_seconds 表示当前流程使用的 timeout_seconds 集合；approved 表示当前步骤使用的 approved 值；validation_runtime 表示当前步骤使用的 validation_runtime 值；track_worktree_changes 表示当前流程使用的 track_worktree_changes 集合；其余参数沿用调用方提供的扩展选项。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def run_validation(
    sandbox: WorkspaceSandbox,
    command: str | list[str],
    *,
    kind: str = "test",
    cwd: str = ".",
    timeout_seconds: float = 120,
    approved: bool = False,
    validation_runtime: dict[str, Any] | None = None,
    track_worktree_changes: bool = True,
    _cancel_event: threading.Event | None = None,
) -> ToolResult:
    # 变量说明：normalized_kind 表示当前步骤使用的 normalized_kind 值。
    normalized_kind = str(kind or "test").strip().lower()
    if normalized_kind not in VALIDATION_KINDS:
        return ToolResult("validate", False, f"unsupported validation kind: {kind}", error_code="invalid_arguments")
    # 变量说明：started 表示当前步骤使用的 started 值。
    started = time.monotonic()
    # 变量说明：before 表示当前步骤使用的 before 值。
    before = capture_worktree_state(sandbox.root) if track_worktree_changes else None
    # 变量说明：result 表示本步骤产生的结果。
    result = run_in_validation_runtime(
        sandbox,
        command,
        runtime=validation_runtime,
        cwd=cwd,
        timeout_seconds=timeout_seconds,
        approved=approved,
        cancel_event=_cancel_event,
    )
    if result.approval_required:
        # 变量说明：tool_name 表示当前步骤使用的 tool_name 值。
        result.tool_name = "validate"
        if result.approval_request is not None:
            # 变量说明：tool_name 表示当前步骤使用的 tool_name 值。
            result.approval_request.tool_name = "validate"
        return result
    # 变量说明：elapsed_ms 表示当前流程使用的 elapsed_ms 集合。
    elapsed_ms = round((time.monotonic() - started) * 1000)
    # 变量说明：validation 表示当前步骤使用的 validation 值。
    validation: dict[str, Any] = {
        "kind": normalized_kind,
        "status": "passed" if result.ok else "failed",
        "cwd": str(result.metadata.get("cwd") or cwd or "."),
        "exit_code": result.metadata.get("exit_code"),
        "duration_ms": elapsed_ms,
    }
    # 变量说明：normalized 表示当前步骤使用的 normalized 值。
    normalized = ToolResult(
        "validate",
        result.ok,
        result.content,
        changed=result.changed,
        error_code=result.error_code,
        metadata={**dict(result.metadata), "validation": validation},
    )
    if track_worktree_changes:
        return annotate_command_changes(
            normalized,
            sandbox.root,
            before,
            source="validation_command",
        )
    return normalized
