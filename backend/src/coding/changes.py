"""Structured, user-facing file-change evidence for coding runs."""
# 文件职责：统一生成、清洗和聚合文件变更记录，供工具事件、会话消息和 diff 面板复用。
# 逻辑关系：补丁/文件工具只负责提供前后文本；本模块负责把它们转换为稳定的 UI 数据契约。

from __future__ import annotations

import re
from collections.abc import Mapping
from difflib import unified_diff
from typing import Any


# 单个事件中的 diff 需要足够展示常规代码修改，同时避免一次 SSE 携带整个生成文件。
MAX_CHANGE_DIFF_CHARS = 120_000
MAX_CHANGE_SET_DIFF_CHARS = 500_000
MAX_CHANGE_FILES = 200
MAX_CHANGE_SOURCE_BYTES = 2_000_000
_HUNK_START = re.compile(r"^@@\s+-\d+(?:,\d+)?\s+\+(\d+)(?:,\d+)?\s+@@")


def _decode(value: bytes | str | None) -> tuple[str | None, bool]:
    """Decode a file snapshot and mark binary/unavailable content without raising."""

    if value is None:
        return "", False
    if isinstance(value, str):
        return value, "\x00" in value[:8192]
    if b"\x00" in value[:8192]:
        return None, True
    try:
        return value.decode("utf-8"), False
    except UnicodeDecodeError:
        return None, True


def _line_counts(diff: str) -> tuple[int, int, int | None]:
    added = 0
    deleted = 0
    first_line: int | None = None
    for line in diff.splitlines():
        if line.startswith("@@"):
            match = _HUNK_START.match(line)
            if match and first_line is None:
                first_line = max(1, int(match.group(1)))
        elif line.startswith("+") and not line.startswith("+++"):
            added += 1
        elif line.startswith("-") and not line.startswith("---"):
            deleted += 1
    return added, deleted, first_line


def build_file_change(
    path: str,
    operation: str,
    before: bytes | str | None,
    after: bytes | str | None,
) -> dict[str, Any]:
    """Build one bounded unified-diff record from before/after snapshots."""

    normalized_path = str(path or "").replace("\\", "/").strip()
    normalized_operation = str(operation or "update").strip().lower()
    if normalized_operation not in {"add", "update", "delete"}:
        normalized_operation = "update"
    # 命令可能生成超大文件；在解码和 difflib 计算前收敛，避免侧栏元数据反向拖垮运行。
    if any(
        value is not None and len(value) > MAX_CHANGE_SOURCE_BYTES
        for value in (before, after)
    ):
        return {
            "path": normalized_path,
            "operation": normalized_operation,
            "added_lines": 0,
            "deleted_lines": 0,
            "line_count": None,
            "first_changed_line": None,
            "diff": "",
            "diff_truncated": True,
            "binary": False,
        }
    old_text, old_binary = _decode(before)
    new_text, new_binary = _decode(after)
    if old_binary or new_binary:
        return {
            "path": normalized_path,
            "operation": normalized_operation,
            "added_lines": 0,
            "deleted_lines": 0,
            "line_count": len((new_text or "").splitlines()) if new_text is not None else None,
            "first_changed_line": None,
            "diff": "",
            "binary": True,
        }

    # difflib 会把无末尾换行的删除/新增内容粘在同一行；统一补行尾后再生成 UI diff。
    old_lines = [f"{line}\n" for line in (old_text or "").splitlines()]
    new_lines = [f"{line}\n" for line in (new_text or "").splitlines()]
    from_name = f"a/{normalized_path}" if before is not None else "/dev/null"
    to_name = f"b/{normalized_path}" if after is not None else "/dev/null"
    full_diff = "".join(unified_diff(old_lines, new_lines, fromfile=from_name, tofile=to_name, n=3))
    added, deleted, first_line = _line_counts(full_diff)
    return {
        "path": normalized_path,
        "operation": normalized_operation,
        "added_lines": added,
        "deleted_lines": deleted,
        "line_count": len(new_lines) if after is not None else 0,
        "first_changed_line": first_line,
        "diff": full_diff[:MAX_CHANGE_DIFF_CHARS],
        "diff_truncated": len(full_diff) > MAX_CHANGE_DIFF_CHARS,
        "binary": False,
    }


def sanitize_change_set(value: object) -> dict[str, Any] | None:
    """Project change metadata into the public event/message contract."""

    if not isinstance(value, Mapping):
        return None
    safe_files: list[dict[str, Any]] = []
    remaining_diff_chars = MAX_CHANGE_SET_DIFF_CHARS
    raw_files = value.get("files")
    if isinstance(raw_files, list):
        for raw in raw_files[:MAX_CHANGE_FILES]:
            if not isinstance(raw, Mapping):
                continue
            path = str(raw.get("path") or "").replace("\\", "/").strip()
            if not path or path.startswith("/") or re.match(r"^[A-Za-z]:/", path) or "../" in f"{path}/":
                continue
            operation = str(raw.get("operation") or raw.get("kind") or "update").lower()
            if operation not in {"add", "update", "delete"}:
                operation = "update"
            item: dict[str, Any] = {"path": path[:1_000], "operation": operation}
            for key in ("added_lines", "deleted_lines", "line_count", "first_changed_line"):
                raw_value = raw.get(key)
                if isinstance(raw_value, int) and not isinstance(raw_value, bool):
                    item[key] = max(0, min(raw_value, 10_000_000))
            for key in ("binary", "diff_truncated"):
                if isinstance(raw.get(key), bool):
                    item[key] = raw[key]
            diff = raw.get("diff")
            if isinstance(diff, str) and diff and remaining_diff_chars > 0:
                limit = min(MAX_CHANGE_DIFF_CHARS, remaining_diff_chars)
                item["diff"] = diff[:limit]
                if len(diff) > limit:
                    item["diff_truncated"] = True
                remaining_diff_chars -= len(item["diff"])
            safe_files.append(item)
    if not safe_files:
        return None
    added = sum(int(item.get("added_lines") or 0) for item in safe_files)
    deleted = sum(int(item.get("deleted_lines") or 0) for item in safe_files)
    result: dict[str, Any] = {
        "status": str(value.get("status") or "observed")[:32],
        "source": str(value.get("source") or "coding")[:64],
        "file_count": len(safe_files),
        "added_lines": added,
        "deleted_lines": deleted,
        "files": safe_files,
    }
    return result


def aggregate_change_sets(values: list[object]) -> dict[str, Any] | None:
    """Merge repeated edits into one final-message summary while keeping latest diff."""

    files: dict[str, dict[str, Any]] = {}
    source = "coding"
    status = "observed"
    retained_diff_chars = 0
    for value in values:
        change_set = sanitize_change_set(value)
        if not change_set:
            continue
        source = str(change_set.get("source") or source)
        status = str(change_set.get("status") or status)
        for item in change_set["files"]:
            path = str(item["path"])
            existing = files.get(path)
            if existing is None:
                if len(files) >= MAX_CHANGE_FILES:
                    continue
                retained = dict(item)
                diff = retained.get("diff")
                if isinstance(diff, str):
                    limit = max(0, MAX_CHANGE_SET_DIFF_CHARS - retained_diff_chars)
                    retained["diff"] = diff[:limit]
                    if len(diff) > limit:
                        retained["diff_truncated"] = True
                    retained_diff_chars += len(retained["diff"])
                files[path] = retained
                continue
            for key in ("added_lines", "deleted_lines"):
                existing[key] = int(existing.get(key) or 0) + int(item.get(key) or 0)
            for key in ("operation", "line_count", "first_changed_line", "binary", "diff_truncated"):
                if key in item:
                    existing[key] = item[key]
            if isinstance(item.get("diff"), str):
                retained_diff_chars -= len(str(existing.get("diff") or ""))
                limit = max(0, MAX_CHANGE_SET_DIFF_CHARS - retained_diff_chars)
                latest_diff = str(item["diff"])
                existing["diff"] = latest_diff[:limit]
                if len(latest_diff) > limit:
                    existing["diff_truncated"] = True
                retained_diff_chars += len(existing["diff"])
    if not files:
        return None
    result_files = list(files.values())
    # 聚合结果再次经过同一公共契约，统一执行文件数和总 diff 容量限制。
    return sanitize_change_set({
        "status": status,
        "source": source,
        "file_count": len(result_files),
        "added_lines": sum(int(item.get("added_lines") or 0) for item in result_files),
        "deleted_lines": sum(int(item.get("deleted_lines") or 0) for item in result_files),
        "files": result_files,
    })


__all__ = [
    "MAX_CHANGE_SOURCE_BYTES",
    "aggregate_change_sets",
    "build_file_change",
    "sanitize_change_set",
]
