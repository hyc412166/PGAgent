"""Git metadata and managed checkouts, ported from openai/codex c0d26949.

对应 git-utils/info.rs、worktree/{lib,git,metadata}.rs。目录状态来自 Git，
会话归属单独保存；子 Agent 不创建自己的分支或 worktree。
"""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from src.config import settings


class GitOperationError(RuntimeError):
    """A real Git or filesystem operation failed."""


@dataclass
class GitInfo:
    commit_hash: str | None
    branch: str | None
    repository_url: str | None


@dataclass
class ManagedWorktree:
    root: str
    cwd: str
    source_root: str
    source_cwd: str
    head_sha: str
    branch: str | None
    owner_thread_id: str | None


def _git_env() -> dict[str, str]:
    env = os.environ.copy()
    for name in list(env):
        if name.upper() in {
            "GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR",
            "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_CONFIG",
            "GIT_CONFIG_PARAMETERS", "GIT_CONFIG_COUNT", "GIT_CEILING_DIRECTORIES",
            "GIT_IMPLICIT_WORK_TREE", "GIT_GRAFT_FILE", "GIT_NO_REPLACE_OBJECTS",
            "GIT_REPLACE_REF_BASE", "GIT_PREFIX", "GIT_SHALLOW_FILE",
        } or name.upper().startswith(("GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_")):
            env.pop(name)
    env.update(GIT_TERMINAL_PROMPT="0", GIT_LFS_SKIP_SMUDGE="1", LC_ALL="C")
    return env


def _output(cwd: Path, args: list[str], *, working_tree: bool = False,
            timeout: int = 5) -> subprocess.CompletedProcess[str]:
    command = ["git", "-c", "safe.bareRepository=explicit", "-c",
               "core.hooksPath=" + ("NUL" if os.name == "nt" else "/dev/null"),
               "-c", "core.fsmonitor=", "-c", "attr.tree=", "-c", "core.attributesFile="]
    if working_tree:
        filters = _output(cwd, ["config", "--null", "--name-only", "--get-regexp", "^filter\\."])
        if filters.returncode not in (0, 1):
            raise GitOperationError(filters.stderr.strip())
        names = {name[7:].rsplit(".", 1)[0] for name in filters.stdout.split("\0")
                 if name.startswith("filter.") and "." in name[7:]}
        for name in names:
            for attribute, value in (("process", ""), ("clean", ""), ("smudge", ""), ("required", "false")):
                command.extend(["-c", f"filter.{name}.{attribute}={value}"])
    try:
        return subprocess.run(command + args, cwd=cwd, env=_git_env(), capture_output=True,
                              text=True, encoding="utf-8", errors="replace", timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise GitOperationError(f"Git {args[0]} failed in {cwd}: {exc}") from exc


def _run_git(cwd: Path, args: list[str], **kwargs) -> str:
    result = _output(cwd, args, **kwargs)
    if result.returncode:
        raise GitOperationError(result.stderr.strip() or result.stdout.strip() or "Git command failed")
    # 不剥离 NUL 或路径空格，porcelain -z 必须完整解析。
    return result.stdout


def repository_root(cwd: str | Path) -> Path | None:
    path = Path(cwd).expanduser().resolve()
    if not path.is_dir():
        raise GitOperationError(f"工作目录不存在：{path}")
    output = _output(path, ["rev-parse", "--show-toplevel"])
    if output.returncode:
        if "not a git repository" in output.stderr.lower():
            return None
        raise GitOperationError(output.stderr.strip() or "Cannot inspect Git repository")
    return Path(output.stdout.rstrip("\r\n")).resolve()


def current_branch_name(cwd: str | Path) -> str | None:
    return _run_git(Path(cwd), ["branch", "--show-current"]).strip() or None


def _sanitize_remote(value: str) -> str | None:
    value = value.strip()
    if not value:
        return None
    if "://" in value:
        parsed = urlsplit(value)
        return urlunsplit((parsed.scheme, parsed.netloc.rsplit("@", 1)[-1], parsed.path, "", ""))
    return value


def collect_git_info(cwd: str | Path) -> GitInfo | None:
    if not str(cwd):
        raise GitOperationError("会话没有有效工作目录")
    root = repository_root(cwd)
    if root is None:
        return None
    def optional(args: list[str]) -> str | None:
        output = _output(root, args)
        if output.returncode == 0:
            return output.stdout.strip() or None
        # 未提交仓库没有 HEAD、未配置 origin 是合法的缺省状态。
        if args == ["rev-parse", "--verify", "HEAD"] and "Needed a single revision" in output.stderr:
            return None
        if args == ["remote", "get-url", "origin"] and "No such remote" in output.stderr:
            return None
        raise GitOperationError(output.stderr.strip() or "Cannot read Git metadata")
    with ThreadPoolExecutor(max_workers=3) as pool:
        head, branch, remote = list(pool.map(optional, [
            ["rev-parse", "--verify", "HEAD"], ["branch", "--show-current"], ["remote", "get-url", "origin"],
        ]))
    return GitInfo(head, branch, _sanitize_remote(remote) if remote else None)


def git_state(cwd: str | Path) -> dict:
    try:
        info = collect_git_info(cwd)
        return {"cwd": str(cwd), "git_info": asdict(info) if info else None, "git_error": None}
    except (GitOperationError, OSError, ValueError) as exc:
        return {"cwd": str(cwd), "git_info": None, "git_error": str(exc)}


def default_worktree_base(cwd: str | Path) -> str:
    output = _run_git(Path(cwd), ["for-each-ref", "--format=%(refname) %(symref)", "refs/remotes", "refs/heads"])
    refs = [line.split(" ", 1) for line in output.splitlines() if " " in line]
    heads = sorted(((name, target) for name, target in refs if name.startswith("refs/remotes/")
                    and name.endswith("/HEAD") and target),
                   key=lambda item: (not item[0].startswith("refs/remotes/origin/"), item[0]))
    if heads:
        return heads[0][1]
    available = {name for name, _ in refs}
    for name in ("refs/remotes/origin/main", "refs/remotes/origin/master", "refs/heads/main", "refs/heads/master"):
        if name in available:
            return name
    raise GitOperationError("无法确定仓库默认分支")


def _git_path(cwd: Path, *args: str) -> Path:
    value = Path(_run_git(cwd, ["rev-parse", "--path-format=absolute", *args]).rstrip("\r\n"))
    return (cwd / value).resolve()


def _owner_path(root: Path) -> Path:
    return _git_path(root, "--git-path", "codex-thread.json")


def read_worktree_owner(root: str | Path) -> str | None:
    path = _owner_path(Path(root))
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        raise GitOperationError(f"Cannot read worktree owner: {path}: {exc}") from exc
    if not isinstance(data, dict) or data.get("version") != 1 or not isinstance(data.get("ownerThreadId"), str) or not data["ownerThreadId"]:
        raise GitOperationError(f"Invalid worktree owner: {path}")
    return data["ownerThreadId"]


def bind_worktree_owner(root: str | Path, thread_id: str) -> None:
    if not thread_id:
        raise GitOperationError("worktree owner thread ID cannot be empty")
    root = Path(root)
    current = read_worktree_owner(root)
    if current == thread_id:
        return
    if current:
        raise GitOperationError(f"worktree already belongs to thread {current}")
    path = _owner_path(root)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as handle:
        temporary = Path(handle.name)
        json.dump({"version": 1, "ownerThreadId": thread_id}, handle)
    try:
        # 同 Codex persist_noclobber：并发绑定不能覆盖已有 owner。
        try:
            os.link(temporary, path)
        except FileExistsError:
            if read_worktree_owner(root) != thread_id:
                raise GitOperationError("worktree was concurrently assigned to another thread")
    finally:
        temporary.unlink()


def _parse_worktree_list(output: str) -> list[tuple[Path, str, str | None]]:
    result = []
    for record in output.split("\0\0"):
        fields = dict(field.split(" ", 1) for field in record.split("\0") if " " in field)
        if fields.get("worktree") and fields.get("HEAD"):
            branch = fields.get("branch")
            result.append((Path(fields["worktree"]), fields["HEAD"],
                           branch.removeprefix("refs/heads/") if branch else None))
    return result


def list_managed_worktrees(source_cwd: str | Path) -> list[ManagedWorktree]:
    source = Path(source_cwd).expanduser().resolve(strict=True)
    source_root = repository_root(source)
    if source_root is None:
        raise GitOperationError("managed worktree requires a Git repository")
    common = _git_path(source, "--git-common-dir")
    managed = (settings.data_dir / "worktrees").resolve()
    relative_cwd = source.relative_to(source_root)
    result = []
    for root, head, branch in _parse_worktree_list(_run_git(source, ["worktree", "list", "--porcelain", "-z"])):
        if root.is_symlink() or root.parent.is_symlink() or not root.is_dir():
            continue
        root = root.resolve()
        if root.parent.parent != managed or not (root / ".git").is_file():
            continue
        if _git_path(root, "--git-common-dir") != common:
            continue
        git_dir = _git_path(root, "--git-dir")
        backlink = Path((git_dir / "gitdir").read_text(encoding="utf-8").rstrip("\r\n"))
        if (git_dir / backlink).resolve() != (root / ".git").resolve():
            continue
        cwd = (root / relative_cwd).resolve()
        if not cwd.is_dir() or not cwd.is_relative_to(root):
            continue
        result.append(ManagedWorktree(str(root), str(cwd), str(source_root), str(source), head, branch,
                                      read_worktree_owner(root)))
    return sorted(result, key=lambda item: item.root)


def require_managed_worktree(source_cwd: str | Path, root: str | Path) -> ManagedWorktree:
    target = Path(root).resolve()
    for checkout in list_managed_worktrees(source_cwd):
        if Path(checkout.root) == target:
            return checkout
    raise GitOperationError(f"{target} is not a managed worktree in this repository")


def create_managed_worktree(source_cwd: str | Path, base: str | None = None) -> ManagedWorktree:
    source = Path(source_cwd).expanduser().resolve(strict=True)
    root = repository_root(source)
    if root is None:
        raise GitOperationError("managed worktree requires a Git repository")
    head = _run_git(root, ["rev-parse", "--verify", "--end-of-options", f"{base or 'HEAD'}^{{commit}}"]).strip()
    pool = (settings.data_dir / "worktrees").resolve()
    pool.mkdir(parents=True, exist_ok=True)
    bucket = Path(tempfile.mkdtemp(dir=pool))
    target = bucket / root.name
    created = False
    try:
        _run_git(root, ["worktree", "add", "--detach", "--no-checkout", str(target), head], working_tree=True, timeout=60)
        created = True
        config = _git_path(target, "--git-path", "config.worktree")
        _run_git(target, ["config", "--file", str(config), "core.worktree", str(target)])
        # hard reset 仅用于刚分配的新 checkout，绝不用于用户现有目录。
        _run_git(target, ["--work-tree=.", "reset", "--hard", "--no-recurse-submodules", head], working_tree=True, timeout=60)
        cwd = (target / source.relative_to(root)).resolve()
        if not cwd.is_dir() or not cwd.is_relative_to(target):
            raise GitOperationError("requested base does not contain a safe working directory")
        return ManagedWorktree(str(target), str(cwd), str(root), str(source), head, None, None)
    except Exception as exc:
        if created:
            try:
                _run_git(root, ["worktree", "remove", "--force", str(target)], working_tree=True, timeout=60)
            except GitOperationError as cleanup:
                raise GitOperationError(f"{exc}; rollback failed: {cleanup}") from exc
        if not any(bucket.iterdir()):
            bucket.rmdir()
        raise


def remove_managed_worktree(source_cwd: str | Path, root: str | Path) -> None:
    checkout = require_managed_worktree(source_cwd, root)
    target = Path(checkout.root)
    if Path(source_cwd).resolve().is_relative_to(target):
        raise GitOperationError("switch to another checkout before deleting the current worktree")
    if _run_git(target, ["ls-files", "--others", "--ignored", "--exclude-standard", "-z"], working_tree=True):
        raise GitOperationError("worktree contains ignored local files; remove them before deleting it")
    _run_git(Path(checkout.source_root), ["worktree", "remove", str(target)], working_tree=True, timeout=60)
    if not any(target.parent.iterdir()):
        target.parent.rmdir()


def release_worktree_owner(root: str | Path, thread_id: str) -> None:
    """用于绑定补偿或会话删除，不能解除别的会话归属。"""
    root = Path(root)
    if read_worktree_owner(root) != thread_id:
        raise GitOperationError("Cannot roll back a different worktree owner")
    _owner_path(root).unlink()


def resolve_session_cwd(workspace_root: str, requested_cwd: str | None) -> tuple[str, ManagedWorktree | None]:
    """会话可在项目目录或该项目注册的 managed checkout 运行。"""
    root = Path(workspace_root).expanduser().resolve()
    if requested_cwd is None:
        return str(root), None
    cwd = Path(requested_cwd).expanduser().resolve(strict=True)
    if not cwd.is_dir():
        raise GitOperationError("Session cwd must be a directory")
    # 项目内的目录允许作为 cwd；托管池目录仍必须验证注册与 owner。
    managed = (settings.data_dir / "worktrees").resolve()
    if cwd.is_relative_to(root) and not cwd.is_relative_to(managed):
        return str(cwd), None
    for checkout in list_managed_worktrees(root):
        if Path(checkout.cwd) == cwd:
            return str(cwd), checkout
    raise GitOperationError("Session cwd must be the workspace or its registered managed worktree")
