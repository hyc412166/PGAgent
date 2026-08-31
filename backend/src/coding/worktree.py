"""Observe real Git worktree changes made by command-backed coding tools."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import subprocess

from src.tools.types import ToolResult


@dataclass(frozen=True, slots=True)
class WorktreeState:
    """Exact tracked diff plus the paths Git currently considers changed."""

    patch: str
    tracked_files: tuple[tuple[str, int, int, int], ...]
    untracked_files: tuple[tuple[str, int, int, int, bytes | None], ...]

    @property
    def paths(self) -> tuple[str, ...]:
        return tuple(sorted(
            {entry[0] for entry in self.tracked_files}
            | {entry[0] for entry in self.untracked_files}
        ))


def capture_worktree_state(root: Path) -> WorktreeState | None:
    """Return a bounded-lifetime Git observation, or ``None`` outside a repository.

    The patch text is intentionally compared in memory rather than persisted as
    a baseline or reduced to a new hash. This catches a shell command replacing
    one version of an already-modified tracked file with another version.
    """

    repository = _run_git(root, ["rev-parse", "--show-toplevel"])
    if repository is None:
        return None
    try:
        top_level = Path(repository.strip()).resolve()
    except OSError:
        return None
    if top_level != root.resolve():
        return None

    patch = _run_git(root, ["diff", "--binary", "--no-ext-diff", "HEAD", "--"])
    tracked = _run_git(
        root,
        ["-c", "core.quotePath=false", "diff", "--name-only", "-z", "--no-ext-diff", "HEAD", "--"],
    )
    untracked = _run_git(
        root,
        ["-c", "core.quotePath=false", "ls-files", "-z", "--others", "--exclude-standard"],
    )
    if patch is None or tracked is None or untracked is None:
        return None
    tracked_paths = {
        path.replace("\\", "/")
        for path in tracked.split("\0")
        if path
    }
    untracked_paths = {
        path.replace("\\", "/")
        for path in untracked.split("\0")
        if path
    }
    tracked_files = _file_states(root, tracked_paths)
    untracked_files = _untracked_file_states(root, untracked_paths)
    return WorktreeState(
        patch=patch,
        tracked_files=tracked_files,
        untracked_files=untracked_files,
    )


def _file_states(
    root: Path,
    paths: set[str],
) -> tuple[tuple[str, int, int, int], ...]:
    states: list[tuple[str, int, int, int]] = []
    for path in sorted(paths):
        try:
            stat = (root / Path(path)).lstat()
        except OSError:
            states.append((path, -1, -1, -1))
            continue
        states.append((path, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns))
    return tuple(states)


def _untracked_file_states(
    root: Path,
    paths: set[str],
) -> tuple[tuple[str, int, int, int, bytes | None], ...]:
    states: list[tuple[str, int, int, int, bytes | None]] = []
    for path in sorted(paths):
        target = root / Path(path)
        try:
            stat = target.lstat()
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


def annotate_command_changes(
    result: ToolResult,
    root: Path,
    before: WorktreeState | None,
    *,
    source: str,
) -> ToolResult:
    """Attach normal coding change evidence when a command changed the worktree."""

    after = capture_worktree_state(root)
    if before is None or after is None or before == after:
        return result

    before_tracked = {entry[0]: entry[1:] for entry in before.tracked_files}
    after_tracked = {entry[0]: entry[1:] for entry in after.tracked_files}
    before_untracked = {entry[0]: entry[1:] for entry in before.untracked_files}
    after_untracked = {entry[0]: entry[1:] for entry in after.untracked_files}
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
    changed_paths = sorted(changed_paths)
    files = [
        {"path": path, "operation": _operation(root, path)}
        for path in changed_paths
    ]
    result.changed = True
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


def _operation(root: Path, relative_path: str) -> str:
    target = root / Path(relative_path)
    if not target.exists():
        return "delete"
    tracked = subprocess.run(
        ["git", "ls-files", "--error-unmatch", "--", relative_path],
        cwd=root,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    return "update" if tracked.returncode == 0 else "add"


def _run_git(root: Path, arguments: list[str]) -> str | None:
    try:
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
