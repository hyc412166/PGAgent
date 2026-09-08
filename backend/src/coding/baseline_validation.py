"""Run a baseline check without replacing the candidate working tree."""
# 文件职责：负责代码任务状态、补丁、工作树及验证中的 baseline_validation 子模块。
# 逻辑关系：上层通过 coding/baseline_validation.py 使用本模块；本模块把处理结果交给同领域服务、持久化层或 API 响应层。

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


# 函数职责：执行 baseline_validation 对应的数据或流程。
# 参数关系：sandbox 表示当前步骤使用的 sandbox 值；command 表示当前步骤使用的 command 值；kind 表示当前步骤使用的 kind 值；cwd 表示当前步骤使用的 cwd 值；timeout_seconds 表示当前流程使用的 timeout_seconds 集合；approved 表示当前步骤使用的 approved 值；_cancel_event 表示当前步骤使用的 _cancel_event 值。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
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

    # 变量说明：normalized_kind 表示当前步骤使用的 normalized_kind 值。
    normalized_kind = str(kind or "test").strip().lower()
    if normalized_kind not in VALIDATION_KINDS:
        return ToolResult(
            "validate_baseline",
            False,
            f"unsupported validation kind: {kind}",
            error_code="invalid_arguments",
        )
    # 变量说明：repository 表示当前步骤使用的 repository 值。
    repository = _git(sandbox.root, ["rev-parse", "--show-toplevel"])
    if repository.returncode != 0:
        return ToolResult(
            "validate_baseline",
            False,
            "当前工作区不是 Git 仓库，无法创建隔离基线。",
            error_code="not_git_repository",
        )
    try:
        # 变量说明：top_level 表示当前步骤使用的 top_level 值。
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

    # 变量说明：temporary_root 表示当前步骤使用的 temporary_root 值。
    temporary_root = Path(tempfile.mkdtemp(prefix="pgagent-baseline-"))
    # 变量说明：baseline_root 表示当前步骤使用的 baseline_root 值。
    baseline_root = temporary_root / "worktree"
    # 变量说明：added 表示当前步骤使用的 added 值。
    added = False
    # 变量说明：started 表示当前步骤使用的 started 值。
    started = time.monotonic()
    # 变量说明：normalized 表示当前步骤使用的 normalized 值。
    normalized: ToolResult | None = None
    # 变量说明：cleanup_error 表示当前步骤使用的 cleanup_error 值。
    cleanup_error: str | None = None
    try:
        # 变量说明：setup 表示当前步骤使用的 setup 值。
        setup = _git(sandbox.root, ["worktree", "add", "--detach", str(baseline_root), "HEAD"])
        if setup.returncode != 0:
            # 变量说明：normalized 表示当前步骤使用的 normalized 值。
            normalized = ToolResult(
                "validate_baseline",
                False,
                setup.stderr or setup.stdout or "创建隔离基线 worktree 失败。",
                error_code="baseline_setup_failed",
            )
        else:
            # 变量说明：added 表示当前步骤使用的 added 值。
            added = True
            # 变量说明：baseline_sandbox 表示当前步骤使用的 baseline_sandbox 值。
            baseline_sandbox = WorkspaceSandbox(baseline_root)
            # 变量说明：result 表示本步骤产生的结果。
            result = builtins.run_command(
                baseline_sandbox,
                command,
                approved=True,
                cwd=cwd,
                timeout_seconds=timeout_seconds,
                _cancel_event=_cancel_event,
            )
            # 变量说明：elapsed_ms 表示当前流程使用的 elapsed_ms 集合。
            elapsed_ms = round((time.monotonic() - started) * 1000)
            # 变量说明：baseline_validation 表示当前步骤使用的 baseline_validation 值。
            baseline_validation = {
                "kind": normalized_kind,
                "status": "passed" if result.ok else "failed",
                "cwd": str(result.metadata.get("cwd") or cwd or "."),
                "exit_code": result.metadata.get("exit_code"),
                "duration_ms": elapsed_ms,
                "ref": "HEAD",
                "isolated": True,
            }
            # 变量说明：normalized 表示当前步骤使用的 normalized 值。
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
            # 变量说明：cleanup_error 表示当前步骤使用的 cleanup_error 值。
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


# 函数职责：移除 worktree 对应的数据或流程。
# 参数关系：repository_root 表示当前步骤使用的 repository_root 值；baseline_root 表示当前步骤使用的 baseline_root 值。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def _remove_worktree(repository_root: Path, baseline_root: Path) -> str | None:
    # A validation command can lock its own temporary worktree. Unlock first;
    # an "is not locked" error is harmless and removal remains authoritative.
    _git(repository_root, ["worktree", "unlock", str(baseline_root)])
    # 变量说明：removed 表示当前步骤使用的 removed 值。
    removed = _git(
        repository_root,
        ["worktree", "remove", "--force", str(baseline_root)],
    )
    if removed.returncode != 0:
        shutil.rmtree(baseline_root, ignore_errors=True)
    # 变量说明：listed 表示当前步骤使用的 listed 值。
    listed = _git(repository_root, ["worktree", "list", "--porcelain"])
    if listed.returncode != 0:
        return listed.stderr or "无法确认临时 worktree 是否已经移除"
    # 变量说明：expected 表示当前步骤使用的 expected 值。
    expected = baseline_root.resolve()
    for line in listed.stdout.splitlines():
        if not line.startswith("worktree "):
            continue
        try:
            # 变量说明：registered 表示当前步骤使用的 registered 值。
            registered = Path(line[len("worktree "):].strip()).resolve()
        except OSError:
            continue
        if registered == expected:
            return removed.stderr or removed.stdout or "临时 worktree 仍在 Git 注册表中"
    return None


# 函数职责：完成 git 对应的业务处理。
# 参数关系：root 表示处理范围的根目录；arguments 表示当前流程使用的 arguments 集合。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
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
