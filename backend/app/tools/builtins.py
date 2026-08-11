"""Local-first tools exposed to PGAgent's model loop."""

from __future__ import annotations

import fnmatch
import html
import inspect
import ipaddress
import os
import re
import shlex
import signal
import socket
import subprocess
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Awaitable, Callable, Iterator, Mapping
from urllib.parse import parse_qs, quote_plus, urlsplit, urlunsplit

import httpx

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
MAX_WEB_RESPONSE_BYTES = 1_000_000
MAX_WEB_TEXT_CHARS = 100_000
MAX_SKILL_INSTRUCTION_CHARS = 40_000
MAX_TODOS = 100


class UnsafeWebUrlError(ValueError):
    """A URL violates the webfetch SSRF boundary."""


class WebHostResolutionError(RuntimeError):
    """The URL is syntactically safe but its public host could not resolve."""


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
        normalized_pattern = str(pattern or "").strip().replace("\\", "/")
        if not normalized_pattern:
            return ToolResult("glob", False, "pattern 不能为空", error_code="invalid_pattern")
        if len(normalized_pattern) > 512 or Path(normalized_pattern).is_absolute():
            return ToolResult("glob", False, "glob pattern 无效", error_code="invalid_pattern")
        root = sandbox.resolve(path, must_exist=True)
        if not root.is_dir():
            return ToolResult("glob", False, "目标不是目录", error_code="not_directory")

        entry_limit = min(max(int(max_entries), 1), 100_000)
        result_limit = min(max(int(limit), 1), 2_000)
        stats = {"skipped": 0, "visited": 0, "scan_limit_reached": 0}
        matches: list[str] = []
        for lexical_path, safe_path in _safe_walk(
            sandbox,
            root,
            recursive=True,
            max_entries=entry_limit,
            stats=stats,
        ):
            try:
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
    except (SandboxViolation, FileNotFoundError, OSError, ValueError) as exc:
        return ToolResult("glob", False, str(exc), error_code="glob_error")


def _unsafe_regular_expression(pattern: str) -> bool:
    """Reject the common catastrophic-backtracking shapes before scanning files."""

    # This is intentionally conservative rather than pretending to prove that
    # every regexp is linear.  It blocks the frequent `(a+)+` / `(.*)*` forms
    # while normal source-code search patterns remain available.
    return bool(re.search(r"\((?:[^()]|\([^()]*\))*[+*][^)]*\)[+*{]", pattern))


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
        query = str(pattern or "")
        if not query:
            return ToolResult("grep", False, "pattern 不能为空", error_code="invalid_pattern")
        if len(query) > 512 or _unsafe_regular_expression(query):
            return ToolResult("grep", False, "正则表达式过长或可能造成过度回溯", error_code="unsafe_pattern")
        root = sandbox.resolve(path, must_exist=True)
        if not root.is_dir():
            return ToolResult("grep", False, "目标不是目录", error_code="not_directory")
        matcher = re.compile(query, 0 if case_sensitive else re.IGNORECASE)
        entry_limit = min(max(int(max_entries), 1), 100_000)
        result_limit = min(max(int(limit), 1), 2_000)
        stats = {"skipped": 0, "visited": 0, "scan_limit_reached": 0}
        skipped = 0
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
        target = sandbox.resolve(path, must_exist=True)
        if not target.is_file():
            return ToolResult("edit", False, "目标不是文件", error_code="not_file")
        with target.open("rb") as binary_stream:
            if b"\x00" in binary_stream.read(8192):
                return ToolResult("edit", False, "暂不支持编辑二进制文件", error_code="binary_file")
        # Keep CRLF/LF exactly as stored except for the requested replacement.
        with target.open("r", encoding="utf-8", errors="replace", newline="") as stream:
            original = stream.read()
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
        updated = original.replace(old_string, new_string, -1 if replace_all else 1)
        with target.open("w", encoding="utf-8", newline="") as stream:
            stream.write(updated)
        replacements = matches if replace_all else 1
        return ToolResult(
            "edit",
            True,
            f"已修改 {sandbox.relative(target)}，替换 {replacements} 处",
            changed=updated != original,
            metadata={"path": sandbox.relative(target), "replacements": replacements},
        )
    except (SandboxViolation, FileNotFoundError, OSError) as exc:
        return ToolResult("edit", False, str(exc), error_code="edit_error")


def _is_public_ip(address: str) -> bool:
    try:
        candidate = ipaddress.ip_address(address)
    except ValueError:
        return False
    return not (
        candidate.is_private
        or candidate.is_loopback
        or candidate.is_link_local
        or candidate.is_multicast
        or candidate.is_reserved
        or candidate.is_unspecified
    )


def _validate_public_http_url(url: str) -> tuple[str, str]:
    """Validate scheme and DNS answers before a web request.

    The client also disables environment proxy discovery and redirects.  DNS is
    revalidated for every explicit fetch.  This is a strong server-side guard
    for normal use; it deliberately does not claim that a generic HTTP client
    turns arbitrary network access into a filesystem sandbox.
    """

    candidate = str(url or "").strip()
    if not candidate or len(candidate) > 4_096:
        raise UnsafeWebUrlError("URL 不能为空或过长")
    parsed = urlsplit(candidate)
    if parsed.scheme.lower() not in {"http", "https"}:
        raise UnsafeWebUrlError("仅允许 http 或 https URL")
    if not parsed.hostname:
        raise UnsafeWebUrlError("URL 缺少主机名")
    if parsed.username or parsed.password:
        raise UnsafeWebUrlError("URL 不允许包含用户名或密码")
    host = parsed.hostname.rstrip(".").lower()
    if host in {"localhost", "localhost.localdomain"} or host.endswith(".local"):
        raise UnsafeWebUrlError("不允许访问本机或本地网络地址")
    try:
        port = parsed.port or (443 if parsed.scheme.lower() == "https" else 80)
    except ValueError as exc:
        raise UnsafeWebUrlError("URL 端口无效") from exc
    try:
        answers = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise WebHostResolutionError("无法解析目标主机") from exc
    addresses = {str(answer[4][0]) for answer in answers if answer[4]}
    if not addresses or any(not _is_public_ip(address) for address in addresses):
        raise UnsafeWebUrlError("不允许访问私有、回环或保留网络地址")
    # Canonicalizing the hostname avoids a host spelling changing after the
    # validation decision, while preserving the query and fragment semantics.
    safe_url = urlunsplit((parsed.scheme.lower(), parsed.netloc, parsed.path or "/", parsed.query, ""))
    return safe_url, host


def _response_peer_is_public(response: httpx.Response) -> bool:
    """Best-effort second SSRF check against the actual connected peer."""

    stream = response.extensions.get("network_stream")
    if stream is None or not hasattr(stream, "get_extra_info"):
        # Some mock/custom transports do not expose peer information.  The
        # preflight DNS guard remains in effect and keeps tests transport-agnostic.
        return True
    peer = stream.get_extra_info("server_addr")
    if not peer:
        return True
    try:
        return _is_public_ip(str(peer[0]))
    except (IndexError, TypeError):
        return False


def web_fetch(
    _sandbox: WorkspaceSandbox,
    url: str,
    *,
    timeout_seconds: float = 15,
    max_bytes: int = MAX_WEB_RESPONSE_BYTES,
) -> ToolResult:
    """Fetch a public HTTP(S) resource with bounded response handling.

    Redirects are deliberately *not* followed.  A model may make a separate
    explicit fetch for the reported public redirect URL, causing it to pass the
    same SSRF checks again.
    """

    try:
        safe_url, host = _validate_public_http_url(url)
        timeout = min(max(float(timeout_seconds), 1.0), 30.0)
        byte_limit = min(max(int(max_bytes), 1_024), MAX_WEB_RESPONSE_BYTES)
        headers = {
            "User-Agent": "PGAgent/0.1 (+local safe webfetch)",
            "Accept": "text/plain,text/html,application/json,application/xml,text/xml;q=0.9,*/*;q=0.1",
        }
        body = bytearray()
        with httpx.Client(
            follow_redirects=False,
            timeout=httpx.Timeout(timeout),
            trust_env=False,
            headers=headers,
        ) as client:
            with client.stream("GET", safe_url) as response:
                if not _response_peer_is_public(response):
                    return ToolResult("webfetch", False, "连接目标不是公共网络地址", error_code="unsafe_url")
                if 300 <= response.status_code < 400:
                    location = response.headers.get("location", "")
                    return ToolResult(
                        "webfetch",
                        True,
                        "服务器返回重定向；出于安全不会自动跟随。请确认后使用返回的 URL 再次抓取。",
                        metadata={
                            "url": safe_url,
                            "host": host,
                            "status_code": response.status_code,
                            "redirect_url": location,
                            "redirect_followed": False,
                        },
                    )
                for chunk in response.iter_bytes():
                    remaining = byte_limit - len(body)
                    if remaining <= 0:
                        break
                    body.extend(chunk[:remaining])
                    if len(chunk) > remaining:
                        break
                truncated = len(body) >= byte_limit
                content_type = response.headers.get("content-type", "").lower()
                if not any(marker in content_type for marker in ("text/", "json", "xml", "javascript")):
                    return ToolResult(
                        "webfetch",
                        response.is_success,
                        "已读取非文本响应；为避免将二进制内容写入上下文，不返回正文。",
                        error_code=None if response.is_success else "http_error",
                        metadata={
                            "url": safe_url,
                            "host": host,
                            "status_code": response.status_code,
                            "content_type": content_type or "unknown",
                            "bytes_read": len(body),
                            "truncated": truncated,
                        },
                    )
                encoding = response.encoding or "utf-8"
                text = bytes(body).decode(encoding, errors="replace")
                text_truncated = len(text) > MAX_WEB_TEXT_CHARS
                text = text[:MAX_WEB_TEXT_CHARS]
                return ToolResult(
                    "webfetch",
                    response.is_success,
                    text if text else f"HTTP {response.status_code}（空响应）",
                    error_code=None if response.is_success else "http_error",
                    metadata={
                        "url": safe_url,
                        "host": host,
                        "status_code": response.status_code,
                        "content_type": content_type or "unknown",
                        "bytes_read": len(body),
                        "truncated": truncated or text_truncated,
                    },
                )
    except (UnsafeWebUrlError, ValueError) as exc:
        return ToolResult("webfetch", False, str(exc), error_code="unsafe_url")
    except WebHostResolutionError:
        return ToolResult("webfetch", False, "网络请求失败: 无法解析目标主机", error_code="network_error")
    except httpx.HTTPError as exc:
        return ToolResult("webfetch", False, f"网络请求失败: {type(exc).__name__}", error_code="network_error")


def _clean_html_text(value: str) -> str:
    without_tags = re.sub(r"<[^>]+>", " ", value)
    return " ".join(html.unescape(without_tags).split())


def _unwrap_duckduckgo_url(value: str) -> str:
    candidate = html.unescape(value)
    if candidate.startswith("//"):
        candidate = f"https:{candidate}"
    parsed = urlsplit(candidate)
    if parsed.netloc.endswith("duckduckgo.com"):
        redirect_target = parse_qs(parsed.query).get("uddg", [""])[0]
        if redirect_target:
            return redirect_target
    return candidate


def _duckduckgo_results(markup: str, limit: int) -> list[tuple[str, str]]:
    """Extract result links from the public HTML endpoint without a new parser dep."""

    result_re = re.compile(
        r"<a\b(?=[^>]*\bclass=[\"'][^\"']*\bresult__a\b[^\"']*[\"'])"
        r"(?=[^>]*\bhref=[\"'](?P<href>[^\"']+)[\"'])[^>]*>(?P<title>.*?)</a>",
        re.IGNORECASE | re.DOTALL,
    )
    results: list[tuple[str, str]] = []
    for match in result_re.finditer(markup):
        title = _clean_html_text(match.group("title"))
        destination = _unwrap_duckduckgo_url(match.group("href"))
        if not title or not destination:
            continue
        results.append((title[:500], destination[:2_000]))
        if len(results) >= limit:
            break
    return results


def web_search(
    sandbox: WorkspaceSandbox,
    query: str,
    *,
    limit: int = 5,
) -> ToolResult:
    """Perform a provider-free DuckDuckGo HTML search, or return an honest error."""

    search_query = str(query or "").strip()
    if not search_query or len(search_query) > 500:
        return ToolResult("websearch", False, "query 不能为空或过长", error_code="invalid_query")
    result_limit = min(max(int(limit), 1), 10)
    response = web_fetch(
        sandbox,
        f"https://html.duckduckgo.com/html/?q={quote_plus(search_query)}",
        timeout_seconds=15,
        max_bytes=MAX_WEB_RESPONSE_BYTES,
    )
    if not response.ok:
        return ToolResult(
            "websearch",
            False,
            f"搜索服务当前不可用: {response.content}",
            error_code=response.error_code or "search_provider_unavailable",
            metadata={"provider": "duckduckgo_html"},
        )
    results = _duckduckgo_results(response.content, result_limit)
    if not results:
        return ToolResult(
            "websearch",
            False,
            "搜索服务未返回可解析的公开结果；没有伪造搜索结果。",
            error_code="search_provider_unavailable",
            metadata={"provider": "duckduckgo_html", "source_status": response.metadata.get("status_code")},
        )
    lines = [f"{index}. {title}\n   {url}" for index, (title, url) in enumerate(results, start=1)]
    return ToolResult(
        "websearch",
        True,
        "\n".join(lines),
        metadata={"provider": "duckduckgo_html", "count": len(results), "query": search_query},
    )


def _normalize_todos(value: Any) -> list[dict[str, str]]:
    if not isinstance(value, list):
        raise ValueError("todos 必须是列表")
    if len(value) > MAX_TODOS:
        raise ValueError(f"todos 最多 {MAX_TODOS} 项")
    normalized: list[dict[str, str]] = []
    allowed_statuses = {"pending", "in_progress", "completed", "cancelled"}
    seen_ids: set[str] = set()
    for index, raw in enumerate(value, start=1):
        if not isinstance(raw, Mapping):
            raise ValueError(f"第 {index} 项 todo 必须是对象")
        content = str(raw.get("content") or "").strip()
        if not content or len(content) > 1_000:
            raise ValueError(f"第 {index} 项 todo 的 content 不能为空且最多 1000 字符")
        todo_id = str(raw.get("id") or index).strip()
        if not todo_id or len(todo_id) > 120 or todo_id in seen_ids:
            raise ValueError(f"第 {index} 项 todo 的 id 无效或重复")
        seen_ids.add(todo_id)
        status = str(raw.get("status") or "pending").strip().lower()
        if status not in allowed_statuses:
            raise ValueError(f"第 {index} 项 todo 的 status 无效")
        item = {"id": todo_id, "content": content, "status": status}
        if raw.get("active_form"):
            item["active_form"] = str(raw["active_form"])[:1_000]
        normalized.append(item)
    return normalized


def todo_write(
    _sandbox: WorkspaceSandbox,
    todos: list[dict[str, Any]],
    *,
    todo_state: list[dict[str, str]],
) -> ToolResult:
    """Replace the run's structured todo state with validated items."""

    try:
        normalized = _normalize_todos(todos)
    except ValueError as exc:
        return ToolResult("todowrite", False, str(exc), error_code="invalid_todos")
    changed = todo_state != normalized
    todo_state[:] = normalized
    active = next((item["content"] for item in normalized if item["status"] == "in_progress"), "")
    return ToolResult(
        "todowrite",
        True,
        f"已更新 {len(normalized)} 项任务" + (f"；当前进行中：{active}" if active else ""),
        changed=changed,
        metadata={"todos": normalized, "count": len(normalized)},
    )


def ask_question(
    _sandbox: WorkspaceSandbox,
    question: str,
    *,
    context: str = "",
) -> ToolResult:
    """Ask for clarification without pretending that a same-run reply exists."""

    prompt = str(question or "").strip()
    if not prompt or len(prompt) > 2_000:
        return ToolResult("question", False, "question 不能为空或过长", error_code="invalid_question")
    details = str(context or "").strip()[:2_000]
    return ToolResult(
        "question",
        True,
        prompt,
        error_code="question_needed",
        metadata={"needs_user_input": True, "question": prompt, "context": details},
    )


def delegate_task(
    _sandbox: WorkspaceSandbox,
    task: str,
    *,
    agent_id: str = "",
    delegate: Callable[..., ToolResult | Awaitable[ToolResult]] | None = None,
) -> ToolResult:
    """Synchronous compatibility path for a real, injected task delegate.

    Runtime executions use :func:`delegate_task_async` so a child Agent can
    make asynchronous model calls without blocking the parent event loop.
    This synchronous entry point remains useful for direct tool tests and
    never tries to start a nested event loop.
    """

    request = str(task or "").strip()
    if not request or len(request) > 8_000:
        return ToolResult("task", False, "task 不能为空或过长", error_code="invalid_task")
    target = str(agent_id or "").strip()
    if not target or len(target) > 80:
        return ToolResult("task", False, "必须提供有效的子 Agent ID", error_code="invalid_delegate_agent")
    if delegate is None:
        return ToolResult(
            "task",
            False,
            "当前没有已接入的子 Agent 委派运行时；未创建或假称已执行子任务。",
            error_code="delegated_task_unavailable",
        )
    try:
        result = _call_task_delegate(delegate, request, target, call_id=None)
        if inspect.isawaitable(result):
            # Do not create a second event loop from a synchronous tool call.
            # Closing the coroutine prevents an unawaited-coroutine warning
            # while making the actual execution contract explicit.
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


def _call_task_delegate(
    delegate: Callable[..., ToolResult | Awaitable[ToolResult]],
    task: str,
    agent_id: str,
    *,
    call_id: str | None,
) -> ToolResult | Awaitable[ToolResult]:
    """Call modern delegates while preserving one-argument test adapters.

    PGAgent owns the production delegate signature.  The fallback only keeps
    older direct unit tests/extensions working; it is intentionally limited to
    a signature mismatch before a delegate is entered.
    """

    try:
        signature = inspect.signature(delegate)
    except (TypeError, ValueError):
        signature = None
    if signature is not None:
        parameters = signature.parameters.values()
        accepts_keywords = any(parameter.kind is inspect.Parameter.VAR_KEYWORD for parameter in parameters)
        names = signature.parameters
        if accepts_keywords or "agent_id" in names or "call_id" in names:
            keyword_arguments: dict[str, str | None] = {}
            if accepts_keywords or "agent_id" in names:
                keyword_arguments["agent_id"] = agent_id
            if accepts_keywords or "call_id" in names:
                keyword_arguments["call_id"] = call_id
            return delegate(task, **keyword_arguments)
    return delegate(task)


async def delegate_task_async(
    _sandbox: WorkspaceSandbox,
    task: str,
    *,
    agent_id: str = "",
    delegate: Callable[..., ToolResult | Awaitable[ToolResult]] | None = None,
    call_id: str | None = None,
) -> ToolResult:
    """Run an injected child-Agent delegate without pretending it is local.

    Validation is shared with the synchronous tool.  A failed delegate is
    converted into a normal tool result so the parent loop's existing repeat
    and no-progress guards remain responsible for recovery.
    """

    request = str(task or "").strip()
    if not request or len(request) > 8_000:
        return ToolResult("task", False, "task 不能为空或过长", error_code="invalid_task")
    target = str(agent_id or "").strip()
    if not target or len(target) > 80:
        return ToolResult("task", False, "必须提供有效的子 Agent ID", error_code="invalid_delegate_agent")
    if delegate is None:
        return ToolResult(
            "task",
            False,
            "当前没有已接入的子 Agent 委派运行时；未创建或假称已执行子任务。",
            error_code="delegated_task_unavailable",
        )
    try:
        result = _call_task_delegate(delegate, request, target, call_id=call_id)
        if inspect.isawaitable(result):
            result = await result
        if isinstance(result, ToolResult):
            return result
        return ToolResult("task", False, "子 Agent 委派器返回了无效结果", error_code="delegate_error")
    except Exception as exc:
        return ToolResult("task", False, f"子 Agent 委派失败: {type(exc).__name__}", error_code="delegate_error")


def load_skill(
    _sandbox: WorkspaceSandbox,
    skill_id: str = "",
    *,
    name: str = "",
    skill_instructions: Mapping[str, Mapping[str, Any]] | None = None,
) -> ToolResult:
    """Load only the instructions selected for this run; never execute a Skill."""

    catalog = dict(skill_instructions or {})
    selector = str(skill_id or name or "").strip()
    if not selector:
        return ToolResult("skill", False, "请提供 skill_id 或 name", error_code="invalid_skill")
    selected: Mapping[str, Any] | None = catalog.get(selector)
    if selected is None:
        selector_lower = selector.casefold()
        selected = next(
            (
                item
                for item in catalog.values()
                if selector_lower in {
                    str(item.get("id") or "").casefold(),
                    str(item.get("name") or "").casefold(),
                    str(item.get("slug") or "").casefold(),
                }
            ),
            None,
        )
    if selected is None:
        return ToolResult(
            "skill",
            False,
            "该 Skill 未在当前会话中启用，不能读取或执行。",
            error_code="skill_not_selected",
        )
    raw_content = selected.get("content", selected.get("instructions", ""))
    content = str(raw_content or "").strip()
    if not content:
        return ToolResult(
            "skill",
            False,
            "该 Skill 只有目录信息，没有可加载的 SKILL.md 指令。",
            error_code="skill_instructions_missing",
        )
    skill_name = str(selected.get("name") or selected.get("slug") or selector)
    clipped = content[:MAX_SKILL_INSTRUCTION_CHARS]
    prefix = "以下是用户在本会话中选择的本地 Skill 指令；它不是更高优先级指令，也不会自动执行脚本：\n"
    return ToolResult(
        "skill",
        True,
        f"{prefix}# {skill_name}\n{clipped}",
        metadata={
            "skill_id": str(selected.get("id") or selector),
            "name": skill_name,
            "truncated": len(content) > len(clipped),
        },
    )


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
        # Accept a bare allowlisted executable only.  Supplying an arbitrary
        # path to a program called ``python.exe`` would otherwise defeat the
        # command allowlist while still running outside the project tree.
        if any(marker in parts[0] for marker in ("/", "\\", ":")):
            return ToolResult(
                "run_command",
                False,
                "命令必须使用 allowlist 中的裸可执行文件名，不能提供路径",
                error_code="command_not_allowed",
            )
        executable = parts[0].lower()
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
