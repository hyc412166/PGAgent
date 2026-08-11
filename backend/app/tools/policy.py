"""Permission policy shared by the runtime tool registry.

The UI exposes three intentionally small modes.  They control whether a tool
call needs a human decision; they never weaken the workspace sandbox, command
allowlist, or network SSRF checks implemented by the tools themselves.
"""

from __future__ import annotations

from typing import Final


PERMISSION_ASK: Final = "ask"
PERMISSION_SMART: Final = "smart"
PERMISSION_FULL: Final = "full"
DEFAULT_PERMISSION_MODE: Final = PERMISSION_SMART


_MODE_ALIASES: Final[dict[str, str]] = {
    "ask": PERMISSION_ASK,
    "request": PERMISSION_ASK,
    "request_approval": PERMISSION_ASK,
    "confirm": PERMISSION_ASK,
    "approval": PERMISSION_ASK,
    "smart": PERMISSION_SMART,
    "intelligent": PERMISSION_SMART,
    "smart_approval": PERMISSION_SMART,
    "full": PERMISSION_FULL,
    "full_access": PERMISSION_FULL,
    "unrestricted": PERMISSION_FULL,
}

# Mutates local state, starts a local process, or asks another runtime to act.
HIGH_IMPACT_TOOLS: Final[frozenset[str]] = frozenset(
    {
        "write",
        "write_file",
        "edit",
        "bash",
        "run_command",
        "task",
    }
)

NETWORK_TOOLS: Final[frozenset[str]] = frozenset({"webfetch", "websearch"})


def normalize_permission_mode(value: object) -> str:
    """Return one supported permission mode, safely defaulting to smart."""

    candidate = str(value or "").strip().lower()
    return _MODE_ALIASES.get(candidate, DEFAULT_PERMISSION_MODE)


def approval_required(tool_name: str, permission_mode: object, *, approved: bool) -> bool:
    """Whether this specific call must pause for a user decision.

    ``approved`` is set only by the persisted approval-resume path.  It is not
    inferred from model arguments, so a model cannot grant itself permission.
    """

    if approved:
        return False
    mode = normalize_permission_mode(permission_mode)
    if mode == PERMISSION_FULL:
        return False
    if tool_name in HIGH_IMPACT_TOOLS:
        return True
    return mode == PERMISSION_ASK and tool_name in NETWORK_TOOLS


def approval_reason(tool_name: str, permission_mode: object) -> str:
    """Human-readable, accurate reason for an approval prompt."""

    mode = normalize_permission_mode(permission_mode)
    if tool_name in NETWORK_TOOLS:
        return "该操作将访问互联网并读取外部内容，需要你的批准。"
    if tool_name in {"bash", "run_command"}:
        return "该命令会以当前用户权限在工作区中启动受控进程，需要你的批准。"
    if tool_name in {"write", "write_file", "edit"}:
        return "该操作会修改工作区中的文件，需要你的批准。"
    if tool_name == "task":
        return "该操作会委派任务给其他 Agent，可能触发其已配置的能力，需要你的批准。"
    if mode == PERMISSION_ASK:
        return "该操作在“请求批准”模式下需要你的批准。"
    return "该操作需要你的批准。"
