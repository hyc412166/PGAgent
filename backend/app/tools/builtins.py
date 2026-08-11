"""Local-first tools exposed to PGAgent's model loop."""

from __future__ import annotations

import fnmatch
import os
import re
import shlex
import signal
import subprocess
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

from .sandbox import SandboxViolation, WorkspaceSandbox
from .types import ApprovalRequest, ToolResult

DEFAULT_COMMAND_ALLOWLIST = frozenset(
    {
        "python",
        "python.exe",
        "pytest",
        "pytest.exe",
        "node",
        "node.exe",
        "npm",
        "npm.cmd",
        "npx",
        "npx.cmd",
        "git",
        "git.exe",
        "rg",
        "rg.exe",
    }
)

_DANGEROUS_SHELL_TOKENS = ("&&", "||", ";", "|", ">", "<", "`", "$(")


def _approval(tool_name: str, arguments: dict[str, Any], reason: str) -> ToolResult:
    request = ApprovalRequest(tool_name=tool_name, arguments=arguments, reason=reason)
    return ToolResult(
        tool_name=tool_name,
        ok=False,
        content=reason,
        approval_required=True,
        approval_request=request,
        error_code="approval_required",
    )


def _safe_walk(
    sandbox: WorkspaceSandbox,
    root: Path,
    *,
    recursive: bool,
    max_entries: int,
    stats: dict[str, int] | None = None,
) -> Iterator[tuple[Path, Path]]:
    """Walk without ever entering an unresolved symlink or Windows junction."""

    pending_directories = [root]
    yielded = 0
    while pending_directories and yielded < max_entries:
        current = pending_directories.pop()
        try:
            children = sorted(current.iterdir(), key=lambda item: item.as_posix().lower())
        except OSError:
            if stats is not None:
                stats["skipped"] = stats.get("skipped", 0) + 1
            continue
        directories_to_visit: list[Path] = []
        for child in children:
            try:
                lexical_relative = child.absolute().relative_to(sandbox.root)
                safe_child = sandbox.resolve(lexical_relative, must_exist=True)
            except (SandboxViolation, FileNotFoundError, OSError, ValueError):
                if stats is not None:
                    stats["skipped"] = stats.get("skipped", 0) + 1
                continue
            yielded += 1
            if stats is not None:
                stats["visited"] = yielded
            yield child, safe_child
            if yielded >= max_entries:
                if stats is not None:
                    # Conservatively report truncation even when the boundary is
                    # exactly equal to tree size; never claim a complete search
                    # after stopping because of the scan cap.
                    stats["scan_limit_reached"] = 1
                break
            if recursive and safe_child.is_dir():
                directories_to_visit.append(safe_child)
        pending_directories.extend(reversed(directories_to_visit))
        if not recursive:
            break


def list_files(
    sandbox: WorkspaceSandbox,
    path: str = ".",
    *,
    recursive: bool = False,
    limit: int = 200,
) -> ToolResult:
    try:
        directory = sandbox.resolve(path, must_exist=True)
        if not directory.is_dir():
            return ToolResult("list_files", False, "目标不是目录", error_code="not_directory")
        entry_limit = max(1, limit)
        entries = [
            lexical
            for lexical, _safe in _safe_walk(
                sandbox,
                directory,
                recursive=recursive,
                max_entries=entry_limit,
            )
        ]
        lines = [
            f"{'dir' if item.is_dir() else 'file'}\t{sandbox.relative(item)}"
            for item in entries
        ]
        return ToolResult(
            "list_files",
            True,
            "\n".join(lines) if lines else "目录为空",
            metadata={"count": len(entries), "truncated": len(entries) >= entry_limit},
        )
    except (SandboxViolation, FileNotFoundError, OSError) as exc:
        return ToolResult("list_files", False, str(exc), error_code="path_error")


def read_file(
    sandbox: WorkspaceSandbox,
    path: str,
    *,
    max_chars: int = 100_000,
) -> ToolResult:
    try:
        target = sandbox.resolve(path, must_exist=True)
        if not target.is_file():
            return ToolResult("read_file", False, "目标不是文件", error_code="not_file")
        size_bytes = target.stat().st_size
        with target.open("rb") as binary_stream:
            prefix = binary_stream.read(8192)
        if b"\x00" in prefix:
            return ToolResult("read_file", False, "暂不支持读取二进制文件", error_code="binary_file")
        max_chars = min(max(int(max_chars), 1), 1_000_000)
        with target.open("r", encoding="utf-8", errors="replace") as text_stream:
            text = text_stream.read(max_chars + 1)
        # Text mode may normalize CRLF, so byte/encoded-length comparison would
        # falsely report truncation. Reading one extra character is authoritative.
        truncated = len(text) > max_chars
        return ToolResult(
            "read_file",
            True,
            text[:max_chars],
            metadata={"truncated": truncated, "size_bytes": size_bytes},
        )
    except (SandboxViolation, FileNotFoundError, OSError) as exc:
        return ToolResult("read_file", False, str(exc), error_code="path_error")


def search_files(
    sandbox: WorkspaceSandbox,
    query: str,
    *,
    path: str = ".",
    pattern: str = "*",
    case_sensitive: bool = False,
    limit: int = 100,
    max_entries: int = 10_000,
) -> ToolResult:
    try:
        root = sandbox.resolve(path, must_exist=True)
        if not root.is_dir():
            return ToolResult("search_files", False, "目标不是目录", error_code="not_directory")
        flags = 0 if case_sensitive else re.IGNORECASE
        matcher = re.compile(re.escape(query), flags)
        matches: list[str] = []
        skipped = 0
        stats = {"skipped": 0, "visited": 0, "scan_limit_reached": 0}
        for file_path, safe_path in _safe_walk(
            sandbox,
            root,
            recursive=True,
            max_entries=min(max(int(max_entries), 1), 100_000),
            stats=stats,
        ):
            if not safe_path.is_file() or not fnmatch.fnmatch(file_path.name, pattern):
                continue
            try:
                if safe_path.stat().st_size > 2_000_000:
                    skipped += 1
                    continue
                text = safe_path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                skipped += 1
                continue
            for line_number, line in enumerate(text.splitlines(), start=1):
                if matcher.search(line):
                    matches.append(f"{sandbox.relative(file_path)}:{line_number}: {line[:500]}")
                    if len(matches) >= limit:
                        break
            if len(matches) >= limit:
                break
        return ToolResult(
            "search_files",
            True,
            "\n".join(matches) if matches else "未找到匹配内容",
            metadata={
                "count": len(matches),
                "truncated": len(matches) >= limit or bool(stats["scan_limit_reached"]),
                "skipped": skipped + stats["skipped"],
                "visited": stats["visited"],
            },
        )
    except (SandboxViolation, FileNotFoundError, OSError, re.error) as exc:
        return ToolResult("search_files", False, str(exc), error_code="search_error")


def get_current_time(*, timezone_name: str | None = None) -> ToolResult:
    # v0.1 uses the host's configured local timezone. The optional name is kept in
    # metadata so a future provider can add IANA timezone conversion without an API break.
    now = datetime.now().astimezone()
    return ToolResult(
        "get_current_time",
        True,
        now.isoformat(),
        metadata={"timezone": timezone_name or str(now.tzinfo)},
    )


def write_file(
    sandbox: WorkspaceSandbox,
    path: str,
    content: str,
    *,
    approved: bool = False,
    overwrite: bool = True,
) -> ToolResult:
    arguments = {"path": path, "content": content, "overwrite": overwrite}
    if not approved:
        return _approval("write_file", arguments, f"写入文件需要批准: {path}")
    try:
        target = sandbox.resolve(path)
        if target.exists() and not overwrite:
            return ToolResult("write_file", False, "文件已存在且禁止覆盖", error_code="already_exists")
        target.parent.mkdir(parents=True, exist_ok=True)
        previous = target.read_text(encoding="utf-8", errors="replace") if target.exists() else None
        target.write_text(content, encoding="utf-8")
        changed = previous != content
        return ToolResult(
            "write_file",
            True,
            f"已写入 {sandbox.relative(target)} ({len(content.encode('utf-8'))} bytes)",
            changed=changed,
            metadata={"path": sandbox.relative(target)},
        )
    except (SandboxViolation, OSError) as exc:
        return ToolResult("write_file", False, str(exc), error_code="write_error")


def _split_command(command: str | list[str]) -> list[str]:
    if isinstance(command, list):
        parts = [str(part) for part in command]
        joined = " ".join(parts)
    else:
        joined = command.strip()
        if any(token in joined for token in _DANGEROUS_SHELL_TOKENS):
            raise ValueError("命令包含被禁止的 shell 链接或重定向符号")
        parts = shlex.split(joined, posix=os.name != "nt")
    if not parts:
        raise ValueError("命令不能为空")
    if any(any(token in part for token in _DANGEROUS_SHELL_TOKENS) for part in parts):
        raise ValueError("命令包含被禁止的 shell 链接或重定向符号")
    return [part.strip('"') for part in parts]


def run_command(
    sandbox: WorkspaceSandbox,
    command: str | list[str],
    *,
    approved: bool = False,
    timeout_seconds: float = 30,
    output_limit: int = 20_000,
    allowlist: frozenset[str] = DEFAULT_COMMAND_ALLOWLIST,
) -> ToolResult:
    arguments = {"command": command, "timeout_seconds": timeout_seconds}
    if not approved:
        return _approval(
            "run_command",
            arguments,
            "命令将以当前用户权限在本机运行，可能访问工作区外资源；执行前必须批准",
        )
    try:
        parts = _split_command(command)
        executable = Path(parts[0]).name.lower()
        normalized_allowlist = {item.lower() for item in allowlist}
        if executable not in normalized_allowlist:
            return ToolResult(
                "run_command",
                False,
                f"命令不在允许列表中: {executable}",
                error_code="command_not_allowed",
            )
        timeout = min(max(float(timeout_seconds), 0.1), 120.0)
        output_limit = min(max(int(output_limit), 256), 1_000_000)
        popen_kwargs: dict[str, Any] = {}
        if os.name == "nt":
            popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            popen_kwargs["start_new_session"] = True
        process = subprocess.Popen(
            parts,
            cwd=sandbox.root,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            shell=False,
            **popen_kwargs,
        )
        chunks: list[str] = []
        captured_chars = 0
        output_truncated = False
        capture_lock = threading.Lock()

        def drain(stream: Any) -> None:
            nonlocal captured_chars, output_truncated
            try:
                while True:
                    chunk = stream.read(4096)
                    if not chunk:
                        return
                    with capture_lock:
                        remaining = output_limit - captured_chars
                        if remaining > 0:
                            kept = chunk[:remaining]
                            chunks.append(kept)
                            captured_chars += len(kept)
                        if len(chunk) > max(0, remaining):
                            output_truncated = True
            finally:
                stream.close()

        readers = [
            threading.Thread(target=drain, args=(process.stdout,), daemon=True),
            threading.Thread(target=drain, args=(process.stderr,), daemon=True),
        ]
        for reader in readers:
            reader.start()
        try:
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            tree_terminated = False
            if os.name == "nt":
                try:
                    killed = subprocess.run(
                        ["taskkill.exe", "/PID", str(process.pid), "/T", "/F"],
                        capture_output=True,
                        check=False,
                        timeout=10,
                    )
                    tree_terminated = killed.returncode == 0
                except (OSError, subprocess.TimeoutExpired):
                    tree_terminated = False
            else:
                try:
                    os.killpg(os.getpgid(process.pid), signal.SIGKILL)
                    tree_terminated = True
                except ProcessLookupError:
                    tree_terminated = process.poll() is not None
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                # Last-resort root-process cleanup. This cannot prove that every
                # descendant died, so report the weaker guarantee truthfully.
                process.kill()
                process.wait(timeout=5)
                tree_terminated = False
            tree_terminated = tree_terminated and process.poll() is not None
            for reader in readers:
                reader.join(timeout=2)
            partial = "".join(chunks)
            termination_text = "进程树已终止" if tree_terminated else "主进程已终止，但无法确认全部子进程"
            return ToolResult(
                "run_command",
                False,
                (partial + f"\n命令执行超时（{timeout_seconds}s），{termination_text}")[:output_limit],
                error_code="timeout",
                metadata={
                    "timeout_seconds": timeout_seconds,
                    "process_tree_terminated": tree_terminated,
                    "truncated": output_truncated,
                    "security_scope": "current_user_host_permissions",
                },
            )
        for reader in readers:
            reader.join(timeout=2)
        combined = "".join(chunks)
        content = combined
        return ToolResult(
            "run_command",
            process.returncode == 0,
            content or f"命令结束，退出码 {process.returncode}",
            changed=False,
            error_code=None if process.returncode == 0 else "nonzero_exit",
            metadata={
                "exit_code": process.returncode,
                "truncated": output_truncated,
                "security_scope": "current_user_host_permissions",
            },
        )
    except (ValueError, OSError) as exc:
        return ToolResult("run_command", False, str(exc), error_code="command_error")
