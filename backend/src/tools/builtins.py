"""Local-first tools exposed to PGAgent's model loop."""
# 文件职责：负责工具定义、授权、注册、调度与执行中的 builtins 子模块。
# 逻辑关系：上层通过 tools/builtins.py 使用本模块；本模块把处理结果交给同领域服务、持久化层或 API 响应层。

from __future__ import annotations

import fnmatch
import html
import asyncio
import base64
import inspect
import ipaddress
import json
import os
import re
import shlex
import shutil
import signal
import socket
import subprocess
import threading
import time
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Awaitable, Callable, Iterator, Mapping
from urllib.parse import parse_qs, quote_plus, urljoin, urlsplit, urlunsplit

import httpx
from src.config.settings import settings

from .sandbox import SandboxViolation, WorkspaceSandbox
from .types import ApprovalRequest, ToolResult
from .web_pages import PageReader

# 变量说明：DEFAULT_COMMAND_ALLOWLIST 表示当前步骤使用的 DEFAULT_COMMAND_ALLOWLIST 值。
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

# 变量说明：_DANGEROUS_SHELL_TOKENS 表示当前流程使用的 _DANGEROUS_SHELL_TOKENS 集合。
_DANGEROUS_SHELL_TOKENS = ("&&", "||", ";", "|", ">", "<", "`", "$(")
# 变量说明：_WINDOWS_BATCH_CONTROL_TOKENS 表示当前流程使用的 _WINDOWS_BATCH_CONTROL_TOKENS 集合。
_WINDOWS_BATCH_CONTROL_TOKENS = ("&", "|", ">", "<", "^", "\r", "\n")

# A coordinator must be able to fan out, but a malformed model response must
# not be able to create an unbounded number of child runs in one turn.
# 变量说明：MAX_PARALLEL_DELEGATED_TASKS 表示当前流程使用的 MAX_PARALLEL_DELEGATED_TASKS 集合。
MAX_PARALLEL_DELEGATED_TASKS = 8
# 变量说明：MAX_WEB_RESPONSE_BYTES 表示当前流程使用的 MAX_WEB_RESPONSE_BYTES 集合。
MAX_WEB_RESPONSE_BYTES = 1_000_000
# 变量说明：DEFAULT_WEB_PAGE_CHARS 表示当前流程使用的 DEFAULT_WEB_PAGE_CHARS 集合。
DEFAULT_WEB_PAGE_CHARS = 12_000
# 变量说明：MAX_WEB_PAGE_CHARS 表示当前流程使用的 MAX_WEB_PAGE_CHARS 集合。
MAX_WEB_PAGE_CHARS = 20_000
MAX_WEB_RUN_NAVIGATION_COMMANDS = 10
MAX_WEB_RUN_COMMANDS = 20
MAX_WEB_RUN_OUTPUT_CHARS = 40_000
# 变量说明：MAX_WEB_REDIRECTS 表示当前流程使用的 MAX_WEB_REDIRECTS 集合。
MAX_WEB_REDIRECTS = 5
# 变量说明：MAX_TODOS 表示当前流程使用的 MAX_TODOS 集合。
MAX_TODOS = 100


# 类职责：表示 UnsafeWebUrlError 场景的领域异常。
# 继承关系：复用基类提供的契约，并向调用方暴露本类声明的字段和方法。
class UnsafeWebUrlError(ValueError):
    """A URL violates the webfetch SSRF boundary."""


# 类职责：表示 WebHostResolutionError 场景的领域异常。
# 继承关系：复用基类提供的契约，并向调用方暴露本类声明的字段和方法。
class WebHostResolutionError(RuntimeError):
    """The URL is syntactically safe but its public host could not resolve."""


# 类职责：定义 _ReadableHtmlParser 在本领域中的数据与行为。
# 继承关系：复用基类提供的契约，并向调用方暴露本类声明的字段和方法。
class _ReadableHtmlParser(HTMLParser):
    """Extract document metadata and readable text without retaining markup."""

    # 变量说明：_SKIPPED 表示当前步骤使用的 _SKIPPED 值。
    _SKIPPED = frozenset({"script", "style", "noscript", "svg", "canvas", "template"})
    # 变量说明：_BLOCKS 表示当前流程使用的 _BLOCKS 集合。
    _BLOCKS = frozenset({
        "article", "aside", "blockquote", "br", "div", "footer", "h1", "h2", "h3",
        "h4", "h5", "h6", "header", "li", "main", "nav", "ol", "p", "pre", "section",
        "table", "td", "th", "tr", "ul",
    })

    # 函数职责：初始化实例依赖与初始状态。
    # 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        # 变量说明：parts 表示当前流程使用的 parts 集合。
        self.parts: list[str] = []
        # 变量说明：title_parts 表示当前流程使用的 title_parts 集合。
        self.title_parts: list[str] = []
        # 变量说明：metadata 表示当前步骤使用的 metadata 值。
        self.metadata: dict[str, str] = {}
        # 变量说明：_skip_depth 表示当前步骤使用的 _skip_depth 值。
        self._skip_depth = 0
        # 变量说明：_in_title 表示当前步骤使用的 _in_title 值。
        self._in_title = False
        self._pre_depth = 0
        self.links: list[dict[str, Any]] = []
        self._anchor: dict[str, Any] | None = None

    # 函数职责：处理 starttag 对应的数据或流程。
    # 参数关系：tag 表示当前步骤使用的 tag 值；attrs 表示当前流程使用的 attrs 集合。
    # 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        # 变量说明：name 表示当前对象名称。
        name = tag.lower()
        if name in self._SKIPPED:
            self._skip_depth += 1
            return
        if self._skip_depth:
            return
        # 变量说明：attributes 表示当前流程使用的 attributes 集合。
        attributes = {str(key).lower(): str(value or "") for key, value in attrs}
        if name == "pre":
            self._pre_depth += 1
        if name == "a" and attributes.get("href"):
            self._anchor = {"id": len(self.links) + 1, "href": attributes["href"], "text": ""}
            self.links.append(self._anchor)
        if name == "title":
            # 变量说明：_in_title 表示当前步骤使用的 _in_title 值。
            self._in_title = True
        elif name == "meta":
            # 变量说明：key 表示用于查找或映射的键。
            key = (attributes.get("property") or attributes.get("name") or "").lower()
            # 变量说明：value 表示当前字段或计算值。
            value = attributes.get("content", "").strip()
            if key and value:
                self.metadata.setdefault(key, value)
        if name in self._BLOCKS:
            self.parts.append("\n")

    # 函数职责：处理 endtag 对应的数据或流程。
    # 参数关系：tag 表示当前步骤使用的 tag 值。
    # 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
    def handle_endtag(self, tag: str) -> None:
        # 变量说明：name 表示当前对象名称。
        name = tag.lower()
        if name in self._SKIPPED:
            # 变量说明：_skip_depth 表示当前步骤使用的 _skip_depth 值。
            self._skip_depth = max(0, self._skip_depth - 1)
            return
        if self._skip_depth:
            return
        if name == "title":
            # 变量说明：_in_title 表示当前步骤使用的 _in_title 值。
            self._in_title = False
        if name == "pre":
            self._pre_depth = max(0, self._pre_depth - 1)
        if name == "a" and self._anchor is not None:
            self.parts.append(f" [{self._anchor['id']}]")
            self._anchor = None
        if name in self._BLOCKS:
            self.parts.append("\n")

    # 函数职责：处理 data 对应的数据或流程。
    # 参数关系：data 表示当前处理的数据。
    # 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        # 变量说明：value 表示当前字段或计算值。
        value = data if self._pre_depth else " ".join(data.split())
        if not value:
            return
        if self._in_title:
            self.title_parts.append(value)
        if self._anchor is not None:
            self._anchor["text"] += value
        self.parts.append(value if self._pre_depth else value + " ")

    # 函数职责：完成 readable_text 对应的业务处理。
    # 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
    def readable_text(self) -> str:
        # 变量说明：lines 表示当前流程使用的 lines 集合。
        # pre/code 内部的缩进和重复行是源码语义，不能按文章正文做空白归一化。
        return "".join(self.parts).strip("\r\n")


# 函数职责：完成 approval 对应的业务处理。
# 参数关系：tool_name 表示当前步骤使用的 tool_name 值；arguments 表示当前流程使用的 arguments 集合；reason 表示当前步骤使用的 reason 值。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def _approval(tool_name: str, arguments: dict[str, Any], reason: str) -> ToolResult:
    # 变量说明：request 表示调用方传入的请求数据。
    request = ApprovalRequest(tool_name=tool_name, arguments=arguments, reason=reason)
    return ToolResult(
        tool_name=tool_name,
        ok=False,
        content=reason,
        approval_required=True,
        approval_request=request,
        error_code="approval_required",
    )


# 函数职责：完成 safe_walk 对应的业务处理。
# 参数关系：sandbox 表示当前步骤使用的 sandbox 值；root 表示处理范围的根目录；recursive 表示当前步骤使用的 recursive 值；max_entries 表示当前流程使用的 max_entries 集合；stats 表示当前流程使用的 stats 集合。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def _safe_walk(
    sandbox: WorkspaceSandbox,
    root: Path,
    *,
    recursive: bool,
    max_entries: int,
    stats: dict[str, int] | None = None,
) -> Iterator[tuple[Path, Path]]:
    """Walk without ever entering an unresolved symlink or Windows junction."""

    # 变量说明：pending_directories 表示当前流程使用的 pending_directories 集合。
    pending_directories = [root]
    # 变量说明：yielded 表示当前步骤使用的 yielded 值。
    yielded = 0
    while pending_directories and yielded < max_entries:
        # 变量说明：current 表示当前步骤使用的 current 值。
        current = pending_directories.pop()
        try:
            # 变量说明：children 表示当前步骤使用的 children 值。
            children = sorted(current.iterdir(), key=lambda item: item.as_posix().lower())
        except OSError:
            if stats is not None:
                # 变量说明：stats 的索引项 表示该语句创建或更新的目标数据。
                stats["skipped"] = stats.get("skipped", 0) + 1
            continue
        # 变量说明：directories_to_visit 表示当前步骤使用的 directories_to_visit 值。
        directories_to_visit: list[Path] = []
        for child in children:
            try:
                # 变量说明：lexical_relative 表示当前步骤使用的 lexical_relative 值。
                lexical_relative = child.absolute().relative_to(sandbox.root)
                # 变量说明：safe_child 表示当前步骤使用的 safe_child 值。
                safe_child = sandbox.resolve(lexical_relative, must_exist=True)
            except (SandboxViolation, FileNotFoundError, OSError, ValueError):
                if stats is not None:
                    # 变量说明：stats 的索引项 表示该语句创建或更新的目标数据。
                    stats["skipped"] = stats.get("skipped", 0) + 1
                continue
            yielded += 1
            if stats is not None:
                # 变量说明：stats 的索引项 表示该语句创建或更新的目标数据。
                stats["visited"] = yielded
            yield child, safe_child
            if yielded >= max_entries:
                if stats is not None:
                    # Conservatively report truncation even when the boundary is
                    # exactly equal to tree size; never claim a complete search
                    # after stopping because of the scan cap.
                    # 变量说明：stats 的索引项 表示该语句创建或更新的目标数据。
                    stats["scan_limit_reached"] = 1
                break
            if recursive and safe_child.is_dir():
                directories_to_visit.append(safe_child)
        pending_directories.extend(reversed(directories_to_visit))
        if not recursive:
            break


# 函数职责：列出 files 对应的数据或流程。
# 参数关系：sandbox 表示当前步骤使用的 sandbox 值；path 表示当前文件或目录路径；recursive 表示当前步骤使用的 recursive 值；limit 表示当前步骤使用的 limit 值。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def list_files(
    sandbox: WorkspaceSandbox,
    path: str = ".",
    *,
    recursive: bool = False,
    limit: int = 200,
) -> ToolResult:
    try:
        # 变量说明：directory 表示当前步骤使用的 directory 值。
        directory = sandbox.resolve(path, must_exist=True)
        if not directory.is_dir():
            return ToolResult("list_files", False, "目标不是目录", error_code="not_directory")
        # 变量说明：entry_limit 表示当前步骤使用的 entry_limit 值。
        entry_limit = max(1, limit)
        # 变量说明：entries 表示当前流程使用的 entries 集合。
        entries = [
            lexical
            for lexical, _safe in _safe_walk(
                sandbox,
                directory,
                recursive=recursive,
                max_entries=entry_limit,
            )
        ]
        # 变量说明：lines 表示当前流程使用的 lines 集合。
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


# 函数职责：完成 read_file 对应的业务处理。
# 参数关系：sandbox 表示当前步骤使用的 sandbox 值；path 表示当前文件或目录路径；offset 表示当前步骤使用的 offset 值；limit 表示当前步骤使用的 limit 值；max_chars 表示当前流程使用的 max_chars 集合。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def read_file(
    sandbox: WorkspaceSandbox,
    path: str,
    *,
    offset: int = 0,
    limit: int | None = None,
    max_chars: int = 100_000,
) -> ToolResult:
    try:
        # 变量说明：target 表示当前步骤使用的 target 值。
        target = sandbox.resolve(path, must_exist=True)
        if not target.is_file():
            return ToolResult("read_file", False, "目标不是文件", error_code="not_file")
        # 变量说明：size_bytes 表示当前流程使用的 size_bytes 集合。
        size_bytes = target.stat().st_size
        with target.open("rb") as binary_stream:
            # 变量说明：prefix 表示当前步骤使用的 prefix 值。
            prefix = binary_stream.read(8192)
        if b"\x00" in prefix:
            return ToolResult("read_file", False, "暂不支持读取二进制文件", error_code="binary_file")
        # 变量说明：line_offset 表示当前步骤使用的 line_offset 值。
        line_offset = max(0, int(offset))
        # 变量说明：line_limit 表示当前步骤使用的 line_limit 值。
        line_limit = None if limit is None else max(1, int(limit))
        # 变量说明：max_chars 表示当前流程使用的 max_chars 集合。
        max_chars = min(max(int(max_chars), 1), 1_000_000)
        # 变量说明：chunks 表示当前流程使用的 chunks 集合。
        chunks: list[str] = []
        # 变量说明：captured_chars 表示当前流程使用的 captured_chars 集合。
        captured_chars = 0
        # 变量说明：returned_lines 表示当前流程使用的 returned_lines 集合。
        returned_lines = 0
        # 变量说明：truncated 表示当前步骤使用的 truncated 值。
        truncated = False
        with target.open("r", encoding="utf-8", errors="replace") as text_stream:
            for line_index, line in enumerate(text_stream):
                if line_index < line_offset:
                    continue
                if line_limit is not None and returned_lines >= line_limit:
                    # 变量说明：truncated 表示当前步骤使用的 truncated 值。
                    truncated = True
                    break
                # 变量说明：remaining 表示当前步骤使用的 remaining 值。
                remaining = max_chars - captured_chars
                if len(line) > remaining:
                    chunks.append(line[:remaining])
                    captured_chars += remaining
                    if remaining:
                        returned_lines += 1
                    # 变量说明：truncated 表示当前步骤使用的 truncated 值。
                    truncated = True
                    break
                chunks.append(line)
                captured_chars += len(line)
                returned_lines += 1
        # 变量说明：text 表示当前步骤使用的 text 值。
        text = "".join(chunks)
        return ToolResult(
            "read_file",
            True,
            text,
            metadata={
                "truncated": truncated,
                "size_bytes": size_bytes,
                "offset": line_offset,
                "line_limit": line_limit,
                "lines_returned": returned_lines,
            },
        )
    except (SandboxViolation, FileNotFoundError, OSError) as exc:
        return ToolResult("read_file", False, str(exc), error_code="path_error")


# 函数职责：完成 search_files 对应的业务处理。
# 参数关系：sandbox 表示当前步骤使用的 sandbox 值；query 表示当前步骤使用的 query 值；path 表示当前文件或目录路径；pattern 表示当前步骤使用的 pattern 值；case_sensitive 表示当前步骤使用的 case_sensitive 值；limit 表示当前步骤使用的 limit 值；max_entries 表示当前流程使用的 max_entries 集合。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
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
        # 变量说明：root 表示处理范围的根目录。
        root = sandbox.resolve(path, must_exist=True)
        if not root.is_dir():
            return ToolResult("search_files", False, "目标不是目录", error_code="not_directory")
        # 变量说明：flags 表示当前流程使用的 flags 集合。
        flags = 0 if case_sensitive else re.IGNORECASE
        # 变量说明：matcher 表示当前步骤使用的 matcher 值。
        matcher = re.compile(re.escape(query), flags)
        # 变量说明：matches 表示当前流程使用的 matches 集合。
        matches: list[str] = []
        # 变量说明：skipped 表示当前步骤使用的 skipped 值。
        skipped = 0
        # 变量说明：stats 表示当前流程使用的 stats 集合。
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
                # 变量说明：text 表示当前步骤使用的 text 值。
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


# 函数职责：完成 glob_files 对应的业务处理。
# 参数关系：sandbox 表示当前步骤使用的 sandbox 值；pattern 表示当前步骤使用的 pattern 值；path 表示当前文件或目录路径；limit 表示当前步骤使用的 limit 值；max_entries 表示当前流程使用的 max_entries 集合。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def glob_files(
    sandbox: WorkspaceSandbox,
    pattern: str,
    *,
    path: str = ".",
    limit: int = 200,
    max_entries: int = 10_000,
) -> ToolResult:
    """List workspace paths matching a glob without following links.

    ``Path.glob`` is deliberately not used because its link traversal behavior
    varies across Python versions and platforms.  The shared safe walker
    resolves every candidate through :class:`WorkspaceSandbox` instead.
    """

    try:
        # 变量说明：normalized_pattern 表示当前步骤使用的 normalized_pattern 值。
        normalized_pattern = str(pattern or "").strip().replace("\\", "/")
        if not normalized_pattern:
            return ToolResult("glob", False, "pattern 不能为空", error_code="invalid_pattern")
        if len(normalized_pattern) > 512:
            return ToolResult("glob", False, "glob pattern 不能超过 512 个字符", error_code="invalid_pattern")
        if Path(normalized_pattern).is_absolute():
            return ToolResult(
                "glob",
                False,
                "glob pattern 必须是工作区相对模式，例如 **/*.py；不要传入盘符或绝对路径",
                error_code="invalid_pattern",
            )
        # 变量说明：root 表示处理范围的根目录。
        root = sandbox.resolve(path, must_exist=True)
        if not root.is_dir():
            return ToolResult("glob", False, "目标不是目录", error_code="not_directory")

        # 变量说明：entry_limit 表示当前步骤使用的 entry_limit 值。
        entry_limit = min(max(int(max_entries), 1), 100_000)
        # 变量说明：result_limit 表示当前步骤使用的 result_limit 值。
        result_limit = min(max(int(limit), 1), 2_000)
        # 变量说明：stats 表示当前流程使用的 stats 集合。
        stats = {"skipped": 0, "visited": 0, "scan_limit_reached": 0}
        # 变量说明：matches 表示当前流程使用的 matches 集合。
        matches: list[str] = []
        for lexical_path, safe_path in _safe_walk(
            sandbox,
            root,
            recursive=True,
            max_entries=entry_limit,
            stats=stats,
        ):
            try:
                # 变量说明：relative_to_root 表示当前步骤使用的 relative_to_root 值。
                relative_to_root = safe_path.relative_to(root).as_posix()
            except ValueError:
                # Defensive: _safe_walk already resolves inside the sandbox.
                continue
            if not (
                fnmatch.fnmatchcase(relative_to_root, normalized_pattern)
                or Path(relative_to_root).match(normalized_pattern)
            ):
                continue
            matches.append(f"{'dir' if safe_path.is_dir() else 'file'}\t{sandbox.relative(lexical_path)}")
            if len(matches) >= result_limit:
                break
        return ToolResult(
            "glob",
            True,
            "\n".join(matches) if matches else "未找到匹配文件",
            metadata={
                "count": len(matches),
                "truncated": len(matches) >= result_limit or bool(stats["scan_limit_reached"]),
                "skipped": stats["skipped"],
                "visited": stats["visited"],
            },
        )
    except SandboxViolation as exc:
        return ToolResult(
            "glob",
            False,
            f"glob 的 path 必须是工作区相对路径，例如 '.'：{exc}",
            error_code="glob_error",
        )
    except (FileNotFoundError, OSError, ValueError) as exc:
        return ToolResult("glob", False, str(exc), error_code="glob_error")


# 函数职责：完成 unsafe_regular_expression 对应的业务处理。
# 参数关系：pattern 表示当前步骤使用的 pattern 值。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def _unsafe_regular_expression(pattern: str) -> bool:
    """Reject the common catastrophic-backtracking shapes before scanning files."""

    # This is intentionally conservative rather than pretending to prove that
    # every regexp is linear.  It blocks the frequent `(a+)+` / `(.*)*` forms
    # while normal source-code search patterns remain available.
    return bool(re.search(r"\((?:[^()]|\([^()]*\))*[+*][^)]*\)[+*{]", pattern))


# 函数职责：完成 grep_files 对应的业务处理。
# 参数关系：sandbox 表示当前步骤使用的 sandbox 值；pattern 表示当前步骤使用的 pattern 值；path 表示当前文件或目录路径；file_pattern 表示当前步骤使用的 file_pattern 值；case_sensitive 表示当前步骤使用的 case_sensitive 值；limit 表示当前步骤使用的 limit 值；max_entries 表示当前流程使用的 max_entries 集合。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def grep_files(
    sandbox: WorkspaceSandbox,
    pattern: str,
    *,
    path: str = ".",
    file_pattern: str = "*",
    case_sensitive: bool = False,
    limit: int = 100,
    max_entries: int = 10_000,
) -> ToolResult:
    """Search text files with a bounded regular expression scan."""

    try:
        # 变量说明：query 表示当前步骤使用的 query 值。
        query = str(pattern or "")
        if not query:
            return ToolResult("grep", False, "pattern 不能为空", error_code="invalid_pattern")
        if len(query) > 512 or _unsafe_regular_expression(query):
            return ToolResult("grep", False, "正则表达式过长或可能造成过度回溯", error_code="unsafe_pattern")
        # 变量说明：root 表示处理范围的根目录。
        root = sandbox.resolve(path, must_exist=True)
        if not root.is_dir():
            return ToolResult("grep", False, "目标不是目录", error_code="not_directory")
        # 变量说明：matcher 表示当前步骤使用的 matcher 值。
        matcher = re.compile(query, 0 if case_sensitive else re.IGNORECASE)
        # 变量说明：entry_limit 表示当前步骤使用的 entry_limit 值。
        entry_limit = min(max(int(max_entries), 1), 100_000)
        # 变量说明：result_limit 表示当前步骤使用的 result_limit 值。
        result_limit = min(max(int(limit), 1), 2_000)
        # 变量说明：stats 表示当前流程使用的 stats 集合。
        stats = {"skipped": 0, "visited": 0, "scan_limit_reached": 0}
        # 变量说明：skipped 表示当前步骤使用的 skipped 值。
        skipped = 0
        # 变量说明：matches 表示当前流程使用的 matches 集合。
        matches: list[str] = []
        for lexical_path, safe_path in _safe_walk(
            sandbox,
            root,
            recursive=True,
            max_entries=entry_limit,
            stats=stats,
        ):
            if not safe_path.is_file():
                continue
            # 变量说明：relative_path 表示relative_path 对应的文件系统位置。
            relative_path = sandbox.relative(lexical_path)
            if not fnmatch.fnmatchcase(relative_path, file_pattern):
                continue
            try:
                if safe_path.stat().st_size > 2_000_000:
                    skipped += 1
                    continue
                with safe_path.open("r", encoding="utf-8", errors="replace") as stream:
                    for line_number, line in enumerate(stream, start=1):
                        # Bound the candidate text too, preventing a single
                        # generated line from monopolizing the model loop.
                        # 变量说明：preview 表示当前步骤使用的 preview 值。
                        preview = line[:10_000]
                        if matcher.search(preview):
                            matches.append(f"{relative_path}:{line_number}: {preview.rstrip()[:500]}")
                            if len(matches) >= result_limit:
                                break
            except OSError:
                skipped += 1
            if len(matches) >= result_limit:
                break
        return ToolResult(
            "grep",
            True,
            "\n".join(matches) if matches else "未找到匹配内容",
            metadata={
                "count": len(matches),
                "truncated": len(matches) >= result_limit or bool(stats["scan_limit_reached"]),
                "skipped": skipped + stats["skipped"],
                "visited": stats["visited"],
            },
        )
    except (SandboxViolation, FileNotFoundError, OSError, re.error, ValueError) as exc:
        return ToolResult("grep", False, str(exc), error_code="grep_error")


# 函数职责：完成 ripgrep_search 对应的业务处理。
# 参数关系：sandbox 表示当前步骤使用的 sandbox 值；pattern 表示当前步骤使用的 pattern 值；path 表示当前文件或目录路径；glob 表示当前步骤使用的 glob 值；case_sensitive 表示当前步骤使用的 case_sensitive 值；fixed_strings 表示当前流程使用的 fixed_strings 集合；context 表示当前步骤使用的 context 值；limit 表示当前步骤使用的 limit 值。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def ripgrep_search(
    sandbox: WorkspaceSandbox,
    pattern: str,
    *,
    path: str = ".",
    glob: str | None = None,
    case_sensitive: bool = False,
    fixed_strings: bool = False,
    context: int = 0,
    limit: int = 200,
) -> ToolResult:
    """Run ripgrep without a shell and keep traversal inside the workspace."""

    # 变量说明：executable 表示当前步骤使用的 executable 值。
    executable = shutil.which("rg")
    if executable is None:
        return ToolResult(
            "rg",
            False,
            "ripgrep executable is unavailable; use tool_search with select:grep, then call grep instead",
            error_code="tool_unavailable",
            metadata={"fallback_tool": "grep", "fallback_activation": "select:grep"},
        )
    try:
        # 变量说明：target 表示当前步骤使用的 target 值。
        target = sandbox.resolve(path, must_exist=True)
        # 变量说明：relative_target 表示当前步骤使用的 relative_target 值。
        relative_target = sandbox.relative(target)
        # 变量说明：result_limit 表示当前步骤使用的 result_limit 值。
        result_limit = min(max(int(limit), 1), 2_000)
        # 变量说明：args 表示当前流程使用的 args 集合。
        args = [
            executable,
            "--line-number",
            "--no-heading",
            "--with-filename",
            "--color",
            "never",
            "--no-follow",
            "--max-columns",
            "1000",
            "--max-filesize",
            "2M",
        ]
        if not case_sensitive:
            args.append("--ignore-case")
        if fixed_strings:
            args.append("--fixed-strings")
        # 变量说明：context_lines 表示当前流程使用的 context_lines 集合。
        context_lines = min(max(int(context), 0), 20)
        if context_lines:
            args.extend(["--context", str(context_lines)])
        if glob:
            args.extend(["--glob", str(glob)])
        args.extend(["--", str(pattern), relative_target])
        # 变量说明：process 表示当前流程使用的 process 集合。
        process = subprocess.Popen(
            args,
            cwd=sandbox.root,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            shell=False,
        )
        # 变量说明：lines 表示当前流程使用的 lines 集合。
        lines: list[str] = []
        # 变量说明：truncated 表示当前步骤使用的 truncated 值。
        truncated = False
        assert process.stdout is not None
        for line in process.stdout:
            if len(lines) >= result_limit:
                # 变量说明：truncated 表示当前步骤使用的 truncated 值。
                truncated = True
                process.terminate()
                break
            lines.append(line[:2_000])
        process.stdout.close()
        try:
            # 变量说明：exit_code 表示当前步骤使用的 exit_code 值。
            exit_code = process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            # 变量说明：exit_code 表示当前步骤使用的 exit_code 值。
            exit_code = process.wait(timeout=5)
        if not truncated and exit_code not in {0, 1}:
            return ToolResult("rg", False, "".join(lines), error_code="rg_error", metadata={"exit_code": exit_code})
        return ToolResult(
            "rg",
            True,
            "".join(lines) if lines else "No matches found",
            metadata={
                "count": len(lines),
                "truncated": truncated,
                "path": relative_target,
                "exit_code": exit_code,
            },
        )
    except (SandboxViolation, FileNotFoundError, OSError, ValueError) as exc:
        return ToolResult("rg", False, str(exc), error_code="rg_error")


# 函数职责：完成 edit_file 对应的业务处理。
# 参数关系：sandbox 表示当前步骤使用的 sandbox 值；path 表示当前文件或目录路径；old_string 表示当前步骤使用的 old_string 值；new_string 表示当前步骤使用的 new_string 值；replace_all 表示当前步骤使用的 replace_all 值；approved 表示当前步骤使用的 approved 值。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def edit_file(
    sandbox: WorkspaceSandbox,
    path: str,
    old_string: str,
    new_string: str,
    *,
    replace_all: bool = False,
    approved: bool = False,
) -> ToolResult:
    """Apply an exact text replacement to one sandboxed UTF-8 text file."""

    # 变量说明：arguments 表示当前流程使用的 arguments 集合。
    arguments = {
        "path": path,
        "old_string": old_string,
        "new_string": new_string,
        "replace_all": replace_all,
    }
    if not approved:
        return _approval("edit", arguments, f"修改文件需要批准: {path}")
    if not old_string:
        return ToolResult("edit", False, "old_string 不能为空", error_code="invalid_edit")
    try:
        # 变量说明：target 表示当前步骤使用的 target 值。
        target = sandbox.resolve(path, must_exist=True)
        if not target.is_file():
            return ToolResult("edit", False, "目标不是文件", error_code="not_file")
        with target.open("rb") as binary_stream:
            if b"\x00" in binary_stream.read(8192):
                return ToolResult("edit", False, "暂不支持编辑二进制文件", error_code="binary_file")
        # Keep CRLF/LF exactly as stored except for the requested replacement.
        with target.open("r", encoding="utf-8", errors="replace", newline="") as stream:
            # 变量说明：original 表示当前步骤使用的 original 值。
            original = stream.read()
        # 变量说明：matches 表示当前流程使用的 matches 集合。
        matches = original.count(old_string)
        if not matches:
            return ToolResult("edit", False, "未找到 old_string，未修改文件", error_code="edit_not_found")
        if matches > 1 and not replace_all:
            return ToolResult(
                "edit",
                False,
                "old_string 匹配了多处；请提供更精确内容或设置 replace_all",
                error_code="edit_not_unique",
                metadata={"matches": matches},
            )
        # 变量说明：updated 表示当前步骤使用的 updated 值。
        updated = original.replace(old_string, new_string, -1 if replace_all else 1)
        with target.open("w", encoding="utf-8", newline="") as stream:
            stream.write(updated)
        # 变量说明：replacements 表示当前流程使用的 replacements 集合。
        replacements = matches if replace_all else 1
        from src.coding.changes import build_file_change
        relative = sandbox.relative(target)
        change = build_file_change(relative, "update", original, updated)
        return ToolResult(
            "edit",
            True,
            f"已修改 {relative}，替换 {replacements} 处",
            changed=updated != original,
            metadata={
                "path": relative,
                "replacements": replacements,
                "change_set": {"status": "applied", "source": "file_tool", "file_count": 1, "files": [change]},
            },
        )
    except (SandboxViolation, FileNotFoundError, OSError) as exc:
        return ToolResult("edit", False, str(exc), error_code="edit_error")


# 函数职责：完成 is_public_ip 对应的业务处理。
# 参数关系：address 表示当前流程使用的 address 集合。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def _is_public_ip(address: str) -> bool:
    try:
        # 变量说明：candidate 表示当前步骤使用的 candidate 值。
        candidate = ipaddress.ip_address(address)
    except ValueError:
        return False
    return candidate.is_global


# urllib 同时支持环境变量和 Windows 系统代理；按目标应用 bypass 后显式交给 httpx。
def _environment_proxy(url: str = "https://www.bing.com/") -> str | None:
    parsed = urlsplit(url)
    proxies = urllib.request.getproxies()
    if urllib.request.proxy_bypass(parsed.netloc):
        return None
    proxy = proxies.get(parsed.scheme.lower()) or proxies.get("all")
    return (proxy if "://" in proxy else f"http://{proxy}") if proxy else None


# 函数职责：校验 public_http_url 对应的数据或流程。
# 参数关系：url 表示当前步骤使用的 url 值。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def _validate_public_http_url(url: str) -> tuple[str, str]:
    """Validate scheme and DNS answers before a web request.

    Redirects are handled by the caller so every hop can be checked.  Callers
    may use an environment proxy, but the requested hostname is still resolved
    and checked before the request.  This is a strong server-side guard for
    normal use; it deliberately does not claim that a generic HTTP client turns
    arbitrary network access into a filesystem sandbox.
    """

    # 变量说明：candidate 表示当前步骤使用的 candidate 值。
    candidate = str(url or "").strip()
    if not candidate or len(candidate) > 4_096:
        raise UnsafeWebUrlError("URL 不能为空或过长")
    # 变量说明：parsed 表示当前步骤使用的 parsed 值。
    parsed = urlsplit(candidate)
    if parsed.scheme.lower() not in {"http", "https"}:
        raise UnsafeWebUrlError("仅允许 http 或 https URL")
    if not parsed.hostname:
        raise UnsafeWebUrlError("URL 缺少主机名")
    if parsed.username or parsed.password:
        raise UnsafeWebUrlError("URL 不允许包含用户名或密码")
    # 变量说明：host 表示当前步骤使用的 host 值。
    host = parsed.hostname.rstrip(".").lower()
    if host in {"localhost", "localhost.localdomain"} or host.endswith(".local"):
        raise UnsafeWebUrlError("不允许访问本机或本地网络地址")
    try:
        # 变量说明：port 表示当前步骤使用的 port 值。
        port = parsed.port or (443 if parsed.scheme.lower() == "https" else 80)
    except ValueError as exc:
        raise UnsafeWebUrlError("URL 端口无效") from exc
    try:
        # 变量说明：answers 表示当前流程使用的 answers 集合。
        answers = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise WebHostResolutionError("无法解析目标主机") from exc
    # 变量说明：addresses 表示当前流程使用的 addresses 集合。
    addresses = {str(answer[4][0]) for answer in answers if answer[4]}
    if not addresses or any(not _is_public_ip(address) for address in addresses):
        raise UnsafeWebUrlError("不允许访问私有、回环或保留网络地址")
    # Canonicalizing the hostname avoids a host spelling changing after the
    # validation decision, while preserving the query and fragment semantics.
    # 变量说明：safe_url 表示safe 的访问地址。
    safe_url = urlunsplit((parsed.scheme.lower(), parsed.netloc, parsed.path or "/", parsed.query, ""))
    return safe_url, host


# 函数职责：完成 response_peer_is_public 对应的业务处理。
# 参数关系：response 表示下游返回的响应。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def _response_peer_is_public(response: httpx.Response, *, proxy_url: str | None = None, proxy_configured: bool | None = None) -> bool:
    """Best-effort second SSRF check against the actual connected peer."""

    # 变量说明：stream 表示当前步骤使用的 stream 值。
    stream = response.extensions.get("network_stream")
    if stream is None or not hasattr(stream, "get_extra_info"):
        # Some mock/custom transports do not expose peer information.  The
        # preflight DNS guard remains in effect and keeps tests transport-agnostic.
        return True
    # 变量说明：peer 表示当前步骤使用的 peer 值。
    try:
        peer = stream.get_extra_info("server_addr")
    except OSError:
        # 响应离开上下文后底层 socket 可能已关闭；此时 peer 信息不可读，
        # 仍保留前置 DNS 公网校验，不把不可观测误报成 unsafe_url。
        return True
    if not peer:
        return True
    try:
        if _is_public_ip(str(peer[0])):
            return True
        # 只认可本次请求实际使用的代理对端，而不是“配置过代理就放行所有私网”。
        if proxy_url or proxy_configured:
            if not proxy_url:
                return True
            proxy = urlsplit(proxy_url)
            port = proxy.port or (443 if proxy.scheme == "https" else 80)
            addresses = {answer[4][0] for answer in socket.getaddrinfo(proxy.hostname, port, type=socket.SOCK_STREAM)}
            return str(peer[0]) in addresses and int(peer[1]) == port
        return False
    except (IndexError, TypeError, ValueError, OSError):
        return False


# 函数职责：完成 web_fetch 对应的业务处理。
# 参数关系：_sandbox 表示当前步骤使用的 _sandbox 值；url 表示当前步骤使用的 url 值；timeout_seconds 表示当前流程使用的 timeout_seconds 集合；max_bytes 表示当前流程使用的 max_bytes 集合；offset 表示当前步骤使用的 offset 值；max_chars 表示当前流程使用的 max_chars 集合；_tool_name 表示当前步骤使用的 _tool_name 值。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def web_fetch(
    _sandbox: WorkspaceSandbox,
    url: str,
    *,
    timeout_seconds: float = 15,
    max_bytes: int = MAX_WEB_RESPONSE_BYTES,
    offset: int = 0,
    max_chars: int = DEFAULT_WEB_PAGE_CHARS,
    _tool_name: str = "webfetch",
    _include_document: bool = False,
) -> ToolResult:
    """Open a public page as bounded structured text, validating every redirect."""

    metadata: dict[str, Any] = {"stage": "arguments"}
    try:
        # 变量说明：timeout 表示当前步骤使用的 timeout 值。
        timeout = min(max(float(timeout_seconds), 1.0), 30.0)
        # 变量说明：byte_limit 表示当前步骤使用的 byte_limit 值。
        byte_limit = min(max(int(max_bytes), 1_024), MAX_WEB_RESPONSE_BYTES)
        # 变量说明：page_offset 表示当前步骤使用的 page_offset 值。
        page_offset = max(int(offset), 0)
        # 变量说明：page_limit 表示当前步骤使用的 page_limit 值。
        page_limit = min(max(int(max_chars), 1_000), MAX_WEB_PAGE_CHARS)
        # 变量说明：headers 表示当前流程使用的 headers 集合。
        headers = {
            "User-Agent": "PGAgent/0.1 (+local safe web open)",
            "Accept": "text/plain,text/html,application/json,application/xml,text/xml;q=0.9,*/*;q=0.1",
        }
        # 变量说明：requested_url 表示requested 的访问地址。
        requested_url = str(url or "").strip()
        # 变量说明：current_url 表示current 的访问地址。
        current_url = requested_url
        # 变量说明：redirects 表示当前流程使用的 redirects 集合。
        redirects: list[str] = []
        for redirect_count in range(MAX_WEB_REDIRECTS + 1):
            metadata["stage"] = "redirect_validation" if redirects else "url_validation"
            safe_url, host = _validate_public_http_url(current_url)
            proxy = _environment_proxy(safe_url)
            metadata.update(url=safe_url, proxy_used=bool(proxy), stage="request")
            body = bytearray()
            # 禁止 httpx 再隐式选择另一条路由；redirect 的下一跳重新解析代理和 bypass。
            with httpx.Client(follow_redirects=False, timeout=httpx.Timeout(timeout),
                              trust_env=False, proxy=proxy, headers=headers) as client:
                with client.stream("GET", safe_url) as response:
                    metadata.update(stage="peer_validation", status_code=response.status_code)
                    if not _response_peer_is_public(response, proxy_url=proxy, proxy_configured=bool(proxy)):
                        return ToolResult(_tool_name, False, "连接对端既不是公网地址，也不是本次请求选用的代理", error_code="unsafe_url", metadata=metadata)
                    metadata["stage"] = "http"
                    if 300 <= response.status_code < 400:
                        # 变量说明：location 表示当前步骤使用的 location 值。
                        location = response.headers.get("location", "").strip()
                        if not location:
                            return ToolResult(_tool_name, False, "服务器返回了缺少目标地址的重定向", error_code="http_error")
                        if redirect_count >= MAX_WEB_REDIRECTS:
                            return ToolResult(_tool_name, False, "网页重定向次数过多", error_code="too_many_redirects")
                        # 变量说明：current_url 表示current 的访问地址。
                        current_url = urljoin(safe_url, location)
                        # Validation occurs at the start of the next loop before any request.
                        redirects.append(current_url)
                        continue
                    for chunk in response.iter_bytes():
                        # 变量说明：remaining 表示当前步骤使用的 remaining 值。
                        remaining = byte_limit - len(body)
                        if remaining <= 0:
                            break
                        body.extend(chunk[:remaining])
                        if len(chunk) > remaining:
                            break
                    # 变量说明：status_code 表示当前步骤使用的 status_code 值。
                    status_code = response.status_code
                    # 变量说明：content_type 表示当前步骤使用的 content_type 值。
                    content_type = response.headers.get("content-type", "").lower()
                    # 变量说明：encoding 表示当前步骤使用的 encoding 值。
                    encoding = response.encoding or "utf-8"
                    break
        else:  # pragma: no cover - bounded loop always returns or breaks
            raise RuntimeError("redirect loop ended unexpectedly")

        # 变量说明：truncated_bytes 表示当前流程使用的 truncated_bytes 集合。
        truncated_bytes = len(body) >= byte_limit
        # 变量说明：metadata 表示当前步骤使用的 metadata 值。
        metadata = {
            **metadata,
            "url": safe_url,
            "requested_url": requested_url,
            "host": host,
            "status_code": status_code,
            "content_type": content_type or "unknown",
            "bytes_read": len(body),
            "redirects": redirects,
        }
        if not 200 <= status_code < 300:
            return ToolResult(
                _tool_name,
                False,
                f"网页请求失败：HTTP {status_code}。错误响应正文未放入上下文。",
                error_code="http_error",
                metadata=metadata,
            )
        if not any(marker in content_type for marker in ("text/", "json", "xml", "javascript")):
            return ToolResult(
                _tool_name,
                False,
                "已读取非文本响应；为避免将二进制内容写入上下文，不返回正文。",
                metadata={**metadata, "truncated": truncated_bytes},
                error_code="unsupported_content_type",
            )

        # 变量说明：decoded 表示当前步骤使用的 decoded 值。
        decoded = bytes(body).decode(encoding, errors="replace")
        # 变量说明：title 表示当前步骤使用的 title 值。
        title = ""
        # 变量说明：description 表示当前步骤使用的 description 值。
        description = ""
        # 变量说明：published_at 表示published_at 对应的时间信息。
        published_at = ""
        links: list[dict[str, Any]] = []
        if "html" in content_type or re.search(r"<html\b", decoded[:2_000], re.IGNORECASE):
            # 变量说明：parser 表示当前步骤使用的 parser 值。
            parser = _ReadableHtmlParser()
            parser.feed(decoded)
            for link in parser.links:
                target_url = urljoin(safe_url, link["href"])
                if urlsplit(target_url).scheme in {"http", "https"}:
                    links.append({"id": link["id"], "url": target_url, "text": link["text"][:300]})
            # 变量说明：readable 表示当前步骤使用的 readable 值。
            readable = parser.readable_text()
            # 变量说明：title 表示当前步骤使用的 title 值。
            title = " ".join(parser.title_parts).strip()
            # 变量说明：description 表示当前步骤使用的 description 值。
            description = (
                parser.metadata.get("description")
                or parser.metadata.get("og:description")
                or parser.metadata.get("twitter:description")
                or ""
            )
            # 变量说明：published_at 表示published_at 对应的时间信息。
            published_at = (
                parser.metadata.get("article:published_time")
                or parser.metadata.get("date")
                or parser.metadata.get("datepublished")
                or ""
            )
        elif "json" in content_type:
            try:
                # 变量说明：readable 表示当前步骤使用的 readable 值。
                readable = json.dumps(json.loads(decoded), ensure_ascii=False, indent=2)
            except json.JSONDecodeError:
                # 变量说明：readable 表示当前步骤使用的 readable 值。
                readable = decoded
        else:
            # 变量说明：readable 表示当前步骤使用的 readable 值。
            readable = _clean_html_text(decoded) if "xml" in content_type else decoded

        # 变量说明：readable 表示当前步骤使用的 readable 值。
        # 原始源码的开头空行也是行号的一部分，不做 strip。
        # 变量说明：page 表示当前步骤使用的 page 值。
        page = readable[page_offset:page_offset + page_limit]
        # 变量说明：next_offset 表示当前步骤使用的 next_offset 值。
        next_offset = page_offset + len(page) if page_offset + len(page) < len(readable) else None
        # 变量说明：payload 表示跨层传递的数据载荷。
        payload = {
            "title": title[:500] or None,
            "url": safe_url,
            "description": " ".join(description.split())[:1_000] or None,
            "published_at": published_at[:200] or None,
            "content": page,
            "links": links,
        }
        return ToolResult(
            _tool_name,
            True,
            json.dumps(payload, ensure_ascii=False),
            metadata={
                **metadata,
                "offset": page_offset,
                "next_offset": next_offset,
                "total_chars": len(readable),
                "truncated": truncated_bytes or next_offset is not None,
                **({"document": {
                    "text": readable, "url": safe_url, "title": title[:500] or None,
                    "links": links, "source_truncated": truncated_bytes,
                    "content_type": content_type,
                    "published_at": published_at[:200] or None,
                    "date_source": "page_metadata" if published_at else None,
                }} if _include_document else {}),
            },
        )
    except UnsafeWebUrlError as exc:
        return ToolResult(_tool_name, False, str(exc), error_code="unsafe_url", metadata=metadata)
    except WebHostResolutionError:
        return ToolResult(_tool_name, False, "网络请求失败: 无法解析目标主机", error_code="dns_error", metadata=metadata)
    except httpx.TimeoutException:
        return ToolResult(_tool_name, False, "网页请求超时", error_code="network_timeout", metadata=metadata)
    except httpx.ProxyError:
        return ToolResult(_tool_name, False, "代理连接失败", error_code="proxy_error", metadata=metadata)
    except httpx.HTTPError as exc:
        return ToolResult(_tool_name, False, f"网络请求失败: {type(exc).__name__}", error_code="network_error", metadata=metadata)
    except ValueError:
        return ToolResult(_tool_name, False, "网页读取参数或响应编码无效", error_code="invalid_arguments", metadata=metadata)


# 函数职责：完成 web_open 对应的业务处理。
# 参数关系：sandbox 表示当前步骤使用的 sandbox 值；url 表示当前步骤使用的 url 值；timeout_seconds 表示当前流程使用的 timeout_seconds 集合；offset 表示当前步骤使用的 offset 值；max_chars 表示当前流程使用的 max_chars 集合。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def web_open(
    sandbox: WorkspaceSandbox,
    url: str,
    *,
    timeout_seconds: float = 15,
    offset: int = 0,
    max_chars: int = DEFAULT_WEB_PAGE_CHARS,
    _include_document: bool = False,
) -> ToolResult:
    """Structured replacement for webfetch; the legacy entry point remains executable."""

    return web_fetch(
        sandbox,
        url,
        timeout_seconds=timeout_seconds,
        offset=offset,
        max_chars=max_chars,
        _tool_name="web_open",
        _include_document=_include_document,
    )


# 函数职责：完成 clean_html_text 对应的业务处理。
# 参数关系：value 表示当前字段或计算值。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def _clean_html_text(value: str) -> str:
    # 变量说明：without_tags 表示当前流程使用的 without_tags 集合。
    without_tags = re.sub(r"<[^>]+>", " ", value)
    return " ".join(html.unescape(without_tags).split())


# 函数职责：完成 unwrap_duckduckgo_url 对应的业务处理。
# 参数关系：value 表示当前字段或计算值。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def _unwrap_duckduckgo_url(value: str) -> str:
    # 变量说明：candidate 表示当前步骤使用的 candidate 值。
    candidate = html.unescape(value)
    if candidate.startswith("//"):
        # 变量说明：candidate 表示当前步骤使用的 candidate 值。
        candidate = f"https:{candidate}"
    # 变量说明：parsed 表示当前步骤使用的 parsed 值。
    parsed = urlsplit(candidate)
    if parsed.netloc.endswith("duckduckgo.com"):
        # 变量说明：redirect_target 表示当前步骤使用的 redirect_target 值。
        redirect_target = parse_qs(parsed.query).get("uddg", [""])[0]
        if redirect_target:
            return redirect_target
    return candidate


# 函数职责：完成 duckduckgo_results 对应的业务处理。
# 参数关系：markup 表示当前步骤使用的 markup 值；limit 表示当前步骤使用的 limit 值。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def _duckduckgo_results(markup: str, limit: int) -> list[tuple[str, str]]:
    """Extract result links from the public HTML endpoint without a new parser dep."""

    # 变量说明：result_re 表示当前步骤使用的 result_re 值。
    result_re = re.compile(
        r"<a\b(?=[^>]*\bclass=[\"'][^\"']*\bresult__a\b[^\"']*[\"'])"
        r"(?=[^>]*\bhref=[\"'](?P<href>[^\"']+)[\"'])[^>]*>(?P<title>.*?)</a>",
        re.IGNORECASE | re.DOTALL,
    )
    # 变量说明：results 表示批量处理结果集合。
    results: list[tuple[str, str]] = []
    for match in result_re.finditer(markup):
        # 变量说明：title 表示当前步骤使用的 title 值。
        title = _clean_html_text(match.group("title"))
        # 变量说明：destination 表示当前步骤使用的 destination 值。
        destination = _unwrap_duckduckgo_url(match.group("href"))
        if not title or not destination:
            continue
        results.append((title[:500], destination[:2_000]))
        if len(results) >= limit:
            break
    return results


def _bing_rss_results(markup: str, limit: int) -> list[dict[str, Any]]:
    """Parse Bing RSS while retaining the evidence fields needed by the model."""
    try:
        root = ET.fromstring(markup)
    except ET.ParseError:
        return []
    results: list[dict[str, Any]] = []
    for item in root.findall('.//item')[:limit]:
        title = _clean_html_text(item.findtext('title') or '')
        url = (item.findtext('link') or '').strip()
        if title and url:
            description = _clean_html_text(item.findtext('description') or '')
            published_at: str | None = None
            raw_date = (item.findtext('pubDate') or '').strip()
            if raw_date:
                try:
                    parsed = parsedate_to_datetime(raw_date)
                    if parsed.tzinfo is None:
                        parsed = parsed.replace(tzinfo=timezone.utc)
                    published_at = parsed.astimezone(timezone.utc).isoformat()
                except (TypeError, ValueError, OverflowError):
                    published_at = None
            hostname = (urlsplit(url).hostname or '').lower().removeprefix('www.')
            results.append({
                "title": title[:500],
                "url": url[:2_000],
                "description": description[:2_000],
                "published_at": published_at,
                "source": hostname,
            })
    return results


def _search_failure(error_code: str, message: str, *, stage: str, **metadata: Any) -> ToolResult:
    """Return a bounded diagnostic while keeping provider details out of content."""

    return ToolResult(
        "websearch",
        False,
        message,
        error_code=error_code,
        metadata={"provider": "bing_rss", "stage": stage, **metadata},
    )


def _bing_search_endpoint(query: str, proxy: str | None) -> str:
    """Choose a Bing host that does not require an unvalidated redirect."""

    host = "www.bing.com" if proxy else "cn.bing.com"
    return f"https://{host}/search?format=rss&q={quote_plus(query)}"


# 函数职责：完成 web_search 对应的业务处理。
# 参数关系：sandbox 表示当前步骤使用的 sandbox 值；query 表示当前步骤使用的 query 值；limit 表示当前步骤使用的 limit 值。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def web_search(
    sandbox: WorkspaceSandbox,
    query: str,
    *,
    limit: int = 5,
    recency: int | None = None,
    domains: list[str] | None = None,
) -> ToolResult:
    """Perform a provider-free Bing RSS search with the public-network guard."""

    # 变量说明：search_query 表示当前步骤使用的 search_query 值。
    search_query = str(query or "").strip()
    if not search_query or len(search_query) > 500:
        return ToolResult("websearch", False, "query 不能为空或过长", error_code="invalid_query")
    if not search_query.isascii() or not re.search(r"[A-Za-z]", search_query):
        return ToolResult(
            "websearch",
            False,
            "搜索查询必须是纯英文（ASCII）文本",
            error_code="invalid_query_language",
        )
    # 变量说明：result_limit 表示当前步骤使用的 result_limit 值。
    result_limit = min(max(int(limit), 1), 10)
    if recency is not None and int(recency) < 0:
        return ToolResult("websearch", False, "recency 必须是非负整数", error_code="invalid_query")
    recency_days = int(recency) if recency is not None else None
    domain_filters = {
        str(domain).strip().lower().removeprefix("www.")
        for domain in (domains or [])
        if str(domain).strip()
    }

    brave_key = str(settings.brave_search_api_key or os.getenv("PGAGENT_BRAVE_SEARCH_API_KEY") or "").strip()
    brave_error: str | None = None
    brave_status_code: int | None = None
    if brave_key:
        params: dict[str, Any] = {"q": search_query, "count": result_limit, "search_lang": "en", "country": "us"}
        if recency_days is not None:
            today = datetime.now(timezone.utc).date()
            params["freshness"] = "pd" if recency_days == 1 else f"{today - timedelta(days=recency_days)}to{today}"
        if domain_filters:
            params["q"] = f"{search_query} ({' OR '.join(f'site:{domain}' for domain in sorted(domain_filters))})"
        try:
            brave_proxy = _environment_proxy("https://api.search.brave.com/")
            with httpx.Client(
                headers={"Accept": "application/json", "X-Subscription-Token": brave_key},
                timeout=httpx.Timeout(15), follow_redirects=False,
                trust_env=False, proxy=brave_proxy,
            ) as client:
                with client.stream(
                    "GET", "https://api.search.brave.com/res/v1/web/search", params=params,
                ) as brave_response:
                    if not _response_peer_is_public(brave_response, proxy_url=brave_proxy, proxy_configured=bool(brave_proxy)):
                        payload, brave_error = None, "unsafe_url"
                    else:
                        brave_status_code = brave_response.status_code
                        if brave_response.status_code != 200:
                            payload, brave_error = None, f"http_{brave_response.status_code}"
                        else:
                            body = bytearray()
                            response_too_large = False
                            for chunk in brave_response.iter_bytes():
                                remaining = MAX_WEB_RESPONSE_BYTES - len(body)
                                if remaining <= 0 or len(chunk) > remaining:
                                    response_too_large = True
                                    break
                                body.extend(chunk)
                            if response_too_large:
                                payload, brave_error = None, "response_too_large"
                            else:
                                payload, brave_error = json.loads(bytes(body)), None
        except (httpx.HTTPError, ValueError, TypeError) as exc:
            payload, brave_error = None, type(exc).__name__
        if brave_error is None:
            if not isinstance(payload, Mapping) or not isinstance(payload.get("web", {}), Mapping):
                return _search_failure("search_response_error", "Brave 响应格式无效", stage="parse", provider="brave")
            brave_results = payload.get("web", {}).get("results", [])
            if not isinstance(brave_results, list):
                return _search_failure("search_response_error", "Brave 结果格式无效", stage="parse", provider="brave")
            results = []
            for item in brave_results:
                if not isinstance(item, Mapping) or not item.get("url"):
                    continue
                source = (urlsplit(str(item["url"])).hostname or "").lower().removeprefix("www.")
                if domain_filters and not any(source == domain or source.endswith(f".{domain}") for domain in domain_filters):
                    continue
                results.append({
                    "title": str(item.get("title") or "")[:500], "url": str(item["url"])[:2_000],
                    "description": _clean_html_text(str(item.get("description") or ""))[:2_000],
                    # Brave page_age 可为最后更新时间，不能当成新闻发布日期。
                    "published_at": None, "page_age": item.get("page_age"),
                    "date_source": "search_index", "opened": False, "source": source,
                })
            results = results[:result_limit]
            if not results:
                return _search_failure("search_empty_results", "Brave 未返回符合当前条件的结果；未放宽过滤或切换来源", stage="filter", provider="brave")
            return ToolResult(
                "websearch", True,
                "\n".join(f"{i}. {x['title']}\n   {x['url']}\n   {x['description']}" for i, x in enumerate(results, 1)),
                metadata={"provider": "brave", "count": len(results), "query": search_query, "results": results},
            )
        brave_error = f"brave_{brave_error}"
        if brave_error in {"brave_http_401", "brave_http_403", "brave_http_429"}:
            status_code = int(brave_error.rsplit("_", 1)[1])
            return _search_failure(brave_error, f"Brave Search API 返回 HTTP {status_code}", stage="http", provider="brave", status_code=status_code)
        if brave_error in {"brave_unsafe_url", "brave_response_too_large"}:
            return _search_failure(brave_error, "Brave 请求未通过网络安全检查", stage="validation", provider="brave")

    # Brave 已尝试但需回退时，Bing 的成功和失败都保留首个上游诊断。
    fallback_metadata = (
        {
            "fallback_from": "brave",
            "brave_error": brave_error,
            "initial_error_code": brave_error,
            **({"initial_status_code": brave_status_code}
               if brave_status_code is not None else {}),
        }
        if brave_key and brave_error else {}
    )

    def bing_failure(error_code: str, message: str, *, stage: str, **metadata: Any) -> ToolResult:
        return _search_failure(error_code, message, stage=stage, **fallback_metadata, **metadata)

    provider_query = search_query
    if domain_filters:
        provider_query = f"{search_query} ({' OR '.join(f'site:{domain}' for domain in sorted(domain_filters))})"

    def fetch_search_response(url: str) -> tuple[str, int] | None:
        """Read one bounded RSS response; None means only the proxy peer failed the second guard."""

        proxy = _environment_proxy(url)
        with httpx.Client(timeout=httpx.Timeout(15), follow_redirects=False, trust_env=False, proxy=proxy,
                headers={"User-Agent": "PGAgent/0.1 (+local search)"}) as client:
            with client.stream("GET", url) as response:
                if not _response_peer_is_public(response, proxy_url=proxy, proxy_configured=bool(proxy)):
                    return None
                response.raise_for_status()
                body = bytearray()
                for chunk in response.iter_bytes():
                    remaining = MAX_WEB_RESPONSE_BYTES - len(body)
                    if remaining <= 0:
                        break
                    body.extend(chunk[:remaining])
                    if len(chunk) > remaining:
                        break
                encoding = response.encoding or "utf-8"
                return bytes(body).decode(encoding, errors="replace"), response.status_code

    try:
        proxy = _environment_proxy()
        search_url, _host = _validate_public_http_url(_bing_search_endpoint(provider_query, proxy))
        response_data = fetch_search_response(search_url)
        if response_data is None:
            return bing_failure("search_unsafe_url", "搜索连接目标不是公共网络地址", stage="peer_validation")
        markup, status_code = response_data
    except UnsafeWebUrlError:
        return bing_failure("search_unsafe_url", "搜索目标未通过公共网络校验", stage="validation")
    except WebHostResolutionError:
        return bing_failure("search_dns_error", "搜索主机解析失败", stage="dns")
    except httpx.TimeoutException:
        return bing_failure("search_timeout", "搜索请求超时", stage="request")
    except httpx.ProxyError:
        return bing_failure("search_proxy_error", "搜索代理连接失败", stage="proxy")
    except httpx.ConnectError:
        return bing_failure("search_connection_error", "搜索服务连接失败", stage="connect")
    except httpx.HTTPStatusError as exc:
        status_code = exc.response.status_code if exc.response is not None else None
        return bing_failure("search_http_error", "搜索服务返回 HTTP 错误", stage="response", status_code=status_code)
    except httpx.HTTPError:
        return bing_failure("search_request_error", "搜索请求失败", stage="request")
    except (UnicodeError, ValueError):
        return bing_failure("search_response_error", "搜索响应无法解码", stage="response")
    # 变量说明：results 表示批量处理结果集合。
    # 先多取一些候选，再应用来源和时效过滤，避免前几条无关结果导致空集。
    try:
        ET.fromstring(markup)
    except ET.ParseError:
        return bing_failure("search_parse_error", "搜索响应不是有效 RSS/XML", stage="parse")
    results = _bing_rss_results(markup, max(result_limit, 20))
    if domain_filters:
        results = [
            result for result in results
            if any(
                (result.get("source") or "") == domain
                or (result.get("source") or "").endswith(f".{domain}")
                for domain in domain_filters
            )
        ]
    if recency_days is not None:
        cutoff = datetime.now(timezone.utc) - timedelta(days=recency_days)
        recent: list[dict[str, Any]] = []
        for result in results:
            published_at = result.get("published_at")
            if not published_at:
                continue
            try:
                published = datetime.fromisoformat(str(published_at))
            except ValueError:
                continue
            if published >= cutoff:
                recent.append(result)
        results = recent
    results = results[:result_limit]
    if not results:
        return bing_failure(
            "search_empty_results",
            "搜索响应有效，但没有符合当前查询或过滤条件的结果。",
            stage="filter",
            source_status=status_code,
            filtered=bool(domain_filters or recency_days is not None),
        )
    # 变量说明：lines 表示当前流程使用的 lines 集合。
    lines = [
        f"{index}. {result['title']}\n   {result['url']}"
        + (f"\n   {result['description']}" if result.get("description") else "")
        + (f"\n   发布时间: {result['published_at']}" if result.get("published_at") else "")
        for index, result in enumerate(results, start=1)
    ]
    return ToolResult(
        "websearch",
        True,
        "\n".join(lines),
        metadata={
            "provider": "bing_rss",
            **fallback_metadata,
            "count": len(results),
            "query": search_query,
            "results": [
                {"ref_id": f"search{index}", **result}
                for index, result in enumerate(results, start=1)
            ],
        },
    )


# 函数职责：完成 public_json 对应的业务处理。
# 参数关系：url 表示当前步骤使用的 url 值；params 表示当前流程使用的 params 集合。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def _public_json(url: str, *, params: Mapping[str, Any] | None = None, headers: Mapping[str, str] | None = None) -> tuple[dict[str, Any] | list[Any] | None, str | None]:
    try:
        # 变量说明：safe_url 表示safe 的访问地址；_ 表示当前步骤使用的 _ 值。
        safe_url, _ = _validate_public_http_url(url)
        proxy = _environment_proxy(safe_url)
        with httpx.Client(timeout=httpx.Timeout(15), trust_env=False, proxy=proxy, headers={"User-Agent": "PGAgent/0.1", **dict(headers or {})}) as client:
            # 变量说明：response 表示下游返回的响应。
            response = client.get(safe_url, params=dict(params or {}))
            if not _response_peer_is_public(response, proxy_url=proxy, proxy_configured=bool(proxy)):
                return None, "unsafe_url"
            response.raise_for_status()
            # 变量说明：payload 表示跨层传递的数据载荷。
            payload = response.json()
        return payload, None
    except UnsafeWebUrlError:
        return None, "unsafe_url"
    except WebHostResolutionError:
        return None, "dns_error"
    except (httpx.HTTPError, ValueError, json.JSONDecodeError):
        return None, "network_error"


# 函数职责：完成 web_weather 对应的业务处理。
# 参数关系：sandbox 表示当前步骤使用的 sandbox 值；location 表示当前步骤使用的 location 值；days 表示当前流程使用的 days 集合。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def web_weather(sandbox: WorkspaceSandbox, *, location: str, days: int = 3) -> ToolResult:
    del sandbox
    # 变量说明：place 表示当前步骤使用的 place 值。
    place = str(location or "").strip()
    if not place or len(place) > 200:
        return ToolResult("web_run", False, "location 不能为空且不能超过 200 个字符", error_code="invalid_arguments")
    # 变量说明：geo 表示当前步骤使用的 geo 值；error 表示当前捕获或准备上报的错误。
    geo, error = _public_json("https://geocoding-api.open-meteo.com/v1/search", params={"name": place, "count": 1, "language": "zh", "format": "json"})
    if error or not isinstance(geo, Mapping) or not geo.get("results"):
        return ToolResult("web_run", False, "无法找到天气地点", error_code=error or "not_found")
    # 变量说明：hit 表示当前步骤使用的 hit 值。
    hit = geo["results"][0]
    # 变量说明：forecast 表示当前步骤使用的 forecast 值；error 表示当前捕获或准备上报的错误。
    forecast, error = _public_json("https://api.open-meteo.com/v1/forecast", params={"latitude": hit["latitude"], "longitude": hit["longitude"], "current": "temperature_2m,relative_humidity_2m,weather_code,wind_speed_10m", "daily": "temperature_2m_max,temperature_2m_min,weather_code", "forecast_days": min(max(int(days), 1), 7), "timezone": "auto"})
    if error or not isinstance(forecast, Mapping):
        return ToolResult("web_run", False, "天气服务当前不可用", error_code=error or "network_error")
    return ToolResult(
        "web_run",
        True,
        json.dumps({"location": hit, "forecast": forecast}, ensure_ascii=False),
        metadata={"provider": "open-meteo", "source_url": "https://api.open-meteo.com/v1/forecast"},
    )


# 函数职责：完成 web_finance 对应的业务处理。
# 参数关系：sandbox 表示当前步骤使用的 sandbox 值；ticker 表示当前步骤使用的 ticker 值；type 表示当前步骤使用的 type 值。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def web_finance(sandbox: WorkspaceSandbox, *, ticker: str, type: str = "equity") -> ToolResult:
    del sandbox
    # 变量说明：symbol 表示当前步骤使用的 symbol 值。
    symbol = str(ticker or "").strip().upper()
    if not re.fullmatch(r"[A-Z0-9.\-^=]{1,20}", symbol):
        return ToolResult("web_run", False, "ticker 格式无效", error_code="invalid_arguments")
    # 变量说明：payload 表示跨层传递的数据载荷；error 表示当前捕获或准备上报的错误。
    payload, error = _public_json(f"https://query1.finance.yahoo.com/v8/finance/chart/{quote_plus(symbol)}", params={"range": "1d", "interval": "5m"})
    if error or not isinstance(payload, Mapping):
        return ToolResult("web_run", False, "行情服务当前不可用", error_code=error or "network_error")
    # 变量说明：result 表示本步骤产生的结果。
    result = ((payload.get("chart") or {}).get("result") or [None])[0]
    if not isinstance(result, Mapping):
        return ToolResult("web_run", False, "未找到行情数据", error_code="not_found")
    # 变量说明：meta 表示当前步骤使用的 meta 值。
    meta = result.get("meta") or {}
    return ToolResult("web_run", True, json.dumps({"ticker": symbol, "asset_type": type, "quote": meta}, ensure_ascii=False), metadata={"provider": "yahoo-finance"})


# 函数职责：完成 web_sports 对应的业务处理。
# 参数关系：sandbox 表示当前步骤使用的 sandbox 值；league 表示当前步骤使用的 league 值；date 表示当前步骤使用的 date 值；team 表示当前步骤使用的 team 值。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def web_sports(sandbox: WorkspaceSandbox, *, league: str, date: str | None = None, team: str | None = None) -> ToolResult:
    del sandbox
    # 变量说明：competition 表示当前步骤使用的 competition 值。
    competition = str(league or "").strip().lower()
    if not re.fullmatch(r"[a-z0-9._-]{2,20}", competition):
        return ToolResult("web_run", False, "league 格式无效", error_code="invalid_arguments")
    # 变量说明：params 表示当前流程使用的 params 集合。
    params = {"dates": date} if date else {}
    if team:
        params["limit"] = 100
    # 变量说明：payload 表示跨层传递的数据载荷；error 表示当前捕获或准备上报的错误。
    payload, error = _public_json(f"https://site.api.espn.com/apis/site/v2/sports/{competition}/scoreboard", params=params)
    if error or not isinstance(payload, Mapping):
        return ToolResult("web_run", False, "体育数据服务当前不可用", error_code=error or "network_error")
    return ToolResult("web_run", True, json.dumps({"league": competition, "events": payload.get("events", [])}, ensure_ascii=False)[:40_000], metadata={"provider": "espn"})


# 函数职责：完成 web_screenshot 对应的业务处理。
# 参数关系：sandbox 表示当前步骤使用的 sandbox 值；url 表示当前步骤使用的 url 值；full_page 表示当前步骤使用的 full_page 值。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def web_screenshot(sandbox: WorkspaceSandbox, *, url: str, full_page: bool = False) -> ToolResult:
    del sandbox
    try:
        # 变量说明：safe_url 表示safe 的访问地址；_ 表示当前步骤使用的 _ 值。
        safe_url, _ = _validate_public_http_url(url)
        from playwright.sync_api import sync_playwright
        with sync_playwright() as playwright:
            # 变量说明：browser 表示当前步骤使用的 browser 值。
            browser = playwright.chromium.launch(headless=True)
            # 变量说明：page 表示当前步骤使用的 page 值。
            page = browser.new_page()
            page.goto(safe_url, wait_until="domcontentloaded", timeout=20_000)
            # 变量说明：data 表示当前处理的数据。
            data = page.screenshot(type="png", full_page=bool(full_page))
            browser.close()
        return ToolResult("web_run", True, "网页截图已生成", metadata={"mime_type": "image/png", "data_base64": base64.b64encode(data).decode("ascii"), "url": safe_url})
    except (ImportError, Exception) as exc:
        return ToolResult("web_run", False, f"网页截图不可用: {type(exc).__name__}", error_code="tool_unavailable")


# 函数职责：完成 web_run 对应的业务处理。
# 参数关系：sandbox 表示当前步骤使用的 sandbox 值；search_query 表示当前步骤使用的 search_query 值；open 表示当前步骤使用的 open 值；click 表示当前步骤使用的 click 值；find 表示当前步骤使用的 find 值；screenshot 表示当前步骤使用的 screenshot 值；finance 表示当前步骤使用的 finance 值；weather 表示当前步骤使用的 weather 值；其余参数沿用调用方提供的扩展选项。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def web_run(
    sandbox: WorkspaceSandbox,
    *,
    search_query: list[Mapping[str, Any]] | None = None,
    open: list[Mapping[str, Any]] | None = None,
    click: list[Mapping[str, Any]] | None = None,
    find: list[Mapping[str, Any]] | None = None,
    screenshot: list[Mapping[str, Any]] | None = None,
    finance: list[Mapping[str, Any]] | None = None,
    weather: list[Mapping[str, Any]] | None = None,
    sports: list[Mapping[str, Any]] | None = None,
    time: list[Mapping[str, Any]] | None = None,
    pages: Mapping[str, Any] | None = None,
    maxItems: int | None = None,
    response_length: str = "short",
    _ref_prefix: str = "",
) -> ToolResult:
    """Execute the public Codex web_run command shape using PGAgent primitives."""

    # 变量说明：stored_pages 表示当前流程使用的 stored_pages 集合。
    stored_pages = dict(pages or {})
    # 兼容旧模型把数组 schema 的 maxItems 元数据误传为顶层参数；实际
    # 查询数量仍由 search_items 的运行时上限约束，不把该元数据当搜索命令。
    del maxItems
    # 变量说明：output 表示当前步骤使用的 output 值。
    output: list[dict[str, Any]] = []
    if response_length not in {"short", "medium", "long"}:
        return ToolResult("web_run", False, "response_length 必须是 short、medium 或 long", error_code="invalid_arguments")
    navigation_count = len(open or []) + len(click or []) + len(find or [])
    command_count = sum(len(items or []) for items in (
        search_query, open, click, find, screenshot, finance, weather, sports, time,
    ))
    if navigation_count > MAX_WEB_RUN_NAVIGATION_COMMANDS:
        return ToolResult("web_run", False, "一次 web_run 最多包含 10 个 open、click 或 find 命令", error_code="invalid_command")
    if command_count > MAX_WEB_RUN_COMMANDS:
        return ToolResult("web_run", False, "一次 web_run 最多包含 20 个命令", error_code="invalid_command")
    reader = PageReader(
        stored_pages,
        lambda url: web_open(sandbox, url, _include_document=True),
        max(1_000, {"short": 4_000, "medium": 12_000, "long": 24_000}[response_length] // max(1, navigation_count)),
        ref_prefix=_ref_prefix,
    )
    # 变量说明：source_url 表示本轮结果中可安全展示的来源地址。
    source_url: str | None = None
    search_items = list(search_query or [])
    if len(search_items) > 5:
        return ToolResult("web_run", False, "一次 web_run 最多包含 5 个 search_query", error_code="invalid_command")

    # 先规范化并去重查询，再并发请求；这样可避免模型在同一轮重复消耗搜索配额。
    unique_searches: list[tuple[str, Mapping[str, Any]]] = []
    seen_queries: set[tuple[str, tuple[str, ...], int | None]] = set()
    for item in search_items:
        query = " ".join(str(item.get("q") or item.get("query") or "").split())
        if not query or not query.isascii() or not re.search(r"[A-Za-z]", query):
            return ToolResult(
                "web_run",
                False,
                "搜索查询必须是纯英文（ASCII）文本；请先将用户意图转换为英文关键词",
                error_code="invalid_query_language",
            )
        raw_domains = item.get("domains")
        normalized_domains = tuple(sorted({
            str(domain).strip().lower().removeprefix("www.")
            for domain in (raw_domains if isinstance(raw_domains, (list, tuple)) else [])
            if str(domain).strip()
        }))
        recency_value = int(item["recency"]) if item.get("recency") is not None else None
        key = (query.casefold(), normalized_domains, recency_value)
        if key in seen_queries:
            continue
        seen_queries.add(key)
        unique_searches.append((query, item))

    def run_search(entry: tuple[str, Mapping[str, Any]]) -> tuple[str, ToolResult]:
        query, item = entry
        domains_value = item.get("domains")
        domains = list(domains_value) if isinstance(domains_value, (list, tuple)) else None
        result = web_search(
            sandbox,
            query,
            limit=int(item.get("limit") or 5),
            recency=int(item["recency"]) if item.get("recency") is not None else None,
            domains=domains,
        )
        # 严格来源/时效过滤在 RSS 上容易造成空召回；保留原查询并放宽过滤重试一次。
        filter_relaxable_errors = {"search_provider_unavailable", "search_empty_results"}
        if (result.metadata.get("provider") != "brave"
                and not result.ok and result.error_code in filter_relaxable_errors
                and (domains or item.get("recency") is not None)):
            fallback = web_search(
                sandbox,
                query,
                limit=int(item.get("limit") or 5),
                recency=None,
                domains=None,
            )
            if fallback.ok:
                fallback.metadata["fallback"] = "relaxed_filters"
                fallback.metadata["initial_error_code"] = result.error_code
                return query, fallback
            result = fallback
        # 代理或上游搜索服务偶发连接失败时，立即重试一次同一查询；不把
        # 瞬时网络抖动暴露为整轮搜索失败，也不伪造任何结果。
        retryable_errors = {
            "search_provider_unavailable",
            "search_timeout",
            "search_proxy_error",
            "search_connection_error",
            "search_request_error",
            "search_http_error",
        }
        if not result.ok and result.error_code in retryable_errors and not (domains or item.get("recency") is not None):
            retry = web_search(
                sandbox,
                query,
                limit=int(item.get("limit") or 5),
                recency=None,
                domains=None,
            )
            if retry.ok:
                retry.metadata["retry"] = "transient_provider_failure"
                retry.metadata["initial_error_code"] = result.error_code
                return query, retry
        return query, result

    # 限制为 5 个并发请求，既利用代理连接池，也避免按查询数量无限扩张。
    with ThreadPoolExecutor(max_workers=min(5, max(1, len(unique_searches)))) as executor:
        search_results = list(executor.map(run_search, unique_searches))
    search_prefix = f"{_ref_prefix}search"
    existing_search_numbers = [
        int(key[len(search_prefix):]) for key in stored_pages
        if key.startswith(search_prefix) and key[len(search_prefix):].isdigit()
    ]
    next_ref = max(existing_search_numbers, default=0) + 1
    for query, result in search_results:
        query_results: list[dict[str, Any]] = []
        for hit in result.metadata.get("results", []) if isinstance(result.metadata, Mapping) else []:
            if not isinstance(hit, Mapping):
                continue
            ref_id = f"{search_prefix}{next_ref}"
            next_ref += 1
            normalized_hit = {**dict(hit), "ref_id": ref_id}
            query_results.append(normalized_hit)
            # 变量说明：stored_pages 的索引项 表示该语句创建或更新的目标数据。
            stored_pages[ref_id] = normalized_hit
        output.append({
            "type": "search_query",
            "query": query,
            "ok": result.ok,
            "content": result.content,
            "results": query_results,
            "error_code": result.error_code,
            "provider": result.metadata.get("provider"),
            **({"warning": "来源或时效过滤已放宽；以下结果不保证符合原过滤条件", "fallback": result.metadata["fallback"]}
               if result.metadata.get("fallback") else {}),
            **({key: result.metadata[key] for key in ("status_code", "initial_status_code", "fallback_from", "initial_error_code", "brave_error")
               if result.metadata.get(key) is not None}),
        })
    for kind, items in (("open", open), ("click", click), ("find", find)):
        for item in items or []:
            navigated = reader.execute(kind, item)
            output.append(navigated)
            if navigated.get("ok"):
                source_url = source_url or navigated.get("source_url") or navigated.get("url")
    for item in screenshot or []:
        # 变量说明：result 表示本步骤产生的结果。
        result = web_screenshot(sandbox, url=str(item.get("url") or item.get("ref_id") or ""), full_page=bool(item.get("full_page")))
        output.append({"type": "screenshot", "ok": result.ok, "content": result.content, "metadata": result.metadata})
    for item in finance or []:
        # 变量说明：result 表示本步骤产生的结果。
        result = web_finance(sandbox, ticker=str(item.get("ticker") or item.get("symbol") or ""), type=str(item.get("type") or "equity"))
        output.append({"type": "finance", "ok": result.ok, "content": result.content, "error_code": result.error_code})
    for item in weather or []:
        # 变量说明：result 表示本步骤产生的结果。
        result = web_weather(sandbox, location=str(item.get("location") or item.get("city") or ""), days=int(item.get("days") or 3))
        weather_command = {"type": "weather", "ok": result.ok, "content": result.content, "error_code": result.error_code}
        weather_source = result.metadata.get("source_url") if isinstance(result.metadata, Mapping) else None
        if isinstance(weather_source, str) and weather_source:
            weather_command["source_url"] = weather_source
            source_url = source_url or weather_source
        output.append(weather_command)
    for item in sports or []:
        # 变量说明：result 表示本步骤产生的结果。
        result = web_sports(sandbox, league=str(item.get("league") or ""), date=item.get("date"), team=item.get("team"))
        output.append({"type": "sports", "ok": result.ok, "content": result.content, "error_code": result.error_code})
    for item in time or []:
        # 变量说明：result 表示本步骤产生的结果。
        result = get_current_time(timezone_name=str(item.get("timezone") or item.get("timezone_name") or "") or None)
        output.append({"type": "time", "ok": result.ok, "content": result.content, "error_code": result.error_code})
    if not output:
        return ToolResult("web_run", False, "至少提供一个 search_query、open、click 或 find 命令", error_code="invalid_command")
    serialized_output = json.dumps(output, ensure_ascii=False)
    visible_output = output
    if len(serialized_output) > MAX_WEB_RUN_OUTPUT_CHARS:
        visible_output = []
        for command in output:
            visible = dict(command)
            content = visible.get("content")
            if isinstance(content, str) and len(content) > 1_000:
                visible["content"] = content[:980] + "…[输出已截断]"
            results = visible.get("results")
            if isinstance(results, list) and len(results) > 2:
                compact_results = []
                for result in results[:2]:
                    compact = dict(result)
                    for key, limit in (("title", 300), ("url", 2_000), ("description", 300)):
                        value = compact.get(key)
                        if isinstance(value, str) and len(value) > limit:
                            compact[key] = value[:limit - 1] + "…"
                    compact_results.append(compact)
                visible["results"] = compact_results
                visible["result_count"] = len(results)
            visible["output_truncated"] = True
            visible_output.append(visible)
        serialized_output = json.dumps(visible_output, ensure_ascii=False)
        if len(serialized_output) > MAX_WEB_RUN_OUTPUT_CHARS:
            minimal_output: list[dict[str, Any]] = []
            scalar_keys = (
                "type", "query", "ok", "error_code", "provider", "opened",
                "status_code", "initial_status_code", "ref_id", "url", "fallback_from",
                "initial_error_code", "brave_error",
            )
            for command in visible_output:
                summary = {
                    key: (command[key][:500] + "…" if isinstance(command.get(key), str) and len(command[key]) > 500 else command[key])
                    for key in scalar_keys if key in command
                }
                content = command.get("content")
                if isinstance(content, str) and content:
                    summary["content"] = content[:200] + ("…" if len(content) > 200 else "")
                results = command.get("results")
                if isinstance(results, list) and results:
                    first = dict(results[0])
                    for key, limit in (("title", 200), ("url", 1_000), ("description", 100)):
                        value = first.get(key)
                        if isinstance(value, str) and len(value) > limit:
                            first[key] = value[:limit - 1] + "…"
                    summary["results"] = [first]
                    summary["result_count"] = command.get("result_count", len(results))
                summary["output_truncated"] = True
                minimal_output.append(summary)
            visible_output = minimal_output
            serialized_output = json.dumps(visible_output, ensure_ascii=False)
        if len(serialized_output) > MAX_WEB_RUN_OUTPUT_CHARS:
            visible_output = [
                {"type": command.get("type"), "ok": command.get("ok"),
                 "error_code": command.get("error_code"), "output_truncated": True}
                for command in output
            ]
            serialized_output = json.dumps(visible_output, ensure_ascii=False)
    successful_commands = [item for item in output if item.get("ok") is True]
    if not successful_commands:
        failure_code = next(
            (str(item.get("error_code")) for item in output if item.get("error_code")),
            "tool_execution_failed",
        )
        return ToolResult(
            "web_run",
            False,
            serialized_output,
            error_code=failure_code,
            metadata={"commands": visible_output, "pages": stored_pages, **({"source_url": source_url} if source_url else {})},
        )
    return ToolResult("web_run", True, serialized_output, metadata={"commands": visible_output, "pages": stored_pages, **({"source_url": source_url} if source_url else {})})


# 函数职责：规范化 todos 对应的数据或流程。
# 参数关系：value 表示当前字段或计算值。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def _normalize_todos(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise ValueError("todos 必须是列表")
    if len(value) > MAX_TODOS:
        raise ValueError(f"todos 最多 {MAX_TODOS} 项")
    # 变量说明：normalized 表示当前步骤使用的 normalized 值。
    normalized: list[dict[str, Any]] = []
    # 变量说明：allowed_statuses 表示当前流程使用的 allowed_statuses 集合。
    allowed_statuses = {"pending", "in_progress", "completed", "cancelled"}
    # 变量说明：seen_ids 表示seen 对象标识集合。
    seen_ids: set[str] = set()
    for index, raw in enumerate(value, start=1):
        if not isinstance(raw, Mapping):
            raise ValueError(f"第 {index} 项 todo 必须是对象")
        # 变量说明：content 表示待处理或返回的正文内容。
        content = str(raw.get("content") or "").strip()
        if not content or len(content) > 1_000:
            raise ValueError(f"第 {index} 项 todo 的 content 不能为空且最多 1000 字符")
        # 变量说明：todo_id 表示todo 对象的唯一标识。
        todo_id = str(raw.get("id") or "").strip()
        if not todo_id or len(todo_id) > 120 or todo_id in seen_ids:
            raise ValueError(f"第 {index} 项 todo 必须提供稳定且不重复的 id")
        seen_ids.add(todo_id)
        # 变量说明：status 表示当前对象或运行的状态。
        status = str(raw.get("status") or "pending").strip().lower()
        if status not in allowed_statuses:
            raise ValueError(f"第 {index} 项 todo 的 status 无效")
        # 变量说明：item 表示当前步骤使用的 item 值。
        item = {"id": todo_id, "content": content, "status": status}
        if raw.get("active_form"):
            item["active_form"] = str(raw["active_form"])[:1_000]
        # 变量说明：dependencies 表示当前流程使用的 dependencies 集合。
        dependencies = raw.get("depends_on", raw.get("blockedBy", []))
        if dependencies is None:
            # 变量说明：dependencies 表示当前流程使用的 dependencies 集合。
            dependencies = []
        if not isinstance(dependencies, (list, tuple)):
            raise ValueError(f"第 {index} 项 todo 的 depends_on 必须是数组")
        # 变量说明：item 的索引项 表示该语句创建或更新的目标数据。
        item["depends_on"] = list(dict.fromkeys(
            str(value).strip() for value in dependencies if str(value).strip()
        ))
        # 变量说明：executor_kind 表示当前步骤使用的 executor_kind 值。
        executor_kind = str(raw.get("executor_kind") or raw.get("executor") or "main").strip().lower()
        if executor_kind not in {"main", "subagent", "background"}:
            raise ValueError(f"第 {index} 项 todo 的 executor_kind 无效")
        # 变量说明：item 的索引项 表示该语句创建或更新的目标数据。
        item["executor_kind"] = executor_kind
        # 变量说明：assigned_agent_id 表示assigned_agent 对象的唯一标识。
        assigned_agent_id = str(raw.get("agent_id") or raw.get("assigned_agent_id") or "").strip()
        if executor_kind == "subagent" and not assigned_agent_id:
            raise ValueError(f"第 {index} 项 subagent todo 必须提供 agent_id")
        if assigned_agent_id:
            # 变量说明：item 的索引项 表示该语句创建或更新的目标数据。
            item["agent_id"] = assigned_agent_id
        # 变量说明：workspace_mode 表示当前步骤使用的 workspace_mode 值。
        workspace_mode = str(raw.get("workspace_mode") or "shared").strip().lower()
        if workspace_mode not in {"shared", "worktree"}:
            raise ValueError(f"第 {index} 项 todo 的 workspace_mode 无效")
        # 变量说明：item 的索引项 表示该语句创建或更新的目标数据。
        item["workspace_mode"] = workspace_mode
        normalized.append(item)
    # 变量说明：known_ids 表示known 对象标识集合。
    known_ids = {item["id"] for item in normalized}
    for item in normalized:
        for dependency_id in item["depends_on"]:
            if dependency_id not in known_ids:
                raise ValueError(f"todo {item['id']} 依赖不存在的步骤 {dependency_id}")
            if dependency_id == item["id"]:
                raise ValueError(f"todo {item['id']} 不能依赖自身")
    from src.tasks.graph import validate_dependency_graph

    validate_dependency_graph(normalized)
    return normalized


# 函数职责：完成 todo_write 对应的业务处理。
# 参数关系：_sandbox 表示当前步骤使用的 _sandbox 值；todos 表示当前流程使用的 todos 集合；todo_state 表示当前步骤使用的 todo_state 值。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def todo_write(
    _sandbox: WorkspaceSandbox,
    todos: list[dict[str, Any]],
    *,
    todo_state: list[dict[str, Any]],
) -> ToolResult:
    """Replace the run's structured todo state with validated items."""

    try:
        # 变量说明：normalized 表示当前步骤使用的 normalized 值。
        normalized = _normalize_todos(todos)
    except ValueError as exc:
        return ToolResult("todowrite", False, str(exc), error_code="invalid_todos")
    # 变量说明：changed 表示当前步骤使用的 changed 值。
    changed = todo_state != normalized
    todo_state[:] = normalized
    # 变量说明：active 表示当前步骤使用的 active 值。
    active = next((item["content"] for item in normalized if item["status"] == "in_progress"), "")
    return ToolResult(
        "todowrite",
        True,
        f"已更新 {len(normalized)} 项任务" + (f"；当前进行中：{active}" if active else ""),
        changed=changed,
        metadata={"todos": normalized, "count": len(normalized)},
    )


# 函数职责：完成 ask_question 对应的业务处理。
# 参数关系：_sandbox 表示当前步骤使用的 _sandbox 值；question 表示当前步骤使用的 question 值；context 表示当前步骤使用的 context 值。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def ask_question(
    _sandbox: WorkspaceSandbox,
    question: str,
    *,
    context: str = "",
) -> ToolResult:
    """Ask for clarification without pretending that a same-run reply exists."""

    # 变量说明：prompt 表示当前步骤使用的 prompt 值。
    prompt = str(question or "").strip()
    if not prompt or len(prompt) > 2_000:
        return ToolResult("question", False, "question 不能为空或过长", error_code="invalid_question")
    # 变量说明：details 表示当前流程使用的 details 集合。
    details = str(context or "").strip()[:2_000]
    return ToolResult(
        "question",
        True,
        prompt,
        error_code="question_needed",
        metadata={"needs_user_input": True, "question": prompt, "context": details},
    )


# 函数职责：规范化 delegate_specs 对应的数据或流程。
# 参数关系：task 表示当前步骤使用的 task 值；agent_id 表示智能体标识；tasks 表示当前流程使用的 tasks 集合；step_id 表示step 对象的唯一标识；depends_on 表示当前步骤使用的 depends_on 值。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def normalize_delegate_specs(
    task: object = "",
    agent_id: object = "",
    tasks: object = None,
    step_id: object = "",
    depends_on: object = None,
    model_id: object = "",
    thinking_level: object = "",
    workspace_mode: object = "shared",
) -> tuple[list[dict[str, Any]], str | None, str | None]:
    """Normalize one task or a dependency-aware batch."""

    if tasks is not None:
        if (
            str(task or "").strip()
            or str(agent_id or "").strip()
            or str(model_id or "").strip()
            or str(thinking_level or "").strip()
        ):
            return [], "invalid_task", "批量委派时 task、agent_id、model_id 和 thinking_level 必须写入 tasks 各项"
        if not isinstance(tasks, (list, tuple)) or not tasks:
            return [], "invalid_task", "tasks 必须是非空数组"
        if len(tasks) > MAX_PARALLEL_DELEGATED_TASKS:
            return [], "delegate_parallel_limit", f"单次最多委派 {MAX_PARALLEL_DELEGATED_TASKS} 个子 Agent 任务"
        # 变量说明：normalized 表示当前步骤使用的 normalized 值。
        normalized: list[dict[str, Any]] = []
        for index, item in enumerate(tasks, start=1):
            if not isinstance(item, Mapping):
                return [], "invalid_task", "tasks 中每一项必须是对象"
            # 变量说明：request 表示调用方传入的请求数据。
            request = str(item.get("task") or "").strip()
            # 变量说明：target 表示当前步骤使用的 target 值。
            target = str(item.get("agent_id") or "").strip()
            if not request or len(request) > 8_000:
                return [], "invalid_task", "tasks 中的 task 不能为空或过长"
            if not target or len(target) > 80:
                return [], "invalid_delegate_agent", "tasks 中每一项都必须提供有效的子 Agent ID"
            # 变量说明：external_id 表示external 对象的唯一标识。
            external_id = str(item.get("id") or item.get("step_id") or "").strip()
            # 变量说明：dependencies 表示当前流程使用的 dependencies 集合。
            dependencies = item.get("depends_on", item.get("blockedBy", []))
            if dependencies is None:
                # 变量说明：dependencies 表示当前流程使用的 dependencies 集合。
                dependencies = []
            if not isinstance(dependencies, (list, tuple)):
                return [], "invalid_task_dependencies", "tasks 中的 depends_on 必须是数组"
            # 变量说明：workspace_mode 表示当前步骤使用的 workspace_mode 值。
            workspace_mode = str(item.get("workspace_mode") or "shared").strip().lower()
            if workspace_mode not in {"shared", "worktree"}:
                return [], "invalid_workspace_mode", "workspace_mode 必须是 shared 或 worktree"
            selected_model = str(item.get("model_id") or "").strip()
            selected_thinking = str(item.get("thinking_level") or "").strip().lower()
            if len(selected_model) > 255:
                return [], "invalid_delegate_model", "tasks 中的 model_id 不能超过 255 个字符"
            if selected_thinking and selected_thinking not in {"low", "medium", "high", "xhigh"}:
                return [], "invalid_delegate_thinking_level", "tasks 中的 thinking_level 必须是 low、medium、high 或 xhigh"
            normalized_item = {
                "id": external_id or f"generated-{index}",
                "generated_id": not bool(external_id),
                "task": request,
                "agent_id": target,
                "depends_on": list(dict.fromkeys(
                    str(value).strip() for value in dependencies if str(value).strip()
                )),
                "workspace_mode": workspace_mode,
            }
            if selected_model:
                normalized_item["model_id"] = selected_model
            if selected_thinking:
                normalized_item["thinking_level"] = selected_thinking
            normalized.append(normalized_item)
    else:
        # 变量说明：request 表示调用方传入的请求数据。
        request = str(task or "").strip()
        if not request or len(request) > 8_000:
            return [], "invalid_task", "task 不能为空或过长"
        # 变量说明：target 表示当前步骤使用的 target 值。
        target = str(agent_id or "").strip()
        if not target or len(target) > 80:
            return [], "invalid_delegate_agent", "必须提供有效的子 Agent ID"
        # 变量说明：dependencies 表示当前流程使用的 dependencies 集合。
        dependencies = [] if depends_on is None else depends_on
        if not isinstance(dependencies, (list, tuple)):
            return [], "invalid_task_dependencies", "depends_on 必须是数组"
        # 变量说明：external_id 表示external 对象的唯一标识。
        external_id = str(step_id or "").strip()
        selected_model = str(model_id or "").strip()
        selected_thinking = str(thinking_level or "").strip().lower()
        selected_workspace = str(workspace_mode or "shared").strip().lower()
        if selected_workspace not in {"shared", "worktree"}:
            return [], "invalid_workspace_mode", "workspace_mode 必须是 shared 或 worktree"
        if len(selected_model) > 255:
            return [], "invalid_delegate_model", "model_id 不能超过 255 个字符"
        if selected_thinking and selected_thinking not in {"low", "medium", "high", "xhigh"}:
            return [], "invalid_delegate_thinking_level", "thinking_level 必须是 low、medium、high 或 xhigh"
        # 变量说明：normalized 表示当前步骤使用的 normalized 值。
        normalized_item = {
            "id": external_id or "generated-1",
            "generated_id": not bool(external_id),
            "task": request,
            "agent_id": target,
            "depends_on": [str(value).strip() for value in dependencies if str(value).strip()],
                "workspace_mode": selected_workspace,
        }
        if selected_model:
            normalized_item["model_id"] = selected_model
        if selected_thinking:
            normalized_item["thinking_level"] = selected_thinking
        normalized = [normalized_item]
    # 变量说明：graph_rows 表示当前流程使用的 graph_rows 集合。
    graph_rows = [
        {"id": item["id"], "depends_on": item["depends_on"]}
        for item in normalized
    ]
    try:
        from src.tasks.graph import validate_dependency_graph

        validate_dependency_graph(graph_rows)
    except ValueError as exc:
        return [], "invalid_task_dependencies", str(exc)
    return normalized, None, None


# 函数职责：规范化 delegate_requests 对应的数据或流程。
# 参数关系：task 表示当前步骤使用的 task 值；agent_id 表示智能体标识；tasks 表示当前流程使用的 tasks 集合。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def normalize_delegate_requests(
    task: object = "",
    agent_id: object = "",
    tasks: object = None,
    model_id: object = "",
    thinking_level: object = "",
) -> tuple[list[tuple[str, str]], str | None, str | None]:
    """Compatibility projection of normalized delegation specifications."""

    # 变量说明：specs 表示当前流程使用的 specs 集合；error_code 表示当前步骤使用的 error_code 值；error 表示当前捕获或准备上报的错误。
    specs, error_code, error = normalize_delegate_specs(
        task, agent_id, tasks, model_id=model_id, thinking_level=thinking_level
    )
    return [(str(item["task"]), str(item["agent_id"])) for item in specs], error_code, error


# 函数职责：完成 invalid_delegate_result 对应的业务处理。
# 参数关系：code 表示当前步骤使用的 code 值；message 表示当前消息。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def _invalid_delegate_result(code: str, message: str) -> ToolResult:
    return ToolResult("task", False, message, error_code=code)


# 函数职责：完成 delegate_task 对应的业务处理。
# 参数关系：_sandbox 表示当前步骤使用的 _sandbox 值；task 表示当前步骤使用的 task 值；agent_id 表示智能体标识；tasks 表示当前流程使用的 tasks 集合；step_id 表示step 对象的唯一标识；depends_on 表示当前步骤使用的 depends_on 值；delegate 表示当前步骤使用的 delegate 值。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def delegate_task(
    _sandbox: WorkspaceSandbox,
    task: str = "",
    *,
    agent_id: str = "",
    tasks: object = None,
    step_id: str = "",
    depends_on: object = None,
    model_id: str = "",
    thinking_level: str = "",
    workspace_mode: str = "shared",
    delegate: Callable[..., ToolResult | Awaitable[ToolResult]] | None = None,
) -> ToolResult:
    """Synchronous compatibility path for a real, injected task delegate.

    Runtime executions use :func:`delegate_task_async` so a child Agent can
    make asynchronous model calls without blocking the parent event loop.
    This synchronous entry point remains useful for direct tool tests and
    never tries to start a nested event loop.
    """

    # 变量说明：requests 表示当前流程使用的 requests 集合；error_code 表示当前步骤使用的 error_code 值；error 表示当前捕获或准备上报的错误。
    specs, error_code, error = normalize_delegate_specs(
        task, agent_id, tasks, step_id, depends_on, model_id, thinking_level, workspace_mode
    )
    if error_code and error:
        return _invalid_delegate_result(error_code, error)
    if delegate is None:
        return ToolResult(
            "task",
            False,
            "当前没有已接入的子 Agent 委派运行时；未创建或假称已执行子任务。",
            error_code="delegated_task_unavailable",
        )
    try:
        if len(specs) > 1:
            return ToolResult(
                "task",
                False,
                "批量子 Agent 委派只能在运行时异步通道中执行",
                error_code="async_delegate_requires_runtime",
            )
        # 变量说明：request 表示调用方传入的请求数据；target 表示当前步骤使用的 target 值。
        spec = specs[0]
        # 变量说明：result 表示本步骤产生的结果。
        result = _call_task_delegate(
            delegate,
            str(spec["task"]),
            str(spec["agent_id"]),
            call_id=None,
            model_id=spec.get("model_id"),
            thinking_level=spec.get("thinking_level"),
        )
        if inspect.isawaitable(result):
            # Do not create a second event loop from a synchronous tool call.
            # Closing the coroutine prevents an unawaited-coroutine warning
            # while making the actual execution contract explicit.
            # 变量说明：close 表示当前步骤使用的 close 值。
            close = getattr(result, "close", None)
            if callable(close):
                close()
            return ToolResult(
                "task",
                False,
                "子 Agent 委派只能在运行时异步通道中执行",
                error_code="async_delegate_requires_runtime",
            )
        if isinstance(result, ToolResult):
            return result
        return ToolResult("task", False, "子 Agent 委派器返回了无效结果", error_code="delegate_error")
    except Exception as exc:
        return ToolResult("task", False, f"子 Agent 委派失败: {type(exc).__name__}", error_code="delegate_error")


# 函数职责：完成 call_task_delegate 对应的业务处理。
# 参数关系：delegate 表示当前步骤使用的 delegate 值；task 表示当前步骤使用的 task 值；agent_id 表示智能体标识；call_id 表示call 对象的唯一标识；plan_step_external_id 表示plan_step_external 对象的唯一标识；graph_call_id 表示graph_call 对象的唯一标识。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def _call_task_delegate(
    delegate: Callable[..., ToolResult | Awaitable[ToolResult]],
    task: str,
    agent_id: str,
    *,
    call_id: str | None,
    plan_step_external_id: str | None = None,
    graph_call_id: str | None = None,
    model_id: str | None = None,
    thinking_level: str | None = None,
) -> ToolResult | Awaitable[ToolResult]:
    """Call modern delegates while preserving one-argument test adapters.

    PGAgent owns the production delegate signature.  The fallback only keeps
    older direct unit tests/extensions working; it is intentionally limited to
    a signature mismatch before a delegate is entered.
    """

    try:
        # 变量说明：signature 表示当前步骤使用的 signature 值。
        signature = inspect.signature(delegate)
    except (TypeError, ValueError):
        # 变量说明：signature 表示当前步骤使用的 signature 值。
        signature = None
    if signature is not None:
        # 变量说明：parameters 表示当前流程使用的 parameters 集合。
        parameters = signature.parameters.values()
        # 变量说明：accepts_keywords 表示当前流程使用的 accepts_keywords 集合。
        accepts_keywords = any(parameter.kind is inspect.Parameter.VAR_KEYWORD for parameter in parameters)
        # 变量说明：names 表示当前流程使用的 names 集合。
        names = signature.parameters
        if model_id and not (accepts_keywords or "model_id" in names):
            return ToolResult(
                "task", False, "当前子 Agent 委派器不支持模型分配", error_code="delegate_allocation_unsupported"
            )
        if thinking_level and not (accepts_keywords or "thinking_level" in names):
            return ToolResult(
                "task", False, "当前子 Agent 委派器不支持思考强度分配", error_code="delegate_allocation_unsupported"
            )
        if (
            accepts_keywords
            or "agent_id" in names
            or "call_id" in names
            or "plan_step_external_id" in names
            or "graph_call_id" in names
            or "model_id" in names
            or "thinking_level" in names
        ):
            # 变量说明：keyword_arguments 表示当前流程使用的 keyword_arguments 集合。
            keyword_arguments: dict[str, str | None] = {}
            if accepts_keywords or "agent_id" in names:
                keyword_arguments["agent_id"] = agent_id
            if accepts_keywords or "call_id" in names:
                # 变量说明：keyword_arguments 的索引项 表示该语句创建或更新的目标数据。
                keyword_arguments["call_id"] = call_id
            if accepts_keywords or "plan_step_external_id" in names:
                # 变量说明：keyword_arguments 的索引项 表示该语句创建或更新的目标数据。
                keyword_arguments["plan_step_external_id"] = plan_step_external_id
            if accepts_keywords or "graph_call_id" in names:
                # 变量说明：keyword_arguments 的索引项 表示该语句创建或更新的目标数据。
                keyword_arguments["graph_call_id"] = graph_call_id
            if accepts_keywords or "model_id" in names:
                keyword_arguments["model_id"] = model_id
            if accepts_keywords or "thinking_level" in names:
                keyword_arguments["thinking_level"] = thinking_level
            return delegate(task, **keyword_arguments)
    return delegate(task)


# 函数职责：准备 delegate_graph 对应的数据或流程。
# 参数关系：delegate 表示当前步骤使用的 delegate 值；specs 表示当前流程使用的 specs 集合；call_id 表示call 对象的唯一标识。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def _prepare_delegate_graph(delegate: Any, specs: list[dict[str, Any]], call_id: str | None) -> Any:
    # 变量说明：prepare_graph 表示当前步骤使用的 prepare_graph 值。
    prepare_graph = getattr(delegate, "prepare_graph", None)
    if not callable(prepare_graph):
        return None
    try:
        # 变量说明：signature 表示当前步骤使用的 signature 值。
        signature = inspect.signature(prepare_graph)
    except (TypeError, ValueError):
        # 变量说明：signature 表示当前步骤使用的 signature 值。
        signature = None
    if signature is not None and (
        "call_id" in signature.parameters
        or any(parameter.kind is inspect.Parameter.VAR_KEYWORD for parameter in signature.parameters.values())
    ):
        return prepare_graph(specs, call_id=call_id)
    return prepare_graph(specs)


# 函数职责：完成 block_delegate_step 对应的业务处理。
# 参数关系：delegate 表示当前步骤使用的 delegate 值；external_id 表示external 对象的唯一标识；reason 表示当前步骤使用的 reason 值；graph_call_id 表示graph_call 对象的唯一标识。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def _block_delegate_step(
    delegate: Any,
    external_id: str,
    reason: str,
    graph_call_id: str | None,
) -> Any:
    # 变量说明：block_step 表示当前步骤使用的 block_step 值。
    block_step = getattr(delegate, "block_step", None)
    if not callable(block_step):
        return None
    try:
        # 变量说明：signature 表示当前步骤使用的 signature 值。
        signature = inspect.signature(block_step)
    except (TypeError, ValueError):
        # 变量说明：signature 表示当前步骤使用的 signature 值。
        signature = None
    if signature is not None and (
        "graph_call_id" in signature.parameters
        or any(parameter.kind is inspect.Parameter.VAR_KEYWORD for parameter in signature.parameters.values())
    ):
        return block_step(external_id, reason, graph_call_id=graph_call_id)
    return block_step(external_id, reason)


# 函数职责：异步完成 delegate_task_async 对应的业务处理。
# 参数关系：_sandbox 表示当前步骤使用的 _sandbox 值；task 表示当前步骤使用的 task 值；agent_id 表示智能体标识；tasks 表示当前流程使用的 tasks 集合；step_id 表示step 对象的唯一标识；depends_on 表示当前步骤使用的 depends_on 值；delegate 表示当前步骤使用的 delegate 值；call_id 表示call 对象的唯一标识。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
async def delegate_task_async(
    _sandbox: WorkspaceSandbox,
    task: str = "",
    *,
    agent_id: str = "",
    tasks: object = None,
    step_id: str = "",
    depends_on: object = None,
    model_id: str = "",
    thinking_level: str = "",
    workspace_mode: str = "shared",
    delegate: Callable[..., ToolResult | Awaitable[ToolResult]] | None = None,
    call_id: str | None = None,
) -> ToolResult:
    """Run one or several injected child-Agent delegates concurrently.

    Validation is shared with the synchronous tool.  A failed delegate is
    converted into a normal tool result so the parent loop's existing repeat
    and no-progress guards remain responsible for recovery.
    """

    # 变量说明：specs 表示当前流程使用的 specs 集合；error_code 表示当前步骤使用的 error_code 值；error 表示当前捕获或准备上报的错误。
    specs, error_code, error = normalize_delegate_specs(
        task, agent_id, tasks, step_id, depends_on, model_id, thinking_level, workspace_mode
    )
    if error_code and error:
        return _invalid_delegate_result(error_code, error)
    if delegate is None:
        return ToolResult(
            "task",
            False,
            "当前没有已接入的子 Agent 委派运行时；未创建或假称已执行子任务。",
            error_code="delegated_task_unavailable",
        )
    try:
        for index, spec in enumerate(specs, start=1):
            if spec.pop("generated_id", False):
                # 变量说明：spec 的索引项 表示该语句创建或更新的目标数据。
                spec["id"] = f"delegate-{call_id or 'call'}-{index}"[:120]
                # 变量说明：spec 的索引项 表示该语句创建或更新的目标数据。
                spec["link_existing"] = True
        # 变量说明：prepared 表示当前步骤使用的 prepared 值。
        prepared = _prepare_delegate_graph(delegate, specs, call_id)
        if inspect.isawaitable(prepared):
            await prepared

        # 函数职责：异步完成 invoke 对应的业务处理。
        # 参数关系：index 表示当前元素的位置索引；spec 表示当前步骤使用的 spec 值。
        # 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
        async def invoke(index: int, spec: dict[str, Any]) -> ToolResult:
            # 变量说明：child_call_id 表示child_call 对象的唯一标识。
            child_call_id = f"{call_id}:{spec['id']}" if call_id and len(specs) > 1 else call_id
            # 变量说明：result 表示本步骤产生的结果。
            result = _call_task_delegate(
                delegate,
                str(spec["task"]),
                str(spec["agent_id"]),
                call_id=child_call_id,
                plan_step_external_id=str(spec["id"]),
                graph_call_id=call_id,
                model_id=spec.get("model_id"),
                thinking_level=spec.get("thinking_level"),
            )
            if inspect.isawaitable(result):
                # 变量说明：result 表示本步骤产生的结果。
                result = await result
            if isinstance(result, ToolResult):
                return result
            return ToolResult("task", False, "子 Agent 委派器返回了无效结果", error_code="delegate_error")

        # 变量说明：results_by_id 表示results_by 对象的唯一标识。
        results_by_id: dict[str, ToolResult] = {}
        # 变量说明：pending 表示当前步骤使用的 pending 值。
        pending = {str(spec["id"]): spec for spec in specs}
        # 变量说明：execution_waves 表示当前流程使用的 execution_waves 集合。
        execution_waves: list[list[str]] = []
        while pending:
            # 变量说明：ready 表示当前步骤使用的 ready 值。
            ready = [
                spec for spec in pending.values()
                if all(
                    dependency_id in results_by_id and results_by_id[dependency_id].ok
                    for dependency_id in spec["depends_on"]
                )
            ]
            if not ready:
                for spec in pending.values():
                    # 变量说明：waiting_dependencies 表示当前流程使用的 waiting_dependencies 集合。
                    waiting_dependencies = [
                        dependency_id for dependency_id in spec["depends_on"]
                        if dependency_id in results_by_id
                        and results_by_id[dependency_id].metadata.get("delegated_child_awaiting_approval")
                    ]
                    # 变量说明：failed_dependencies 表示当前流程使用的 failed_dependencies 集合。
                    failed_dependencies = [
                        dependency_id for dependency_id in spec["depends_on"]
                        if dependency_id in results_by_id
                        and not results_by_id[dependency_id].ok
                        and dependency_id not in waiting_dependencies
                    ]
                    if waiting_dependencies and not failed_dependencies:
                        # 变量说明：waiting_event 表示当前步骤使用的 waiting_event 值。
                        waiting_event = any(
                            results_by_id[dependency_id].metadata.get("delegated_child_waiting_event")
                            for dependency_id in waiting_dependencies
                        )
                        # 变量说明：results_by_id 的索引项 表示该语句创建或更新的目标数据。
                        results_by_id[str(spec["id"])] = ToolResult(
                            "task",
                            False,
                            f"prerequisite task is waiting: {', '.join(waiting_dependencies)}",
                            error_code="delegate_dependency_waiting",
                            metadata={
                                "plan_step_external_id": str(spec["id"]),
                                "blocked_by": waiting_dependencies,
                                "delegated_child_awaiting_approval": True,
                                "delegated_child_waiting_event": waiting_event,
                            },
                        )
                        continue
                    # 变量说明：results_by_id 的索引项 表示该语句创建或更新的目标数据。
                    results_by_id[str(spec["id"])] = ToolResult(
                        "task",
                        False,
                        f"prerequisite task failed: {', '.join(failed_dependencies)}",
                        error_code="delegate_dependency_failed",
                        metadata={"plan_step_external_id": str(spec["id"]), "blocked_by": failed_dependencies},
                    )
                    # 变量说明：blocked 表示当前步骤使用的 blocked 值。
                    blocked = _block_delegate_step(
                        delegate,
                        str(spec["id"]),
                        f"prerequisite task failed: {', '.join(failed_dependencies)}",
                        call_id,
                    )
                    if blocked is not None:
                        if inspect.isawaitable(blocked):
                            await blocked
                pending.clear()
                break
            execution_waves.append([str(spec["id"]) for spec in ready])
            # 变量说明：wave_results 表示当前流程使用的 wave_results 集合。
            wave_results = await asyncio.gather(
                *(invoke(index, spec) for index, spec in enumerate(ready)),
                return_exceptions=True,
            )
            for spec, result in zip(ready, wave_results, strict=True):
                if isinstance(result, ToolResult):
                    # 变量说明：stored_result 表示当前步骤使用的 stored_result 值。
                    stored_result = result
                else:
                    # 变量说明：stored_result 表示当前步骤使用的 stored_result 值。
                    stored_result = ToolResult(
                        "task", False, f"子 Agent 委派失败: {type(result).__name__}", error_code="delegate_error"
                    )
                # 变量说明：results_by_id 的索引项 表示该语句创建或更新的目标数据。
                results_by_id[str(spec["id"])] = stored_result
                if not stored_result.ok and not stored_result.metadata.get("delegated_child_awaiting_approval"):
                    # 变量说明：blocked 表示当前步骤使用的 blocked 值。
                    blocked = _block_delegate_step(
                        delegate,
                        str(spec["id"]),
                        stored_result.error_code or stored_result.content or "delegated task failed",
                        call_id,
                    )
                    if blocked is not None:
                        if inspect.isawaitable(blocked):
                            await blocked
                pending.pop(str(spec["id"]), None)
        # 变量说明：normalized_results 表示当前流程使用的 normalized_results 集合。
        normalized_results = [results_by_id[str(spec["id"])] for spec in specs]
        if len(normalized_results) == 1:
            return normalized_results[0]

        # 变量说明：children 表示当前步骤使用的 children 值。
        children: list[dict[str, Any]] = []
        for result in normalized_results:
            try:
                # 变量说明：child_payload 表示当前步骤使用的 child_payload 值。
                child_payload = json.loads(result.content)
            except (TypeError, json.JSONDecodeError):
                # 变量说明：child_payload 表示当前步骤使用的 child_payload 值。
                child_payload = result.to_dict()
            # 变量说明：child 表示当前步骤使用的 child 值。
            child = child_payload if isinstance(child_payload, dict) else result.to_dict()
            child.setdefault("ok", result.ok)
            child.setdefault("error_code", result.error_code)
            children.append(child)
        # 变量说明：waiting 表示当前步骤使用的 waiting 值。
        waiting = any(
            bool(result.metadata.get("delegated_child_awaiting_approval"))
            for result in normalized_results
        )
        # 变量说明：waiting_event 表示当前步骤使用的 waiting_event 值。
        waiting_event = any(
            bool(result.metadata.get("delegated_child_waiting_event"))
            for result in normalized_results
        )
        # 变量说明：succeeded 表示当前步骤使用的 succeeded 值。
        succeeded = sum(1 for result in normalized_results if result.ok)
        if waiting:
            # 变量说明：aggregate_status 表示当前流程使用的 aggregate_status 集合。
            aggregate_status = "waiting_background" if waiting_event else "awaiting_approval"
            # 变量说明：aggregate_error_code 表示当前步骤使用的 aggregate_error_code 值。
            aggregate_error_code = "delegate_child_waiting_event" if waiting_event else "delegate_child_awaiting_approval"
        elif succeeded == len(normalized_results):
            # 变量说明：aggregate_status 表示当前流程使用的 aggregate_status 集合。
            aggregate_status = "completed"
            # 变量说明：aggregate_error_code 表示当前步骤使用的 aggregate_error_code 值。
            aggregate_error_code = None
        elif succeeded:
            # 变量说明：aggregate_status 表示当前流程使用的 aggregate_status 集合。
            aggregate_status = "partial_failure"
            # 变量说明：aggregate_error_code 表示当前步骤使用的 aggregate_error_code 值。
            aggregate_error_code = "delegate_partial_failure"
        else:
            # 变量说明：aggregate_status 表示当前流程使用的 aggregate_status 集合。
            aggregate_status = "failed"
            # 变量说明：aggregate_error_code 表示当前步骤使用的 aggregate_error_code 值。
            aggregate_error_code = "delegate_batch_failed"
        # 变量说明：aggregate 表示当前步骤使用的 aggregate 值。
        aggregate = {
            "status": aggregate_status,
            "parallel": any(len(wave) > 1 for wave in execution_waves),
            "execution_waves": execution_waves,
            "child_count": len(children),
            "completed_count": succeeded,
            "failed_count": len(children) - succeeded,
            "children": children,
        }
        return ToolResult(
            "task",
            all(result.ok for result in normalized_results),
            json.dumps(aggregate, ensure_ascii=False, separators=(",", ":")),
            changed=any(result.changed for result in normalized_results),
            error_code=aggregate_error_code,
            metadata={
                "status": aggregate["status"],
                "parallel": aggregate["parallel"],
                "execution_waves": execution_waves,
                "child_count": len(children),
                "children": [dict(result.metadata) for result in normalized_results],
                "delegated_child_awaiting_approval": waiting,
                "delegated_child_waiting_event": waiting_event,
            },
        )
    except Exception as exc:
        return ToolResult("task", False, f"子 Agent 委派失败: {type(exc).__name__}", error_code="delegate_error")



# 函数职责：读取 current_time 对应的数据或流程。
# 参数关系：timezone_name 表示当前步骤使用的 timezone_name 值。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def get_current_time(*, timezone_name: str | None = None) -> ToolResult:
    # 变量说明：requested 表示当前步骤使用的 requested 值。
    requested = str(timezone_name or "").strip()
    if requested:
        try:
            # 变量说明：now 表示当前时间。
            now = datetime.now(ZoneInfo(requested))
        except ZoneInfoNotFoundError:
            return ToolResult("get_current_time", False, f"未知时区: {requested}", error_code="invalid_timezone")
    else:
        # 变量说明：now 表示当前时间。
        now = datetime.now().astimezone()
    return ToolResult(
        "get_current_time",
        True,
        now.isoformat(),
        metadata={"timezone": timezone_name or str(now.tzinfo)},
    )


# 函数职责：执行 readonly_git 对应的数据或流程。
# 参数关系：sandbox 表示当前步骤使用的 sandbox 值；arguments 表示当前流程使用的 arguments 集合；tool_name 表示当前步骤使用的 tool_name 值；max_chars 表示当前流程使用的 max_chars 集合。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def _run_readonly_git(
    sandbox: WorkspaceSandbox,
    arguments: list[str],
    *,
    tool_name: str,
    max_chars: int,
) -> ToolResult:
    """Run a bounded, read-only git query inside the workspace.

    These helpers deliberately do not reuse ``run_command``: git inspection is
    safe in smart/ask mode and should not create an approval interruption, while
    still using the same process, timeout and output-size boundaries.
    """

    # 变量说明：limit 表示当前步骤使用的 limit 值。
    limit = min(max(int(max_chars), 256), 100_000)
    try:
        # 变量说明：check 表示当前步骤使用的 check 值。
        check = subprocess.run(
            ["git", "rev-parse", "--is-inside-work-tree"],
            cwd=sandbox.root,
            capture_output=True,
            check=False,
            timeout=10,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        if check.returncode != 0 or check.stdout.strip().lower() != "true":
            return ToolResult(tool_name, False, "当前工作区不是 Git 仓库", error_code="not_git_repository")
        # 变量说明：result 表示本步骤产生的结果。
        result = subprocess.run(
            arguments,
            cwd=sandbox.root,
            capture_output=True,
            check=False,
            timeout=20,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        # 变量说明：output 表示当前步骤使用的 output 值。
        output = (result.stdout or "") + ("\n" + result.stderr if result.stderr else "")
        # 变量说明：output 表示当前步骤使用的 output 值。
        output = output.strip()
        # 变量说明：truncated 表示当前步骤使用的 truncated 值。
        truncated = len(output) > limit
        if truncated:
            # 变量说明：output 表示当前步骤使用的 output 值。
            output = output[:limit]
        return ToolResult(
            tool_name,
            result.returncode == 0,
            output or f"git 命令结束，退出码 {result.returncode}",
            error_code=None if result.returncode == 0 else "git_error",
            metadata={"truncated": truncated, "read_only": True, "exit_code": result.returncode},
        )
    except subprocess.TimeoutExpired:
        return ToolResult(tool_name, False, "Git 查询超时", error_code="timeout")
    except OSError as exc:
        return ToolResult(tool_name, False, f"Git 不可用: {exc}", error_code="git_unavailable")


# 函数职责：完成 git_status 对应的业务处理。
# 参数关系：sandbox 表示当前步骤使用的 sandbox 值；include_untracked 表示当前步骤使用的 include_untracked 值；max_chars 表示当前流程使用的 max_chars 集合。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def git_status(
    sandbox: WorkspaceSandbox,
    *,
    include_untracked: bool = True,
    max_chars: int = 20_000,
) -> ToolResult:
    """Return branch and working-tree status without changing the repository."""

    # 变量说明：arguments 表示当前流程使用的 arguments 集合。
    arguments = ["git", "--no-optional-locks", "status", "--short", "--branch"]
    if not include_untracked:
        arguments.extend(["--untracked-files=no"])
    return _run_readonly_git(sandbox, arguments, tool_name="git_status", max_chars=max_chars)


# 函数职责：完成 git_diff 对应的业务处理。
# 参数关系：sandbox 表示当前步骤使用的 sandbox 值；staged 表示当前步骤使用的 staged 值；path 表示当前文件或目录路径；max_chars 表示当前流程使用的 max_chars 集合。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def git_diff(
    sandbox: WorkspaceSandbox,
    *,
    staged: bool = False,
    path: str | None = None,
    max_chars: int = 40_000,
) -> ToolResult:
    """Return a bounded diff, optionally limited to one workspace-relative path."""

    # 变量说明：arguments 表示当前流程使用的 arguments 集合。
    arguments = ["git", "--no-pager", "diff", "--no-ext-diff", "--unified=3"]
    if staged:
        arguments.append("--cached")
    if path:
        try:
            # 变量说明：resolved 表示当前步骤使用的 resolved 值。
            resolved = sandbox.resolve(path, must_exist=True)
            arguments.extend(["--", sandbox.relative(resolved)])
        except (SandboxViolation, FileNotFoundError, OSError) as exc:
            return ToolResult("git_diff", False, str(exc), error_code="path_error")
    return _run_readonly_git(sandbox, arguments, tool_name="git_diff", max_chars=max_chars)


# 函数职责：完成 file_info 对应的业务处理。
# 参数关系：sandbox 表示当前步骤使用的 sandbox 值；path 表示当前文件或目录路径。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def file_info(
    sandbox: WorkspaceSandbox,
    path: str = ".",
) -> ToolResult:
    """Return safe metadata for one workspace-relative file or directory."""

    try:
        # 变量说明：target 表示当前步骤使用的 target 值。
        target = sandbox.resolve(path, must_exist=True)
        # 变量说明：stat 表示当前步骤使用的 stat 值。
        stat = target.stat()
        # 变量说明：kind 表示当前步骤使用的 kind 值。
        kind = "directory" if target.is_dir() else "file" if target.is_file() else "other"
        # 变量说明：metadata 表示当前步骤使用的 metadata 值。
        metadata: dict[str, Any] = {
            "path": sandbox.relative(target),
            "kind": kind,
            "size_bytes": stat.st_size,
            "modified_at": datetime.fromtimestamp(stat.st_mtime).astimezone().isoformat(),
            "read_only": True,
        }
        if target.is_dir():
            try:
                # 变量说明：metadata 的索引项 表示该语句创建或更新的目标数据。
                metadata["children"] = sum(1 for _ in target.iterdir())
            except OSError:
                # 变量说明：metadata 的索引项 表示该语句创建或更新的目标数据。
                metadata["children"] = None
        return ToolResult("file_info", True, f"{metadata['path']} ({kind}, {stat.st_size} bytes)", metadata=metadata)
    except (SandboxViolation, FileNotFoundError, OSError) as exc:
        return ToolResult("file_info", False, str(exc), error_code="path_error")


# 函数职责：完成 write_file 对应的业务处理。
# 参数关系：sandbox 表示当前步骤使用的 sandbox 值；path 表示当前文件或目录路径；content 表示待处理或返回的正文内容；approved 表示当前步骤使用的 approved 值；overwrite 表示当前步骤使用的 overwrite 值。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def write_file(
    sandbox: WorkspaceSandbox,
    path: str,
    content: str,
    *,
    approved: bool = False,
    overwrite: bool = True,
) -> ToolResult:
    # 变量说明：arguments 表示当前流程使用的 arguments 集合。
    arguments = {"path": path, "content": content, "overwrite": overwrite}
    if not approved:
        return _approval("write_file", arguments, f"写入文件需要批准: {path}")
    try:
        # 变量说明：target 表示当前步骤使用的 target 值。
        target = sandbox.resolve(path)
        if target.exists() and not overwrite:
            return ToolResult("write_file", False, "文件已存在且禁止覆盖", error_code="already_exists")
        target.parent.mkdir(parents=True, exist_ok=True)
        # 变量说明：previous 表示当前流程使用的 previous 集合。
        previous = target.read_text(encoding="utf-8", errors="replace") if target.exists() else None
        target.write_text(content, encoding="utf-8")
        # 变量说明：changed 表示当前步骤使用的 changed 值。
        changed = previous != content
        from src.coding.changes import build_file_change
        relative = sandbox.relative(target)
        change = build_file_change(relative, "update" if previous is not None else "add", previous, content)
        return ToolResult(
            "write_file",
            True,
            f"已写入 {relative} ({len(content.encode('utf-8'))} bytes)",
            changed=changed,
            metadata={
                "path": relative,
                "operation": "update" if previous is not None else "add",
                "change_set": {"status": "applied", "source": "file_tool", "file_count": 1, "files": [change]},
            },
        )
    except (SandboxViolation, OSError) as exc:
        return ToolResult("write_file", False, str(exc), error_code="write_error")


# 函数职责：删除 file 对应的数据或流程。
# 参数关系：sandbox 表示当前步骤使用的 sandbox 值；path 表示当前文件或目录路径；approved 表示当前步骤使用的 approved 值。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def delete_file(
    sandbox: WorkspaceSandbox,
    path: str,
    *,
    approved: bool = False,
) -> ToolResult:
    """Delete one workspace file without exposing a general shell primitive."""

    # 变量说明：arguments 表示当前流程使用的 arguments 集合。
    arguments = {"path": path}
    try:
        # 变量说明：target 表示当前步骤使用的 target 值。
        target = sandbox.resolve(path)
        # 变量说明：cursor 表示当前步骤使用的 cursor 值。
        cursor = sandbox.root
        for part in Path(path).parts:
            if part in {"", "."}:
                continue
            if part == "..":
                return ToolResult("delete", False, "删除路径不允许包含 ..", error_code="path_error")
            # 变量说明：cursor 表示当前步骤使用的 cursor 值。
            cursor = cursor / part
            # 变量说明：is_junction 表示表示是否满足 junction 条件的布尔标记。
            is_junction = getattr(cursor, "is_junction", lambda: False)
            if cursor.is_symlink() or is_junction():
                return ToolResult(
                    "delete",
                    False,
                    "不允许删除经过符号链接或目录联接的路径",
                    error_code="path_link_not_allowed",
                )
        # 变量说明：relative_path 表示relative_path 对应的文件系统位置。
        relative_path = sandbox.relative(target)
        if not target.exists():
            return ToolResult(
                "delete",
                True,
                f"文件不存在，无需删除: {relative_path}",
                metadata={"path": relative_path, "kind": "missing"},
            )
        if target.is_dir():
            return ToolResult(
                "delete",
                False,
                "delete 只允许删除单个文件，不支持目录或递归删除",
                error_code="directory_not_allowed",
            )
        if not target.is_file():
            return ToolResult(
                "delete",
                False,
                "目标不是普通文件",
                error_code="file_not_allowed",
            )
        if not approved:
            return _approval("delete", arguments, f"删除文件需要批准: {relative_path}")
        # 变量说明：deleted_path 表示deleted_path 对应的文件系统位置。
        # 在安全删除前读取快照，供历史变更面板展示被删除内容。
        try:
            previous_bytes = target.read_bytes()
        except OSError:
            previous_bytes = None
        deleted_path = _atomic_delete_regular_file(sandbox, path)
        if deleted_path is None:
            return ToolResult(
                "delete",
                True,
                f"文件不存在，无需删除: {relative_path}",
                metadata={"path": relative_path, "kind": "missing"},
            )
        from src.coding.changes import build_file_change
        change = build_file_change(deleted_path, "delete", previous_bytes, None)
        return ToolResult(
            "delete",
            True,
            f"已删除 {deleted_path}",
            changed=True,
            metadata={
                "path": deleted_path,
                "kind": "file",
                "change_set": {"status": "applied", "source": "file_tool", "file_count": 1, "files": [change]},
            },
        )
    except (SandboxViolation, OSError, ValueError) as exc:
        return ToolResult("delete", False, str(exc), error_code="path_error")


# 函数职责：完成 atomic_delete_regular_file 对应的业务处理。
# 参数关系：sandbox 表示当前步骤使用的 sandbox 值；path 表示当前文件或目录路径。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def _atomic_delete_regular_file(sandbox: WorkspaceSandbox, path: str) -> str | None:
    """Delete the same filesystem object that is boundary-checked.

    POSIX pins every parent with directory descriptors and ``O_NOFOLLOW``.
    Windows opens the final object as a handle, validates its kernel-resolved
    path and attributes, then marks that exact handle for deletion.  This
    avoids a check-then-unlink race through swapped symlinks or junctions.
    """

    return _atomic_delete_windows(sandbox, path) if os.name == "nt" else _atomic_delete_posix(sandbox, path)


# 函数职责：完成 atomic_delete_posix 对应的业务处理。
# 参数关系：sandbox 表示当前步骤使用的 sandbox 值；path 表示当前文件或目录路径。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def _atomic_delete_posix(sandbox: WorkspaceSandbox, path: str) -> str | None:
    import stat

    # 变量说明：relative 表示当前步骤使用的 relative 值。
    relative = Path(path)
    # 变量说明：parts 表示当前流程使用的 parts 集合。
    parts = [part for part in relative.parts if part not in {"", "."}]
    if not parts or any(part == ".." for part in parts):
        raise SandboxViolation("删除路径无效")
    # 变量说明：descriptors 表示当前流程使用的 descriptors 集合。
    descriptors: list[int] = []
    # 变量说明：flags 表示当前流程使用的 flags 集合。
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        # 变量说明：parent_fd 表示当前步骤使用的 parent_fd 值。
        parent_fd = os.open(sandbox.root, flags)
        descriptors.append(parent_fd)
        for part in parts[:-1]:
            # 变量说明：parent_fd 表示当前步骤使用的 parent_fd 值。
            parent_fd = os.open(part, flags, dir_fd=parent_fd)
            descriptors.append(parent_fd)
        try:
            # 变量说明：info 表示当前步骤使用的 info 值。
            info = os.stat(parts[-1], dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            return None
        if stat.S_ISLNK(info.st_mode):
            raise SandboxViolation("不允许删除符号链接")
        if not stat.S_ISREG(info.st_mode):
            raise SandboxViolation("delete 只允许删除单个普通文件")
        os.unlink(parts[-1], dir_fd=parent_fd)
        return "/".join(parts)
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


# 函数职责：完成 atomic_delete_windows 对应的业务处理。
# 参数关系：sandbox 表示当前步骤使用的 sandbox 值；path 表示当前文件或目录路径。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def _atomic_delete_windows(sandbox: WorkspaceSandbox, path: str) -> str | None:
    import ctypes
    from ctypes import wintypes

    # 变量说明：relative 表示当前步骤使用的 relative 值。
    relative = Path(path)
    # 变量说明：parts 表示当前流程使用的 parts 集合。
    parts = [part for part in relative.parts if part not in {"", "."}]
    if not parts or any(part == ".." for part in parts):
        raise SandboxViolation("删除路径无效")
    # 变量说明：lexical 表示当前步骤使用的 lexical 值。
    lexical = Path(os.path.abspath(sandbox.root.joinpath(*parts)))
    try:
        lexical.relative_to(sandbox.root)
    except ValueError as exc:
        raise SandboxViolation("路径越过了工作区边界") from exc

    # 变量说明：kernel32 表示当前步骤使用的 kernel32 值。
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    # 变量说明：create_file 表示当前步骤使用的 create_file 值。
    create_file = kernel32.CreateFileW
    # 变量说明：argtypes 表示当前流程使用的 argtypes 集合。
    create_file.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    # 变量说明：restype 表示当前步骤使用的 restype 值。
    create_file.restype = wintypes.HANDLE
    # 变量说明：close_handle 表示当前步骤使用的 close_handle 值。
    close_handle = kernel32.CloseHandle
    # 变量说明：argtypes 表示当前流程使用的 argtypes 集合。
    close_handle.argtypes = [wintypes.HANDLE]
    # 变量说明：restype 表示当前步骤使用的 restype 值。
    close_handle.restype = wintypes.BOOL
    # 变量说明：get_info 表示当前步骤使用的 get_info 值。
    get_info = kernel32.GetFileInformationByHandle
    # 变量说明：argtypes 表示当前流程使用的 argtypes 集合。
    get_info.argtypes = [wintypes.HANDLE, wintypes.LPVOID]
    # 变量说明：restype 表示当前步骤使用的 restype 值。
    get_info.restype = wintypes.BOOL
    # 变量说明：get_final_path 表示get_final_path 对应的文件系统位置。
    get_final_path = kernel32.GetFinalPathNameByHandleW
    # 变量说明：argtypes 表示当前流程使用的 argtypes 集合。
    get_final_path.argtypes = [wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD]
    # 变量说明：restype 表示当前步骤使用的 restype 值。
    get_final_path.restype = wintypes.DWORD
    # 变量说明：set_info 表示当前步骤使用的 set_info 值。
    set_info = kernel32.SetFileInformationByHandle
    # 变量说明：argtypes 表示当前流程使用的 argtypes 集合。
    set_info.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPVOID, wintypes.DWORD]
    # 变量说明：restype 表示当前步骤使用的 restype 值。
    set_info.restype = wintypes.BOOL

    # 变量说明：delete_access 表示当前流程使用的 delete_access 集合。
    delete_access = 0x00010000 | 0x00000080
    # 变量说明：share_all 表示当前步骤使用的 share_all 值。
    share_all = 0x00000001 | 0x00000002 | 0x00000004
    # 变量说明：open_existing 表示当前步骤使用的 open_existing 值。
    open_existing = 3
    # 变量说明：open_reparse_point 表示当前步骤使用的 open_reparse_point 值。
    open_reparse_point = 0x00200000
    # 变量说明：backup_semantics 表示当前流程使用的 backup_semantics 集合。
    backup_semantics = 0x02000000
    # 变量说明：invalid_handle 表示当前步骤使用的 invalid_handle 值。
    invalid_handle = wintypes.HANDLE(-1).value
    # 变量说明：handle 表示当前步骤使用的 handle 值。
    handle = create_file(
        str(lexical),
        delete_access,
        share_all,
        None,
        open_existing,
        open_reparse_point | backup_semantics,
        None,
    )
    if handle == invalid_handle:
        # 变量说明：error 表示当前捕获或准备上报的错误。
        error = ctypes.get_last_error()
        if error in {2, 3}:
            return None
        raise OSError(error, ctypes.FormatError(error), str(lexical))

    # 类职责：定义 ByHandleFileInformation 在本领域中的数据与行为。
    # 继承关系：复用基类提供的契约，并向调用方暴露本类声明的字段和方法。
    class ByHandleFileInformation(ctypes.Structure):
        # 变量说明：_fields_ 表示当前步骤使用的 _fields_ 值。
        _fields_ = [
            ("file_attributes", wintypes.DWORD),
            ("creation_time", wintypes.FILETIME),
            ("last_access_time", wintypes.FILETIME),
            ("last_write_time", wintypes.FILETIME),
            ("volume_serial_number", wintypes.DWORD),
            ("file_size_high", wintypes.DWORD),
            ("file_size_low", wintypes.DWORD),
            ("number_of_links", wintypes.DWORD),
            ("file_index_high", wintypes.DWORD),
            ("file_index_low", wintypes.DWORD),
        ]

    # 类职责：定义 FileDispositionInfo 在本领域中的数据与行为。
    # 继承关系：复用基类提供的契约，并向调用方暴露本类声明的字段和方法。
    class FileDispositionInfo(ctypes.Structure):
        # 变量说明：_fields_ 表示当前步骤使用的 _fields_ 值。
        _fields_ = [("delete_file", ctypes.c_ubyte)]

    try:
        # 变量说明：info 表示当前步骤使用的 info 值。
        info = ByHandleFileInformation()
        if not get_info(handle, ctypes.byref(info)):
            # 变量说明：error 表示当前捕获或准备上报的错误。
            error = ctypes.get_last_error()
            raise OSError(error, ctypes.FormatError(error), str(lexical))
        if info.file_attributes & 0x00000400:
            raise SandboxViolation("不允许删除符号链接或目录联接")
        if info.file_attributes & 0x00000010:
            raise SandboxViolation("delete 只允许删除单个文件，不支持目录或递归删除")

        # 变量说明：size 表示当前步骤使用的 size 值。
        size = get_final_path(handle, None, 0, 0)
        if not size:
            # 变量说明：error 表示当前捕获或准备上报的错误。
            error = ctypes.get_last_error()
            raise OSError(error, ctypes.FormatError(error), str(lexical))
        # 变量说明：buffer 表示当前步骤使用的 buffer 值。
        buffer = ctypes.create_unicode_buffer(size + 1)
        if not get_final_path(handle, buffer, len(buffer), 0):
            # 变量说明：error 表示当前捕获或准备上报的错误。
            error = ctypes.get_last_error()
            raise OSError(error, ctypes.FormatError(error), str(lexical))
        # 变量说明：final_text 表示final 的文本表示。
        final_text = buffer.value
        if final_text.startswith("\\\\?\\UNC\\"):
            # 变量说明：final_text 表示final 的文本表示。
            final_text = "\\\\" + final_text[8:]
        elif final_text.startswith("\\\\?\\"):
            # 变量说明：final_text 表示final 的文本表示。
            final_text = final_text[4:]
        # 变量说明：final_path 表示final_path 对应的文件系统位置。
        final_path = Path(final_text)
        if os.path.normcase(str(final_path)) != os.path.normcase(str(lexical)):
            raise SandboxViolation("删除路径经过了符号链接或目录联接")

        # 变量说明：disposition 表示当前步骤使用的 disposition 值。
        disposition = FileDispositionInfo(1)
        if not set_info(handle, 4, ctypes.byref(disposition), ctypes.sizeof(disposition)):
            # 变量说明：error 表示当前捕获或准备上报的错误。
            error = ctypes.get_last_error()
            raise OSError(error, ctypes.FormatError(error), str(lexical))
        return lexical.relative_to(sandbox.root).as_posix()
    finally:
        close_handle(handle)


# 函数职责：完成 split_command 对应的业务处理。
# 参数关系：command 表示当前步骤使用的 command 值。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def _split_command(command: str | list[str]) -> list[str]:
    if isinstance(command, list):
        # 变量说明：parts 表示当前流程使用的 parts 集合。
        parts = [str(part) for part in command]
    else:
        # 变量说明：joined 表示当前步骤使用的 joined 值。
        joined = command.strip()
        if any(token in joined for token in _DANGEROUS_SHELL_TOKENS):
            raise ValueError("命令包含被禁止的 shell 链接或重定向符号")
        # 变量说明：parts 表示当前流程使用的 parts 集合。
        parts = shlex.split(joined, posix=os.name != "nt")
    if not parts:
        raise ValueError("命令不能为空")
    return [part.strip('"') for part in parts]


# 函数职责：解析 command_argv 对应的数据或流程。
# 参数关系：command 表示当前步骤使用的 command 值；allowlist 表示当前步骤使用的 allowlist 值。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def parse_command_argv(
    command: str | list[str],
    *,
    allowlist: frozenset[str] = DEFAULT_COMMAND_ALLOWLIST,
) -> list[str]:
    """Parse one shell-free command and enforce its executable boundary."""

    # 变量说明：normalized 表示当前步骤使用的 normalized 值。
    normalized = _split_command(command)
    if any(marker in normalized[0] for marker in ("/", "\\", ":")):
        raise ValueError("命令必须使用 allowlist 中的裸可执行文件名，不能提供路径")
    # 变量说明：executable 表示当前步骤使用的 executable 值。
    executable = normalized[0].lower()
    # 变量说明：normalized_allowlist 表示当前步骤使用的 normalized_allowlist 值。
    normalized_allowlist = {item.lower() for item in allowlist}
    if executable not in normalized_allowlist:
        raise ValueError(f"命令不在允许列表中: {executable}")
    if executable.endswith((".cmd", ".bat")) and any(
        any(token in argument for token in _WINDOWS_BATCH_CONTROL_TOKENS)
        for argument in normalized[1:]
    ):
        raise ValueError("批处理命令参数包含被禁止的 shell 链接或重定向符号")
    return normalized


# 函数职责：执行 command 对应的数据或流程。
# 参数关系：sandbox 表示当前步骤使用的 sandbox 值；command 表示当前步骤使用的 command 值；approved 表示当前步骤使用的 approved 值；cwd 表示当前步骤使用的 cwd 值；timeout_seconds 表示当前流程使用的 timeout_seconds 集合；output_limit 表示当前步骤使用的 output_limit 值；allowlist 表示当前步骤使用的 allowlist 值；_cancel_event 表示当前步骤使用的 _cancel_event 值。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def run_command(
    sandbox: WorkspaceSandbox,
    command: str | list[str],
    *,
    approved: bool = False,
    cwd: str = ".",
    timeout_seconds: float = 30,
    output_limit: int = 20_000,
    allowlist: frozenset[str] = DEFAULT_COMMAND_ALLOWLIST,
    _cancel_event: threading.Event | None = None,
) -> ToolResult:
    # 变量说明：arguments 表示当前流程使用的 arguments 集合。
    arguments = {"command": command, "cwd": cwd, "timeout_seconds": timeout_seconds}
    if not approved:
        return _approval(
            "run_command",
            arguments,
            "命令将以当前用户权限在本机运行，可能访问工作区外资源；执行前必须批准",
        )
    try:
        # 变量说明：working_directory 表示当前步骤使用的 working_directory 值。
        working_directory = sandbox.resolve(cwd, must_exist=True)
        if not working_directory.is_dir():
            return ToolResult("run_command", False, "cwd is not a directory", error_code="not_directory")
        # 变量说明：relative_cwd 表示当前步骤使用的 relative_cwd 值。
        relative_cwd = sandbox.relative(working_directory)
        try:
            # 变量说明：parts 表示当前流程使用的 parts 集合。
            parts = parse_command_argv(command, allowlist=allowlist)
        except ValueError as exc:
            # 变量说明：message 表示当前消息。
            message = str(exc)
            return ToolResult(
                "run_command",
                False,
                message,
                error_code=(
                    "command_error"
                    if message.startswith("命令包含被禁止")
                    else "command_not_allowed"
                ),
            )
        # 变量说明：timeout 表示当前步骤使用的 timeout 值。
        timeout = min(max(float(timeout_seconds), 0.1), 120.0)
        # 变量说明：output_limit 表示当前步骤使用的 output_limit 值。
        output_limit = min(max(int(output_limit), 256), 1_000_000)
        # 变量说明：popen_kwargs 表示当前流程使用的 popen_kwargs 集合。
        popen_kwargs: dict[str, Any] = {}
        if os.name == "nt":
            popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            # 变量说明：popen_kwargs 的索引项 表示该语句创建或更新的目标数据。
            popen_kwargs["start_new_session"] = True
        # 变量说明：process 表示当前流程使用的 process 集合。
        process = subprocess.Popen(
            parts,
            cwd=working_directory,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            shell=False,
            **popen_kwargs,
        )
        # 变量说明：chunks 表示当前流程使用的 chunks 集合。
        chunks: list[str] = []
        # 变量说明：captured_chars 表示当前流程使用的 captured_chars 集合。
        captured_chars = 0
        # 变量说明：output_truncated 表示当前步骤使用的 output_truncated 值。
        output_truncated = False
        # 变量说明：capture_lock 表示当前步骤使用的 capture_lock 值。
        capture_lock = threading.Lock()

        # 函数职责：完成 drain 对应的业务处理。
        # 参数关系：stream 表示当前步骤使用的 stream 值。
        # 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
        def drain(stream: Any) -> None:
            nonlocal captured_chars, output_truncated
            try:
                while True:
                    # 变量说明：chunk 表示当前步骤使用的 chunk 值。
                    chunk = stream.read(4096)
                    if not chunk:
                        return
                    with capture_lock:
                        # 变量说明：remaining 表示当前步骤使用的 remaining 值。
                        remaining = output_limit - captured_chars
                        if remaining > 0:
                            # 变量说明：kept 表示当前步骤使用的 kept 值。
                            kept = chunk[:remaining]
                            chunks.append(kept)
                            captured_chars += len(kept)
                        if len(chunk) > max(0, remaining):
                            # 变量说明：output_truncated 表示当前步骤使用的 output_truncated 值。
                            output_truncated = True
            finally:
                stream.close()

        # 变量说明：readers 表示当前流程使用的 readers 集合。
        readers = [
            threading.Thread(target=drain, args=(process.stdout,), daemon=True),
            threading.Thread(target=drain, args=(process.stderr,), daemon=True),
        ]
        for reader in readers:
            reader.start()
        # 变量说明：deadline 表示当前步骤使用的 deadline 值。
        deadline = time.monotonic() + timeout
        # 变量说明：cancelled 表示当前步骤使用的 cancelled 值。
        cancelled = False
        # 变量说明：timed_out 表示当前步骤使用的 timed_out 值。
        timed_out = False
        while process.poll() is None:
            if _cancel_event is not None and _cancel_event.is_set():
                # 变量说明：cancelled 表示当前步骤使用的 cancelled 值。
                cancelled = True
                break
            # 变量说明：remaining 表示当前步骤使用的 remaining 值。
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                # 变量说明：timed_out 表示当前步骤使用的 timed_out 值。
                timed_out = True
                break
            try:
                process.wait(timeout=min(0.2, remaining))
            except subprocess.TimeoutExpired:
                continue
        if cancelled or timed_out:
            # 变量说明：tree_terminated 表示当前步骤使用的 tree_terminated 值。
            tree_terminated = False
            if os.name == "nt":
                try:
                    # 变量说明：killed 表示当前步骤使用的 killed 值。
                    killed = subprocess.run(
                        ["taskkill.exe", "/PID", str(process.pid), "/T", "/F"],
                        capture_output=True,
                        check=False,
                        timeout=3,
                    )
                    # 变量说明：tree_terminated 表示当前步骤使用的 tree_terminated 值。
                    tree_terminated = killed.returncode == 0
                except (OSError, subprocess.TimeoutExpired):
                    # 变量说明：tree_terminated 表示当前步骤使用的 tree_terminated 值。
                    tree_terminated = False
            else:
                try:
                    os.killpg(os.getpgid(process.pid), signal.SIGKILL)
                    # 变量说明：tree_terminated 表示当前步骤使用的 tree_terminated 值。
                    tree_terminated = True
                except ProcessLookupError:
                    # 变量说明：tree_terminated 表示当前步骤使用的 tree_terminated 值。
                    tree_terminated = process.poll() is not None
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                # Last-resort root-process cleanup. This cannot prove that every
                # descendant died, so report the weaker guarantee truthfully.
                process.kill()
                process.wait(timeout=2)
                # 变量说明：tree_terminated 表示当前步骤使用的 tree_terminated 值。
                tree_terminated = False
            # 变量说明：tree_terminated 表示当前步骤使用的 tree_terminated 值。
            tree_terminated = tree_terminated and process.poll() is not None
            for reader in readers:
                reader.join(timeout=2)
            # 变量说明：partial 表示当前步骤使用的 partial 值。
            partial = "".join(chunks)
            if cancelled:
                return ToolResult(
                    "run_command",
                    False,
                    (partial + "\nCommand execution interrupted by the user.")[:output_limit],
                    error_code="cancelled",
                    metadata={
                        "command": command,
                        "cwd": relative_cwd,
                        "process_tree_terminated": tree_terminated,
                        "truncated": output_truncated,
                        "output_truncated": output_truncated,
                        "security_scope": "current_user_host_permissions",
                    },
                )
            # 变量说明：termination_text 表示termination 的文本表示。
            termination_text = "进程树已终止" if tree_terminated else "主进程已终止，但无法确认全部子进程"
            return ToolResult(
                "run_command",
                False,
                (partial + f"\n命令执行超时（{timeout_seconds}s），{termination_text}")[:output_limit],
                error_code="timeout",
                metadata={
                    "command": command,
                    "cwd": relative_cwd,
                    "timeout_seconds": timeout_seconds,
                    "process_tree_terminated": tree_terminated,
                    "truncated": output_truncated,
                    "output_truncated": output_truncated,
                    "security_scope": "current_user_host_permissions",
                },
            )
        for reader in readers:
            reader.join(timeout=2)
        # 变量说明：combined 表示当前步骤使用的 combined 值。
        combined = "".join(chunks)
        # 变量说明：content 表示待处理或返回的正文内容。
        content = combined
        return ToolResult(
            "run_command",
            process.returncode == 0,
            content or f"命令结束，退出码 {process.returncode}",
            changed=False,
            error_code=None if process.returncode == 0 else "nonzero_exit",
            metadata={
                "command": command,
                "cwd": relative_cwd,
                "exit_code": process.returncode,
                "truncated": output_truncated,
                "output_truncated": output_truncated,
                "security_scope": "current_user_host_permissions",
            },
        )
    except (ValueError, OSError) as exc:
        return ToolResult("run_command", False, str(exc), error_code="command_error", metadata={"command": command})
