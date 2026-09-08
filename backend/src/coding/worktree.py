"""Observe real Git worktree changes made by command-backed coding tools."""
# 文件职责：负责代码任务状态、补丁、工作树及验证中的 worktree 子模块。
# 逻辑关系：上层通过 coding/worktree.py 使用本模块；本模块把处理结果交给同领域服务、持久化层或 API 响应层。

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import subprocess

from src.tools.types import ToolResult


# 类职责：集中描述 WorktreeState 的流程状态。
@dataclass(frozen=True, slots=True)
class WorktreeState:
    """Exact tracked diff plus the paths Git currently considers changed."""

    # 变量说明：patch 表示当前步骤使用的 patch 值。
    patch: str
    # 变量说明：tracked_files 表示当前流程使用的 tracked_files 集合。
    tracked_files: tuple[tuple[str, int, int, int], ...]
    # 变量说明：untracked_files 表示当前流程使用的 untracked_files 集合。
    untracked_files: tuple[tuple[str, int, int, int, bytes | None], ...]

    # 函数职责：完成 paths 对应的业务处理。
    # 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
    @property
    def paths(self) -> tuple[str, ...]:
        return tuple(sorted(
            {entry[0] for entry in self.tracked_files}
            | {entry[0] for entry in self.untracked_files}
        ))


# 函数职责：完成 capture_worktree_state 对应的业务处理。
# 参数关系：root 表示处理范围的根目录。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def capture_worktree_state(root: Path) -> WorktreeState | None:
    """Return a bounded-lifetime Git observation, or ``None`` outside a repository.

    The patch text is intentionally compared in memory rather than persisted as
    a baseline or reduced to a new hash. This catches a shell command replacing
    one version of an already-modified tracked file with another version.
    """

    # 变量说明：repository 表示当前步骤使用的 repository 值。
    repository = _run_git(root, ["rev-parse", "--show-toplevel"])
    if repository is None:
        return None
    try:
        # 变量说明：top_level 表示当前步骤使用的 top_level 值。
        top_level = Path(repository.strip()).resolve()
    except OSError:
        return None
    if top_level != root.resolve():
        return None

    # 变量说明：patch 表示当前步骤使用的 patch 值。
    patch = _run_git(root, ["diff", "--binary", "--no-ext-diff", "HEAD", "--"])
    # 变量说明：tracked 表示当前步骤使用的 tracked 值。
    tracked = _run_git(
        root,
        ["-c", "core.quotePath=false", "diff", "--name-only", "-z", "--no-ext-diff", "HEAD", "--"],
    )
    # 变量说明：untracked 表示当前步骤使用的 untracked 值。
    untracked = _run_git(
        root,
        ["-c", "core.quotePath=false", "ls-files", "-z", "--others", "--exclude-standard"],
    )
    if patch is None or tracked is None or untracked is None:
        return None
    # 变量说明：tracked_paths 表示当前流程使用的 tracked_paths 集合。
    tracked_paths = {
        path.replace("\\", "/")
        for path in tracked.split("\0")
        if path
    }
    # 变量说明：untracked_paths 表示当前流程使用的 untracked_paths 集合。
    untracked_paths = {
        path.replace("\\", "/")
        for path in untracked.split("\0")
        if path
    }
    # 变量说明：tracked_files 表示当前流程使用的 tracked_files 集合。
    tracked_files = _file_states(root, tracked_paths)
    # 变量说明：untracked_files 表示当前流程使用的 untracked_files 集合。
    untracked_files = _untracked_file_states(root, untracked_paths)
    return WorktreeState(
        patch=patch,
        tracked_files=tracked_files,
        untracked_files=untracked_files,
    )


# 函数职责：完成 file_states 对应的业务处理。
# 参数关系：root 表示处理范围的根目录；paths 表示当前流程使用的 paths 集合。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def _file_states(
    root: Path,
    paths: set[str],
) -> tuple[tuple[str, int, int, int], ...]:
    # 变量说明：states 表示当前流程使用的 states 集合。
    states: list[tuple[str, int, int, int]] = []
    for path in sorted(paths):
        try:
            # 变量说明：stat 表示当前步骤使用的 stat 值。
            stat = (root / Path(path)).lstat()
        except OSError:
            states.append((path, -1, -1, -1))
            continue
        states.append((path, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns))
    return tuple(states)


# 函数职责：完成 untracked_file_states 对应的业务处理。
# 参数关系：root 表示处理范围的根目录；paths 表示当前流程使用的 paths 集合。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def _untracked_file_states(
    root: Path,
    paths: set[str],
) -> tuple[tuple[str, int, int, int, bytes | None], ...]:
    # 变量说明：states 表示当前流程使用的 states 集合。
    states: list[tuple[str, int, int, int, bytes | None]] = []
    for path in sorted(paths):
        # 变量说明：target 表示当前步骤使用的 target 值。
        target = root / Path(path)
        try:
            # 变量说明：stat 表示当前步骤使用的 stat 值。
            stat = target.lstat()
            # 变量说明：content 表示待处理或返回的正文内容。
            content = target.read_bytes()
        except OSError:
            states.append((path, -1, -1, -1, None))
            continue
        states.append((
            path,
            stat.st_size,
            stat.st_mtime_ns,
            stat.st_ctime_ns,
            content,
        ))
    return tuple(states)


# 函数职责：完成 annotate_command_changes 对应的业务处理。
# 参数关系：result 表示本步骤产生的结果；root 表示处理范围的根目录；before 表示当前步骤使用的 before 值；source 表示当前步骤使用的 source 值。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def annotate_command_changes(
    result: ToolResult,
    root: Path,
    before: WorktreeState | None,
    *,
    source: str,
) -> ToolResult:
    """Attach normal coding change evidence when a command changed the worktree."""

    # 变量说明：after 表示当前步骤使用的 after 值。
    after = capture_worktree_state(root)
    if before is None or after is None or before == after:
        return result

    # 变量说明：before_tracked 表示当前步骤使用的 before_tracked 值。
    before_tracked = {entry[0]: entry[1:] for entry in before.tracked_files}
    # 变量说明：after_tracked 表示当前步骤使用的 after_tracked 值。
    after_tracked = {entry[0]: entry[1:] for entry in after.tracked_files}
    # 变量说明：before_untracked 表示当前步骤使用的 before_untracked 值。
    before_untracked = {entry[0]: entry[1:] for entry in before.untracked_files}
    # 变量说明：after_untracked 表示当前步骤使用的 after_untracked 值。
    after_untracked = {entry[0]: entry[1:] for entry in after.untracked_files}
    # 变量说明：changed_paths 表示当前流程使用的 changed_paths 集合。
    changed_paths = {
        path
        for path in set(before_tracked) | set(after_tracked)
        if before_tracked.get(path) != after_tracked.get(path)
    }
    changed_paths.update(
        path
        for path in set(before_untracked) | set(after_untracked)
        if before_untracked.get(path) != after_untracked.get(path)
    )
    if before.patch != after.patch and not changed_paths:
        # Exact patch comparison is the final fallback if filesystem timestamps
        # are unavailable or were deliberately restored by the command.
        changed_paths.update(set(before_tracked) | set(after_tracked))
    # 变量说明：changed_paths 表示当前流程使用的 changed_paths 集合。
    changed_paths = sorted(changed_paths)
    # 变量说明：files 表示当前流程使用的 files 集合。
    files = [
        {"path": path, "operation": _operation(root, path)}
        for path in changed_paths
    ]
    # 变量说明：changed 表示当前步骤使用的 changed 值。
    result.changed = True
    # 变量说明：metadata 表示当前步骤使用的 metadata 值。
    result.metadata = {
        **dict(result.metadata),
        "change_set": {
            "status": "observed",
            "source": source,
            "file_count": len(files),
            "files": files,
        },
    }
    return result


# 函数职责：完成 operation 对应的业务处理。
# 参数关系：root 表示处理范围的根目录；relative_path 表示relative_path 对应的文件系统位置。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def _operation(root: Path, relative_path: str) -> str:
    # 变量说明：target 表示当前步骤使用的 target 值。
    target = root / Path(relative_path)
    if not target.exists():
        return "delete"
    # 变量说明：tracked 表示当前步骤使用的 tracked 值。
    tracked = subprocess.run(
        ["git", "ls-files", "--error-unmatch", "--", relative_path],
        cwd=root,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    return "update" if tracked.returncode == 0 else "add"


# 函数职责：执行 git 对应的数据或流程。
# 参数关系：root 表示处理范围的根目录；arguments 表示当前流程使用的 arguments 集合。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def _run_git(root: Path, arguments: list[str]) -> str | None:
    try:
        # 变量说明：completed 表示当前步骤使用的 completed 值。
        completed = subprocess.run(
            ["git", *arguments],
            cwd=root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return completed.stdout if completed.returncode == 0 else None
