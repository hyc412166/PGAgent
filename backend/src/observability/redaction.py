"""把诊断字段压缩为可观察但不包含凭据的安全摘要。"""

from __future__ import annotations

import re
import shlex
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any


REDACTED = "[REDACTED]"

# 字段名按词元和明确复合名匹配，避免把 input_tokens 等用量字段误判为凭据。
_SECRET_FIELD_PARTS = frozenset(
    {"token", "password", "passwd", "pwd", "secret", "credential", "authorization", "cookie", "headers"}
)
_SECRET_FIELD_COMPOUNDS = ("api_key", "apikey", "access_key", "private_key")
_SAFE_TOKEN_METRIC_FIELDS = frozenset({"token_count"})

# 文本模式只替换值，不删除键名或 URL 的非敏感部分，便于定位问题。
_BEARER_RE = re.compile(r"(?i)(\bbearer\s+)([^\s,;]+)")
_JWT_RE = re.compile(r"\beyJ[A-Za-z0-9_-]{2,}\.[A-Za-z0-9_-]{2,}\.[A-Za-z0-9_-]{2,}\b")
_URL_QUERY_RE = re.compile(
    r"(?i)([?&](?:api[-_]?key|access[-_]?token|authorization|token|password|passwd|pwd|secret|client[-_]?secret|cookie)\s*=\s*)([^&#\s]+)"
)
_URL_PASSWORD_RE = re.compile(r"(?i)(://[^/\s:@]+:)([^@\s/]+)(@)")
_ASSIGNMENT_RE = re.compile(
    r"(?i)(?P<key>\b[A-Za-z_][A-Za-z0-9_-]*\b)(?P<separator>\s*=\s*)"
    r"(?P<value>(?:bearer|basic)\s+[^\s,;&]+|\"[^\"]*\"|'[^']*'|[^\s,;&]+)"
)
_COLON_SECRET_RE = re.compile(
    r"(?i)(?P<prefix>[\"']?(?:api[-_]?key|apikey|access[-_]?key|private[-_]?key|token|password|passwd|pwd|secret|credential|authorization|cookie|headers)[\"']?\s*:\s*)"
    r"(?:basic\s+)?(?:\"[^\"]*\"|'[^']*'|[^\s,;}\]]+)"
)


def _is_secret_field(key: object) -> bool:
    normalized = str(key).casefold().replace("-", "_").replace(" ", "_")
    if normalized in _SAFE_TOKEN_METRIC_FIELDS:
        return False
    if any(compound in normalized for compound in _SECRET_FIELD_COMPOUNDS):
        return True
    return any(part in _SECRET_FIELD_PARTS for part in normalized.split("_"))


def redact_text(value: str, *, limit: int = 2000) -> str:
    """脱敏自由文本并限制长度，避免错误消息成为凭据泄漏通道。"""

    if limit <= 0:
        return ""

    text = value.replace("\x00", "")
    text = _URL_QUERY_RE.sub(r"\1" + REDACTED, text)
    text = _URL_PASSWORD_RE.sub(r"\1" + REDACTED + r"\3", text)

    def _assignment_replacement(match: re.Match[str]) -> str:
        key = match.group("key")
        if not _is_secret_field(key):
            return match.group(0)
        return f"{key}{match.group('separator')}{REDACTED}"

    text = _COLON_SECRET_RE.sub(r"\g<prefix>" + REDACTED, text)
    text = _ASSIGNMENT_RE.sub(_assignment_replacement, text)
    text = _BEARER_RE.sub(r"\1" + REDACTED, text)
    text = _JWT_RE.sub(REDACTED, text)
    return text[:limit]


def _redact_value(value: Any, *, max_text_chars: int) -> Any:
    if isinstance(value, Mapping):
        return {
            key: REDACTED if _is_secret_field(key) else _redact_value(item, max_text_chars=max_text_chars)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact_value(item, max_text_chars=max_text_chars) for item in value]
    if isinstance(value, tuple):
        return tuple(_redact_value(item, max_text_chars=max_text_chars) for item in value)
    if isinstance(value, str):
        return redact_text(value, limit=max_text_chars)
    return value


def redact_mapping(value: Mapping[str, Any], *, max_text_chars: int = 2000) -> dict[str, Any]:
    """递归脱敏映射、列表和文本值，保留非敏感标量。"""

    return _redact_value(value, max_text_chars=max_text_chars)


def _command_parts(command: Sequence[str] | str) -> list[str]:
    if isinstance(command, str):
        try:
            return shlex.split(command)
        except ValueError:
            # 不尝试记录无法解析的参数正文；只按空白取得一个安全的可执行文件名。
            return command.split()
    return [str(part) for part in command]


def _executable_name(value: str) -> str:
    normalized = value.strip().strip("\"'").replace("\\", "/")
    return normalized.rsplit("/", 1)[-1]


def _relative_cwd(cwd: str | None, workspace_root: str | None) -> str | None:
    if not cwd:
        return None
    if not workspace_root:
        return "."

    cwd_path = Path(cwd).resolve()
    root_path = Path(workspace_root).resolve()
    try:
        relative = cwd_path.relative_to(root_path)
    except ValueError:
        return "<outside>"
    return relative.as_posix() or "."


def summarize_command(
    command: Sequence[str] | str,
    *,
    cwd: str | None,
    workspace_root: str | None,
) -> dict[str, Any]:
    """只返回可执行文件名、参数数量和工作区相对目录。"""

    parts = _command_parts(command)
    return {
        "executable": _executable_name(parts[0]) if parts else "",
        "argument_count": max(0, len(parts) - 1),
        "cwd": _relative_cwd(cwd, workspace_root),
    }
