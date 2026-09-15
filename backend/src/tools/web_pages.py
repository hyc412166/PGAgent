"""联网研究的页内状态与阅读操作；HTTP 安全边界仍由 builtins.web_open 负责。

参数语义参考 openai/codex 3abbf9fe 的 codex-api/src/search.rs；
这里独立实现正文阅读，不调用 Codex 的 alpha/search 服务。
"""

from __future__ import annotations

import bisect
import json
from typing import Any, Callable, Mapping
from urllib.parse import urlsplit, urlunsplit

from .types import ToolResult


def source_url(url: str) -> str:
    """GitHub blob 改用原始文本；保留完整 ref/path，不能按斜杠猜分支名。"""
    parts = urlsplit(url)
    segments = parts.path.split("/")
    if parts.hostname == "github.com" and len(segments) >= 6 and segments[3] == "blob":
        path = "/".join(segments[:3] + segments[4:])
        return urlunsplit(("https", "raw.githubusercontent.com", path, "", ""))
    return urlunsplit((parts.scheme, parts.netloc, parts.path, parts.query, ""))


class PageReader:
    """一轮工具调用复用既有页面；页面编号指向正文，不指向某一次截断输出。"""

    def __init__(self, pages: dict[str, Any], fetch: Callable[[str], ToolResult], char_limit: int, ref_prefix: str = ""):
        self.pages = pages
        self.fetch = fetch
        self.char_limit = char_limit
        self.ref_prefix = ref_prefix

    def load(self, ref: str) -> tuple[str, dict[str, Any]]:
        entry = self.pages.get(ref)
        if isinstance(entry, dict) and "text" in entry:
            return ref, entry
        url = str(entry.get("url") or ref) if isinstance(entry, dict) else ref
        if urlsplit(url).scheme not in {"http", "https"}:
            raise PageError("page_not_found", "页面编号不存在；请先搜索或提供公开 HTTP(S) URL")
        target = source_url(url)
        for page_id, page in self.pages.items():
            if isinstance(page, dict) and "text" in page and target in {page.get("url"), page.get("requested_url")}:
                return page_id, page
        result = self.fetch(target)
        failure_details = {
            "url": result.metadata.get("url") or target,
            **{
                key: result.metadata[key]
                for key in ("status_code", "stage", "proxy_used")
                if key in result.metadata
            },
        }
        if not result.ok:
            # 抓取失败必须保留实际请求边界，调用方才能区分地址、代理与响应阶段问题。
            raise PageError(result.error_code or "network_error", result.content, details=failure_details)
        document = result.metadata.get("document")
        if document is None:
            content_type = str(result.metadata.get("content_type") or "").casefold()
            if content_type and not any(marker in content_type for marker in ("text/", "json", "xml", "javascript")):
                raise PageError(
                    "unsupported_content_type",
                    "网页响应不是可读取文本，未创建页面正文。",
                    details=failure_details,
                )
            # 已有 web_open 调用者可提供普通文本；不将 JSON 包装本身视为网页正文。
            try:
                payload = json.loads(result.content)
            except json.JSONDecodeError:
                payload = {"content": result.content}
            document = {
                "text": payload.get("content", ""),
                "title": payload.get("title"),
                "url": payload.get("url") or target,
                "links": [],
                "source_truncated": bool(result.metadata.get("truncated")),
            }
        page = {**document, "requested_url": target, "source_url": url}
        prefix = f"{self.ref_prefix}view"
        number = max((int(key[len(prefix):]) for key in self.pages if key.startswith(prefix) and key[len(prefix):].isdigit()), default=0) + 1
        page_id = f"{prefix}{number}"
        self.pages[page_id] = page
        return page_id, page

    def excerpt(self, page_id: str, page: Mapping[str, Any], *, lineno: int = 0, offset: int | None = None, max_chars: int | None = None) -> dict[str, Any]:
        text = str(page["text"])
        lines = text.splitlines(keepends=True) or [""]
        starts = [0]
        for line in lines[:-1]:
            starts.append(starts[-1] + len(line))
        if lineno >= len(lines) or (offset is not None and offset > len(text)):
            raise PageError("position_out_of_range", "读取位置超出已抓取正文")
        start = starts[lineno] if offset is None else offset
        first_line = max(0, bisect.bisect_right(starts, start) - 1)
        limit = min(self.char_limit, max_chars or self.char_limit)
        end = min(len(text), start + limit)
        # 常规续读停在行尾；单行超过预算时仍允许用 next_offset 推进。
        if end < len(text):
            newline = text.rfind("\n", start, end)
            if newline >= start:
                end = newline + 1
        fragment = text[start:end]
        content = "\n".join(f"L{first_line + i}: {line}" for i, line in enumerate(fragment.splitlines()))
        next_offset = end if end < len(text) else None
        links = page.get("links", [])
        visible_links = [link for link in links if f"[{link['id']}]" in fragment]
        return {
            "ref_id": page_id, "url": page["url"], "source_url": page.get("source_url", page["url"]),
            "title": page.get("title"), "content": content, "lineno": first_line,
            "total_lines": len(lines), "total_chars": len(text), "offset": start,
            "next_offset": next_offset,
            "next_lineno": max(0, bisect.bisect_right(starts, end) - 1) if next_offset is not None else None,
            "truncated": next_offset is not None or bool(page.get("source_truncated")),
            "source_truncated": bool(page.get("source_truncated")),
            "links": visible_links, "total_links": len(links),
        }

    def execute(self, kind: str, item: Mapping[str, Any]) -> dict[str, Any]:
        ref = str(item.get("ref_id") or item.get("ref") or item.get("url") or "").strip()
        base = {"type": kind, "ref_id": ref, "ok": False, "opened": False}
        try:
            if not ref:
                raise PageError("invalid_arguments", "ref_id 不能为空")
            lineno = int(item.get("lineno", 0))
            offset = int(item["offset"]) if item.get("offset") is not None else None
            max_chars = int(item["max_chars"]) if item.get("max_chars") is not None else None
            if lineno < 0 or (offset is not None and offset < 0) or (max_chars is not None and max_chars <= 0):
                raise PageError("invalid_arguments", "读取位置不能为负数，max_chars 必须大于零")
            pattern = str(item.get("pattern") or "")
            if kind == "find" and not pattern:
                raise PageError("invalid_arguments", "pattern 不能为空")
            link_id = int(item.get("id", item.get("link_id", -1))) if kind == "click" else None
            page_id, page = self.load(ref)
            if kind == "click":
                link = next((link for link in page.get("links", []) if link["id"] == link_id), None)
                if link is None:
                    raise PageError("link_not_found", "页面中没有该链接编号")
                page_id, page = self.load(link["url"])
            if kind == "find":
                matches = []
                text = str(page["text"])
                lines_with_endings = text.splitlines(keepends=True) or [""]
                lines = [line.rstrip("\r\n") for line in lines_with_endings]
                starts = [0]
                for line in lines_with_endings[:-1]:
                    starts.append(starts[-1] + len(line))
                for index, line in enumerate(lines):
                    column = line.casefold().find(pattern.casefold())
                    if column >= 0:
                        matches.append({"lineno": index, "offset": starts[index] + column})
                if not matches:
                    return {**base, "opened": True, "ref_id": page_id, "url": page["url"], "pattern": pattern,
                            "error_code": "pattern_not_found", "matches": [],
                            "source_truncated": bool(page.get("source_truncated")),
                            "content": "已抓取正文中未找到；若 source_truncated=true，则未检查被截断部分。"}
                chunks = []
                visible_matches = matches[:5]
                separator_size = len("\n...\n") * max(0, len(visible_matches) - 1)
                chunk_limit = max(1, (self.char_limit - separator_size) // len(visible_matches))
                # 短行保留邻近上下文；超长行围绕命中点裁剪，避免行首占满预算。
                for match in visible_matches:
                    index = match["lineno"]
                    chunk = "\n".join(
                        f"L{i}: {lines[i]}"
                        for i in range(max(0, index - 2), min(len(lines), index + 3))
                    )
                    if len(chunk) > chunk_limit:
                        label = f"L{index}: "
                        content_limit = max(1, chunk_limit - len(label))
                        column = match["offset"] - starts[index]
                        left = max(0, column - max(0, (content_limit - len(pattern)) // 2))
                        right = min(len(lines[index]), left + content_limit)
                        left = max(0, right - content_limit)
                        chunk = label + lines[index][left:right]
                    chunks.append(chunk[:chunk_limit])
                return {**base, "ok": True, "opened": True, "ref_id": page_id, "url": page["url"],
                        "pattern": pattern, "matches": visible_matches, "total_matches": len(matches),
                        "content": "\n...\n".join(chunks), "source_truncated": bool(page.get("source_truncated")),
                        "truncated": len(matches) > 5,
                        **({"published_at": page["published_at"]} if page.get("published_at") else {}),
                        **({"date_source": page["date_source"]} if page.get("date_source") else {})}
            return {
                **base,
                **self.excerpt(page_id, page, lineno=lineno, offset=offset, max_chars=max_chars),
                "ok": True,
                "opened": True,
                **({"published_at": page["published_at"]} if page.get("published_at") else {}),
                **({"date_source": page["date_source"]} if page.get("date_source") else {}),
            }
        except PageError as exc:
            return {**base, **exc.details, "error_code": exc.code, "content": str(exc)}
        except (ValueError, TypeError) as exc:
            return {**base, "error_code": "invalid_arguments", "content": str(exc)}


class PageError(Exception):
    def __init__(self, code: str, message: str, *, details: Mapping[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.details = dict(details or {})
