"""Run a baseline check without replacing the candidate working tree."""

from __future__ import annotations

from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import time

from src.tools import builtins
from src.tools.sandbox import WorkspaceSandbox
from src.tools.types import ToolResult

from .validation import VALIDATION_KINDS


def run_baseline_validation(
    sandbox: WorkspaceSandbox,
    command: str | list[str],
    *,
    kind: str = "test",
    cwd: str = ".",
    timeout_seconds: float = 120,
    approved: bool = False,
    _cancel_event: threading.Event | None = None,
) -> ToolResult:
    """Validate ``HEAD`` in an isolated temporary worktree.

    This capability exists for one concrete lifecycle boundary: an Agent may
    need to prove that a failure exists on pristine code, but replacing files
    in the candidate worktree can leave the user's result reverted if the run
    is interrupted. The temporary worktree makes that comparison isolated.
    """

    normalized_kind = str(kind or "test").strip().lower()
    if normalized_kind not in VALIDATION_KINDS:
        return ToolResult(
            "validate_baseline",
            False,
            f"unsupported validation kind: {kind}",
            error_code="invalid_arguments",
        )
    repository = _git(sandbox.root, ["rev-parse", "--show-toplevel"])
    if repository.returncode != 0:
        return ToolResult(
            "validate_baseline",
            False,
            "当前工作区不是 Git 仓库，无法创建隔离基线。",
            error_code="not_git_repository",
        )
    try:
        top_level = Path(repository.stdout.strip()).resolve()
    except OSError as exc:
        return ToolResult(
            "validate_baseline",
            False,
            f"无法解析 Git 根目录：{exc}",
            error_code="baseline_setup_failed",
        )
    if top_level != sandbox.root.resolve():
        return ToolResult(
            "validate_baseline",
            False,
            "validate_baseline 需要工作区根目录与 Git 根目录一致。",
            error_code="workspace_not_repository_root",
        )

    temporary_root = Path(tempfile.mkdtemp(prefix="pgagent-baseline-"))
    baseline_root = temporary_root / "worktree"
    added = False
    started = time.monotonic()
    normalized: ToolResult | None = None
    cleanup_error: str | None = None
    try:
        setup = _git(sandbox.root, ["worktree", "add", "--detach", str(baseline_root), "HEAD"])
        if setup.returncode != 0:
            normalized = ToolResult(
                "validate_baseline",
                False,
                setup.stderr or setup.stdout or "创建隔离基线 worktree 失败。",
                error_code="baseline_setup_failed",
            )
        else:
            added = True
            baseline_sandbox = WorkspaceSandbox(baseline_root)
            result = builtins.run_command(
                baseline_sandbox,
                command,
                approved=True,
                cwd=cwd,
                timeout_seconds=timeout_seconds,
                _cancel_event=_cancel_event,
            )
            elapsed_ms = round((time.monotonic() - started) * 1000)
            baseline_validation = {
                "kind": normalized_kind,
                "status": "passed" if result.ok else "failed",
                "cwd": str(result.metadata.get("cwd") or cwd or "."),
                "exit_code": result.metadata.get("exit_code"),
                "duration_ms": elapsed_ms,
                "ref": "HEAD",
                "isolated": True,
            }
            normalized = ToolResult(
                "validate_baseline",
                result.ok,
                result.content,
                changed=False,
                error_code=result.error_code,
                metadata={
                    **dict(result.metadata),
                    "baseline_validation": baseline_validation,
                },
            )
    finally:
        if added:
            cleanup_error = _remove_worktree(sandbox.root, baseline_root)
        shutil.rmtree(temporary_root, ignore_errors=True)
    assert normalized is not None
    if cleanup_error:
        return ToolResult(
            "validate_baseline",
            False,
            f"{normalized.content}\n隔离基线清理失败：{cleanup_error}".strip(),
            changed=False,
            error_code="baseline_cleanup_failed",
            metadata=dict(normalized.metadata),
        )
    return normalized


def _remove_worktree(repository_root: Path, baseline_root: Path) -> str | None:
    # A validation command can lock its own temporary worktree. Unlock first;
    # an "is not locked" error is harmless and removal remains authoritative.
    _git(repository_root, ["worktree", "unlock", str(baseline_root)])
    removed = _git(
        repository_root,
        ["worktree", "remove", "--force", str(baseline_root)],
    )
    if removed.returncode != 0:
        shutil.rmtree(baseline_root, ignore_errors=True)
    listed = _git(repository_root, ["worktree", "list", "--porcelain"])
    if listed.returncode != 0:
        return listed.stderr or "无法确认临时 worktree 是否已经移除"
    expected = baseline_root.resolve()
    for line in listed.stdout.splitlines():
        if not line.startswith("worktree "):
            continue
        try:
            registered = Path(line[len("worktree "):].strip()).resolve()
        except OSError:
            continue
        if registered == expected:
            return removed.stderr or removed.stdout or "临时 worktree 仍在 Git 注册表中"
    return None


def _git(root: Path, arguments: list[str]) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            ["git", *arguments],
            cwd=root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=120,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return subprocess.CompletedProcess(
            ["git", *arguments],
            returncode=1,
            stdout="",
            stderr=f"{type(exc).__name__}: {exc}",
        )
