"""Deterministic approval policy for a single PGAgent tool call.

Permission modes decide whether a call pauses for a human decision.  They do
not weaken the tool-level boundaries: the workspace sandbox, command
allowlist, shell-token rejection, and public-network checks are enforced by
the tools themselves.

``smart`` deliberately evaluates the *actual call*, rather than treating a
whole tool as dangerous.  This keeps ordinary source edits and common project
checks fluid while escalating destructive, sensitive, broad, or ambiguous
operations to the user.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import os
from pathlib import Path
import re
import shlex
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

# Calls with an observable side effect or an external network boundary.  In
# request-approval mode, every one of these is confirmed individually.
SIDE_EFFECT_OR_NETWORK_TOOLS: Final[frozenset[str]] = frozenset(
    {
        "write",
        "write_file",
        "edit",
        "apply_patch",
        "delete",
        "bash",
        "run_command",
        "validate",
        "validate_baseline",
        "task",
        "Agent",
        "webfetch",
        "websearch",
        "WebFetch",
        "WebSearch",
        "edit_file",
        "NotebookEdit",
        "PowerShell",
        "REPL",
        "RemoteTrigger",
        "MCP",
        "MemoryWrite",
        "TodoWrite",
        "todowrite",
        "Config",
        "EnterPlanMode",
        "ExitPlanMode",
        "TaskCreate",
        "RunTaskPacket",
        "TaskStop",
        "TaskUpdate",
        "task_create",
        "task_update",
        "claim_task",
        "background_run",
        "write_stdin",
        "spawn_teammate",
        "send_message",
        "read_inbox",
        "broadcast",
        "shutdown_request",
        "integrate_teammate",
        "plan_approval",
        "TeamCreate",
        "TeamDelete",
        "WorkerCreate",
        "WorkerObserve",
        "WorkerResolveTrust",
        "WorkerSendPrompt",
        "WorkerRestart",
        "WorkerTerminate",
        "WorkerObserveCompletion",
        "CronCreate",
        "CronDelete",
    }
)

# Kept as a public compatibility name for callers that previously imported it.
HIGH_IMPACT_TOOLS: Final[frozenset[str]] = SIDE_EFFECT_OR_NETWORK_TOOLS
NETWORK_TOOLS: Final[frozenset[str]] = frozenset({"webfetch", "websearch", "WebFetch", "WebSearch", "RemoteTrigger", "MCP"})
FILE_WRITE_TOOLS: Final[frozenset[str]] = frozenset({"write", "write_file", "edit", "edit_file"})
FILE_DELETE_TOOLS: Final[frozenset[str]] = frozenset({"delete"})
COMMAND_TOOLS: Final[frozenset[str]] = frozenset({"bash", "run_command", "validate", "validate_baseline", "PowerShell", "REPL", "background_run"})

_SENSITIVE_FILE_NAMES: Final[frozenset[str]] = frozenset(
    {
        ".npmrc",
        ".pypirc",
        ".netrc",
        "credentials",
        "credential",
        "secrets",
        "secret",
        "id_rsa",
        "id_ed25519",
        "authorized_keys",
        "known_hosts",
    }
)
_SENSITIVE_SUFFIXES: Final[tuple[str, ...]] = (
    ".pem",
    ".key",
    ".p12",
    ".pfx",
    ".kdbx",
    ".keystore",
)
_CONTROL_PLANE_FILE_NAMES: Final[frozenset[str]] = frozenset(
    {
        "package.json",
        "package-lock.json",
        "pnpm-lock.yaml",
        "yarn.lock",
        "bun.lock",
        "bun.lockb",
        "pyproject.toml",
        "pipfile",
        "pipfile.lock",
        "poetry.lock",
        "setup.py",
        "setup.cfg",
        "tox.ini",
        "cargo.toml",
        "cargo.lock",
        "go.mod",
        "go.sum",
        "gemfile",
        "gemfile.lock",
        "composer.json",
        "composer.lock",
        "dockerfile",
        "docker-compose.yml",
        "docker-compose.yaml",
        "compose.yml",
        "compose.yaml",
        "jenkinsfile",
        "makefile",
        "taskfile.yml",
        "taskfile.yaml",
        ".gitlab-ci.yml",
        "azure-pipelines.yml",
    }
)
_CONTROL_PLANE_PREFIXES: Final[tuple[str, ...]] = (
    ".github/workflows/",
    ".github/actions/",
    ".gitlab/",
    ".circleci/",
    ".azure-pipelines/",
    ".husky/",
    "scripts/",
    "bin/",
)
_SCRIPT_SUFFIXES: Final[tuple[str, ...]] = (".sh", ".bash", ".zsh", ".ps1", ".bat", ".cmd")
_SHELL_CONTROL_TOKENS: Final[tuple[str, ...]] = ("&&", "||", ";", "|", ">", "<", "`", "$(")
_SAFE_NPM_SCRIPTS: Final[frozenset[str]] = frozenset(
    {"test", "lint", "typecheck", "check", "build", "format", "format:check"}
)
_SAFE_PYTHON_MODULES: Final[frozenset[str]] = frozenset({"pytest", "unittest", "compileall"})
_SAFE_GIT_SUBCOMMANDS: Final[frozenset[str]] = frozenset(
    {"status", "diff", "log", "show", "ls-files", "grep", "rev-parse", "cat-file"}
)
_SENSITIVE_CONTENT_PATTERNS: Final[tuple[re.Pattern[str], ...]] = (
    re.compile(r"-----BEGIN(?: [A-Z]+)? PRIVATE KEY-----"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bghp_[A-Za-z0-9]{30,}\b"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"),
)

_LARGE_WRITE_CHARS: Final = 256_000
_VERY_LARGE_EXISTING_FILE_BYTES: Final = 2_000_000
_BROAD_REWRITE_MIN_BYTES: Final = 64_000


@dataclass(frozen=True, slots=True)
class ToolRiskDecision:
    """A deterministic, explainable decision for one exact tool call."""

    requires_approval: bool
    reason: str = ""
    risk_level: str = "low"


def normalize_permission_mode(value: object) -> str:
    """Return one supported permission mode, safely defaulting to smart."""

    candidate = str(value or "").strip().lower()
    return _MODE_ALIASES.get(candidate, DEFAULT_PERMISSION_MODE)


def _allow() -> ToolRiskDecision:
    return ToolRiskDecision(False)


def _approval(reason: str, *, risk_level: str = "high") -> ToolRiskDecision:
    return ToolRiskDecision(True, reason, risk_level)


def _normalized_relative_path(value: object) -> str:
    """Normalize only for classification; real access stays in WorkspaceSandbox."""

    raw = str(value or "").strip().replace("\\", "/")
    parts: list[str] = []
    for part in raw.split("/"):
        if not part or part == ".":
            continue
        if part == ".." and parts and parts[-1] != "..":
            parts.pop()
            continue
        parts.append(part)
    return "/".join(parts).casefold()


def _file_path_risk(path: object) -> str | None:
    normalized = _normalized_relative_path(path)
    if not normalized:
        return None
    parts = tuple(part for part in normalized.split("/") if part)
    filename = parts[-1]
    if filename == ".env" or filename.startswith(".env."):
        return "目标是环境变量文件"
    if filename in _SENSITIVE_FILE_NAMES or filename.endswith(_SENSITIVE_SUFFIXES):
        return "目标可能包含凭据、密钥或证书"
    if any(part in {".ssh", ".aws", ".gnupg"} for part in parts):
        return "目标位于凭据或签名配置目录"
    if normalized.startswith(_CONTROL_PLANE_PREFIXES):
        return "目标是自动化、启动或钩子脚本"
    if filename.startswith("requirements") and filename.endswith(".txt"):
        return "目标是依赖配置文件"
    if filename in _CONTROL_PLANE_FILE_NAMES:
        return "目标是依赖、构建或部署配置文件"
    if filename.endswith(_SCRIPT_SUFFIXES):
        return "目标是可执行脚本"
    return None


def _workspace_path_for_risk(
    workspace_root: str | Path | None,
    path: object,
) -> object:
    if workspace_root is None:
        return path
    raw_path = str(path or "").strip()
    candidate_input = Path(raw_path)
    if not raw_path or candidate_input.is_absolute():
        return path
    try:
        root = Path(workspace_root).expanduser().resolve()
        candidate = (root / candidate_input).resolve(strict=False)
        return candidate.relative_to(root).as_posix()
    except (OSError, ValueError):
        return path


def _existing_file_size(workspace_root: str | Path | None, path: object) -> int | None:
    """Read only file metadata after independently checking the sandbox boundary."""

    if workspace_root is None:
        return None
    raw_path = str(path or "").strip()
    if not raw_path:
        return None
    candidate_input = Path(raw_path)
    if candidate_input.is_absolute():
        return None
    try:
        root = Path(workspace_root).expanduser().resolve()
        candidate = (root / candidate_input).resolve(strict=False)
        candidate.relative_to(root)
        if candidate.is_file():
            return candidate.stat().st_size
    except (OSError, ValueError):
        return None
    return None


def _content_contains_secret(value: str) -> bool:
    return any(pattern.search(value) is not None for pattern in _SENSITIVE_CONTENT_PATTERNS)


def _write_decision(
    tool_name: str,
    arguments: Mapping[str, object],
    workspace_root: str | Path | None,
) -> ToolRiskDecision:
    path = arguments.get("path")
    path = _workspace_path_for_risk(workspace_root, path)
    path_risk = _file_path_risk(path)
    if path_risk:
        return _approval(f"{path_risk}，智能审批需要你确认这次文件修改。")

    if tool_name in {"edit", "edit_file"}:
        old_string = arguments.get("old_string")
        new_string = arguments.get("new_string")
        if not isinstance(old_string, str) or not isinstance(new_string, str):
            return _allow()
        if _content_contains_secret(new_string):
            return _approval("修改内容看起来包含访问密钥或私钥，智能审批需要你确认。")
        if max(len(old_string), len(new_string)) > _LARGE_WRITE_CHARS:
            return _approval("单次替换内容过大，智能审批需要你确认影响范围。")
        if not new_string and len(old_string) >= _BROAD_REWRITE_MIN_BYTES:
            return _approval("这次编辑会删除大段已有内容，智能审批需要你确认。")
        existing_size = _existing_file_size(workspace_root, path)
        if bool(arguments.get("replace_all")) and existing_size and existing_size >= _VERY_LARGE_EXISTING_FILE_BYTES:
            return _approval("将在超大文件中执行全量替换，智能审批需要你确认影响范围。")
        return _allow()

    content = arguments.get("content")
    if not isinstance(content, str):
        return _allow()
    if _content_contains_secret(content):
        return _approval("写入内容看起来包含访问密钥或私钥，智能审批需要你确认。")
    if len(content) > _LARGE_WRITE_CHARS:
        return _approval("单次写入内容过大，智能审批需要你确认影响范围。")
    existing_size = _existing_file_size(workspace_root, path)
    if existing_size is not None:
        if not content and existing_size > 0:
            return _approval("这次写入会清空已有文件，智能审批需要你确认。")
        if existing_size >= _VERY_LARGE_EXISTING_FILE_BYTES:
            return _approval("这次写入会覆盖超大已有文件，智能审批需要你确认。")
        if existing_size >= _BROAD_REWRITE_MIN_BYTES and len(content.encode("utf-8")) * 5 < existing_size:
            return _approval("这次写入会显著缩小已有文件，智能审批需要你确认。")
    return _allow()


def _patch_decision(
    arguments: Mapping[str, object],
    workspace_root: str | Path | None,
) -> ToolRiskDecision:
    patch = arguments.get("patch")
    if not isinstance(patch, str):
        return _allow()
    if _content_contains_secret(patch):
        return _approval("补丁内容看起来包含访问密钥或私钥，智能审批需要你确认。")
    if len(patch) > _LARGE_WRITE_CHARS:
        return _approval("单次补丁内容过大，智能审批需要你确认影响范围。")
    for line in patch.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if line.startswith("*** Delete File: "):
            return _approval("补丁会删除文件，智能审批需要你确认这项不可逆操作。")
        if line.startswith(("*** Add File: ", "*** Update File: ")):
            path = line.split(": ", 1)[1]
            path = _workspace_path_for_risk(workspace_root, path)
            path_risk = _file_path_risk(path)
            if path_risk:
                return _approval(f"{path_risk}，智能审批需要你确认这次补丁。")
    return _allow()


def _command_parts(value: object) -> list[str] | None:
    if isinstance(value, (list, tuple)):
        if not value or any(not isinstance(item, str) for item in value):
            return None
        parts = [item.strip() for item in value]
        joined = " ".join(parts)
    elif isinstance(value, str):
        joined = value.strip()
        if not joined:
            return None
        if any(token in joined for token in _SHELL_CONTROL_TOKENS):
            return None
        try:
            parts = shlex.split(joined, posix=os.name != "nt")
        except ValueError:
            return None
    else:
        return None
    if not parts or not parts[0] or any(token in joined for token in _SHELL_CONTROL_TOKENS):
        return None
    return parts


def _command_decision(arguments: Mapping[str, object]) -> ToolRiskDecision:
    parts = _command_parts(arguments.get("command"))
    if parts is None:
        return _approval("命令无法可靠解析或包含 shell 控制符，智能审批需要你确认。")
    executable = parts[0].casefold()
    if any(marker in executable for marker in ("/", "\\", ":")):
        return _approval("命令指定了可执行文件路径，智能审批需要你确认。")

    if executable in {"pytest", "pytest.exe", "rg", "rg.exe"}:
        return _allow()
    if executable in {"python", "python.exe"}:
        if len(parts) == 2 and parts[1] in {"--version", "-V", "--help"}:
            return _allow()
        if len(parts) >= 3 and parts[1] == "-m" and parts[2].casefold() in _SAFE_PYTHON_MODULES:
            return _allow()
        return _approval("该 Python 调用可执行任意代码，智能审批需要你确认。")
    if executable in {"node", "node.exe"}:
        if len(parts) == 2 and parts[1] in {"--version", "-v", "--help"}:
            return _allow()
        return _approval("该 Node 调用可执行任意脚本，智能审批需要你确认。")
    if executable in {"npm", "npm.cmd"}:
        if len(parts) == 2 and parts[1] in {"--version", "-v", "--help", "help"}:
            return _allow()
        if len(parts) >= 2 and parts[1].casefold() == "test":
            return _allow()
        if len(parts) >= 3 and parts[1].casefold() == "run" and parts[2].casefold() in _SAFE_NPM_SCRIPTS:
            return _allow()
        return _approval("该 npm 操作可能安装依赖、运行生命周期脚本或改变项目状态，智能审批需要你确认。")
    if executable in {"git", "git.exe"}:
        if len(parts) < 2 or parts[1].startswith("-"):
            return _approval("Git 操作无法确认是只读检查，智能审批需要你确认。")
        subcommand = parts[1].casefold()
        unsafe_diff_flags = {"--no-index", "--ext-diff", "--textconv", "--output"}
        if subcommand in _SAFE_GIT_SUBCOMMANDS and not any(item.casefold() in unsafe_diff_flags for item in parts[2:]):
            return _allow()
        return _approval("该 Git 操作可能改写工作区、历史或远端，智能审批需要你确认。")
    return _approval("该命令的副作用无法可靠判断，智能审批需要你确认。")


def _smart_decision(
    tool_name: str,
    arguments: Mapping[str, object],
    workspace_root: str | Path | None,
) -> ToolRiskDecision:
    if tool_name in FILE_DELETE_TOOLS:
        return _approval("删除文件是不可逆操作，智能审批需要你确认。")
    if tool_name == "apply_patch":
        return _patch_decision(arguments, workspace_root)
    if tool_name in FILE_WRITE_TOOLS:
        return _write_decision(tool_name, arguments, workspace_root)
    if tool_name in COMMAND_TOOLS:
        return _command_decision(arguments)
    if tool_name in SIDE_EFFECT_OR_NETWORK_TOOLS:
        return _approval(
            "该高级工具会修改持久状态、调用子 Agent 或访问外部网络，智能审批需要你确认。"
        )
    return _allow()


def assess_tool_call(
    tool_name: str,
    permission_mode: object,
    *,
    arguments: Mapping[str, object] | None = None,
    approved: bool,
    workspace_root: str | Path | None = None,
) -> ToolRiskDecision:
    """Classify one exact call without delegating the decision to a model.

    A persisted approval resume passes ``approved=True`` only after the exact
    tool name and arguments were matched by the runtime.  Model-supplied input
    can never set that flag itself.
    """

    if approved:
        return _allow()
    mode = normalize_permission_mode(permission_mode)
    normalized_name = str(tool_name or "").strip()
    payload: Mapping[str, object] = arguments if isinstance(arguments, Mapping) else {}
    if mode == PERMISSION_FULL:
        return _allow()
    if mode == PERMISSION_ASK:
        if normalized_name in SIDE_EFFECT_OR_NETWORK_TOOLS:
            return _approval("请求批准模式：该操作会修改状态、调用子 Agent 或访问外部网络，需要你确认。")
        return _allow()
    return _smart_decision(normalized_name, payload, workspace_root)


def approval_required(
    tool_name: str,
    permission_mode: object,
    *,
    arguments: Mapping[str, object] | None = None,
    approved: bool,
    workspace_root: str | Path | None = None,
) -> bool:
    """Compatibility helper returning only the decision boolean."""

    return assess_tool_call(
        tool_name,
        permission_mode,
        arguments=arguments,
        approved=approved,
        workspace_root=workspace_root,
    ).requires_approval


def approval_reason(
    tool_name: str,
    permission_mode: object,
    *,
    arguments: Mapping[str, object] | None = None,
    workspace_root: str | Path | None = None,
) -> str:
    """Return the same explainable reason used for the matching decision."""

    decision = assess_tool_call(
        tool_name,
        permission_mode,
        arguments=arguments,
        approved=False,
        workspace_root=workspace_root,
    )
    return decision.reason or "该操作需要你确认。"
