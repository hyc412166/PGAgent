"""Project validation tool built on the existing bounded command primitive."""

from __future__ import annotations

import time
from typing import Any

from src.tools import builtins
from src.tools.sandbox import WorkspaceSandbox
from src.tools.types import ToolResult

from .worktree import annotate_command_changes, capture_worktree_state


VALIDATION_KINDS = frozenset({"test", "lint", "typecheck", "build", "format_check", "other"})


def run_validation(
    sandbox: WorkspaceSandbox,
    command: str | list[str],
    *,
    kind: str = "test",
    cwd: str = ".",
    timeout_seconds: float = 120,
    approved: bool = False,
    track_worktree_changes: bool = True,
) -> ToolResult:
    normalized_kind = str(kind or "test").strip().lower()
    if normalized_kind not in VALIDATION_KINDS:
        return ToolResult("validate", False, f"unsupported validation kind: {kind}", error_code="invalid_arguments")
    started = time.monotonic()
    before = capture_worktree_state(sandbox.root) if track_worktree_changes else None
    result = builtins.run_command(
        sandbox,
        command,
        cwd=cwd,
        timeout_seconds=timeout_seconds,
        approved=approved,
    )
    if result.approval_required:
        result.tool_name = "validate"
        if result.approval_request is not None:
            result.approval_request.tool_name = "validate"
        return result
    elapsed_ms = round((time.monotonic() - started) * 1000)
    validation: dict[str, Any] = {
        "kind": normalized_kind,
        "status": "passed" if result.ok else "failed",
        "cwd": str(result.metadata.get("cwd") or cwd or "."),
        "exit_code": result.metadata.get("exit_code"),
        "duration_ms": elapsed_ms,
    }
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
