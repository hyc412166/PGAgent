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
# 文件职责：负责工具定义、授权、注册、调度与执行中的 policy 子模块。
# 逻辑关系：上层通过 tools/policy.py 使用本模块；本模块把处理结果交给同领域服务、持久化层或 API 响应层。

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import os
from pathlib import Path
import re
import shlex
from typing import Final


# 变量说明：PERMISSION_ASK 表示当前步骤使用的 PERMISSION_ASK 值。
PERMISSION_ASK: Final = "ask"
# 变量说明：PERMISSION_SMART 表示当前步骤使用的 PERMISSION_SMART 值。
PERMISSION_SMART: Final = "smart"
# 变量说明：PERMISSION_FULL 表示当前步骤使用的 PERMISSION_FULL 值。
PERMISSION_FULL: Final = "full"
# 变量说明：DEFAULT_PERMISSION_MODE 表示当前步骤使用的 DEFAULT_PERMISSION_MODE 值。
DEFAULT_PERMISSION_MODE: Final = PERMISSION_SMART


# 变量说明：_MODE_ALIASES 表示当前流程使用的 _MODE_ALIASES 集合。
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
# 变量说明：SIDE_EFFECT_OR_NETWORK_TOOLS 表示当前流程使用的 SIDE_EFFECT_OR_NETWORK_TOOLS 集合。
SIDE_EFFECT_OR_NETWORK_TOOLS: Final[frozenset[str]] = frozenset(
    {
        "write",
        "write_file",
        "edit",
        "apply_patch",
        "delete",
        "bash",
        "shell",
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
# 变量说明：HIGH_IMPACT_TOOLS 表示当前流程使用的 HIGH_IMPACT_TOOLS 集合。
HIGH_IMPACT_TOOLS: Final[frozenset[str]] = SIDE_EFFECT_OR_NETWORK_TOOLS
# 变量说明：NETWORK_TOOLS 表示当前流程使用的 NETWORK_TOOLS 集合。
NETWORK_TOOLS: Final[frozenset[str]] = frozenset({"webfetch", "websearch", "WebFetch", "WebSearch", "RemoteTrigger", "MCP"})
# 变量说明：FILE_WRITE_TOOLS 表示当前流程使用的 FILE_WRITE_TOOLS 集合。
FILE_WRITE_TOOLS: Final[frozenset[str]] = frozenset({"write", "write_file", "edit", "edit_file"})
# 变量说明：FILE_DELETE_TOOLS 表示当前流程使用的 FILE_DELETE_TOOLS 集合。
FILE_DELETE_TOOLS: Final[frozenset[str]] = frozenset({"delete"})
# 变量说明：COMMAND_TOOLS 表示当前流程使用的 COMMAND_TOOLS 集合。
COMMAND_TOOLS: Final[frozenset[str]] = frozenset({"bash", "shell", "run_command", "validate", "validate_baseline", "PowerShell", "REPL", "background_run"})

# 变量说明：_SENSITIVE_FILE_NAMES 表示当前流程使用的 _SENSITIVE_FILE_NAMES 集合。
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
# 变量说明：_SENSITIVE_SUFFIXES 表示当前流程使用的 _SENSITIVE_SUFFIXES 集合。
_SENSITIVE_SUFFIXES: Final[tuple[str, ...]] = (
    ".pem",
    ".key",
    ".p12",
    ".pfx",
    ".kdbx",
    ".keystore",
)
# 变量说明：_CONTROL_PLANE_FILE_NAMES 表示当前流程使用的 _CONTROL_PLANE_FILE_NAMES 集合。
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
# 变量说明：_CONTROL_PLANE_PREFIXES 表示当前流程使用的 _CONTROL_PLANE_PREFIXES 集合。
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
# 变量说明：_SCRIPT_SUFFIXES 表示当前流程使用的 _SCRIPT_SUFFIXES 集合。
_SCRIPT_SUFFIXES: Final[tuple[str, ...]] = (".sh", ".bash", ".zsh", ".ps1", ".bat", ".cmd")
# 变量说明：_SHELL_CONTROL_TOKENS 表示当前流程使用的 _SHELL_CONTROL_TOKENS 集合。
_SHELL_CONTROL_TOKENS: Final[tuple[str, ...]] = ("&&", "||", ";", "|", ">", "<", "`", "$(")
# 变量说明：_SAFE_NPM_SCRIPTS 表示当前流程使用的 _SAFE_NPM_SCRIPTS 集合。
_SAFE_NPM_SCRIPTS: Final[frozenset[str]] = frozenset(
    {"test", "lint", "typecheck", "check", "build", "format", "format:check"}
)
# 变量说明：_SAFE_PYTHON_MODULES 表示当前流程使用的 _SAFE_PYTHON_MODULES 集合。
_SAFE_PYTHON_MODULES: Final[frozenset[str]] = frozenset({"pytest", "unittest", "compileall"})
# 变量说明：_SAFE_GIT_SUBCOMMANDS 表示当前流程使用的 _SAFE_GIT_SUBCOMMANDS 集合。
_SAFE_GIT_SUBCOMMANDS: Final[frozenset[str]] = frozenset(
    {"status", "diff", "log", "show", "ls-files", "grep", "rev-parse", "cat-file"}
)
# 变量说明：_SENSITIVE_CONTENT_PATTERNS 表示当前流程使用的 _SENSITIVE_CONTENT_PATTERNS 集合。
_SENSITIVE_CONTENT_PATTERNS: Final[tuple[re.Pattern[str], ...]] = (
    re.compile(r"-----BEGIN(?: [A-Z]+)? PRIVATE KEY-----"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bghp_[A-Za-z0-9]{30,}\b"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"),
)

# 变量说明：_LARGE_WRITE_CHARS 表示当前流程使用的 _LARGE_WRITE_CHARS 集合。
_LARGE_WRITE_CHARS: Final = 256_000
# 变量说明：_VERY_LARGE_EXISTING_FILE_BYTES 表示当前流程使用的 _VERY_LARGE_EXISTING_FILE_BYTES 集合。
_VERY_LARGE_EXISTING_FILE_BYTES: Final = 2_000_000
# 变量说明：_BROAD_REWRITE_MIN_BYTES 表示当前流程使用的 _BROAD_REWRITE_MIN_BYTES 集合。
_BROAD_REWRITE_MIN_BYTES: Final = 64_000


# 类职责：定义 ToolRiskDecision 在本领域中的数据与行为。
@dataclass(frozen=True, slots=True)
class ToolRiskDecision:
    """A deterministic, explainable decision for one exact tool call."""

    # 变量说明：requires_approval 表示当前步骤使用的 requires_approval 值。
    requires_approval: bool
    # 变量说明：reason 表示当前步骤使用的 reason 值。
    reason: str = ""
    # 变量说明：risk_level 表示当前步骤使用的 risk_level 值。
    risk_level: str = "low"


# 函数职责：规范化 permission_mode 对应的数据或流程。
# 参数关系：value 表示当前字段或计算值。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def normalize_permission_mode(value: object) -> str:
    """Return one supported permission mode, safely defaulting to smart."""

    # 变量说明：candidate 表示当前步骤使用的 candidate 值。
    candidate = str(value or "").strip().lower()
    return _MODE_ALIASES.get(candidate, DEFAULT_PERMISSION_MODE)


# 函数职责：完成 allow 对应的业务处理。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def _allow() -> ToolRiskDecision:
    return ToolRiskDecision(False)


# 函数职责：完成 approval 对应的业务处理。
# 参数关系：reason 表示当前步骤使用的 reason 值；risk_level 表示当前步骤使用的 risk_level 值。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def _approval(reason: str, *, risk_level: str = "high") -> ToolRiskDecision:
    return ToolRiskDecision(True, reason, risk_level)


# 函数职责：完成 normalized_relative_path 对应的业务处理。
# 参数关系：value 表示当前字段或计算值。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def _normalized_relative_path(value: object) -> str:
    """Normalize only for classification; real access stays in WorkspaceSandbox."""

    # 变量说明：raw 表示当前步骤使用的 raw 值。
    raw = str(value or "").strip().replace("\\", "/")
    # 变量说明：parts 表示当前流程使用的 parts 集合。
    parts: list[str] = []
    for part in raw.split("/"):
        if not part or part == ".":
            continue
        if part == ".." and parts and parts[-1] != "..":
            parts.pop()
            continue
        parts.append(part)
    return "/".join(parts).casefold()


# 函数职责：完成 file_path_risk 对应的业务处理。
# 参数关系：path 表示当前文件或目录路径。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def _file_path_risk(path: object) -> str | None:
    # 变量说明：normalized 表示当前步骤使用的 normalized 值。
    normalized = _normalized_relative_path(path)
    if not normalized:
        return None
    # 变量说明：parts 表示当前流程使用的 parts 集合。
    parts = tuple(part for part in normalized.split("/") if part)
    # 变量说明：filename 表示当前步骤使用的 filename 值。
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


# 函数职责：完成 workspace_path_for_risk 对应的业务处理。
# 参数关系：workspace_root 表示当前步骤使用的 workspace_root 值；path 表示当前文件或目录路径。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def _workspace_path_for_risk(
    workspace_root: str | Path | None,
    path: object,
) -> object:
    if workspace_root is None:
        return path
    # 变量说明：raw_path 表示raw_path 对应的文件系统位置。
    raw_path = str(path or "").strip()
    # 变量说明：candidate_input 表示当前步骤使用的 candidate_input 值。
    candidate_input = Path(raw_path)
    if not raw_path or candidate_input.is_absolute():
        return path
    try:
        # 变量说明：root 表示处理范围的根目录。
        root = Path(workspace_root).expanduser().resolve()
        # 变量说明：candidate 表示当前步骤使用的 candidate 值。
        candidate = (root / candidate_input).resolve(strict=False)
        return candidate.relative_to(root).as_posix()
    except (OSError, ValueError):
        return path


# 函数职责：完成 existing_file_size 对应的业务处理。
# 参数关系：workspace_root 表示当前步骤使用的 workspace_root 值；path 表示当前文件或目录路径。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def _existing_file_size(workspace_root: str | Path | None, path: object) -> int | None:
    """Read only file metadata after independently checking the sandbox boundary."""

    if workspace_root is None:
        return None
    # 变量说明：raw_path 表示raw_path 对应的文件系统位置。
    raw_path = str(path or "").strip()
    if not raw_path:
        return None
    # 变量说明：candidate_input 表示当前步骤使用的 candidate_input 值。
    candidate_input = Path(raw_path)
    if candidate_input.is_absolute():
        return None
    try:
        # 变量说明：root 表示处理范围的根目录。
        root = Path(workspace_root).expanduser().resolve()
        # 变量说明：candidate 表示当前步骤使用的 candidate 值。
        candidate = (root / candidate_input).resolve(strict=False)
        candidate.relative_to(root)
        if candidate.is_file():
            return candidate.stat().st_size
    except (OSError, ValueError):
        return None
    return None


# 函数职责：完成 content_contains_secret 对应的业务处理。
# 参数关系：value 表示当前字段或计算值。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def _content_contains_secret(value: str) -> bool:
    return any(pattern.search(value) is not None for pattern in _SENSITIVE_CONTENT_PATTERNS)


# 函数职责：完成 write_decision 对应的业务处理。
# 参数关系：tool_name 表示当前步骤使用的 tool_name 值；arguments 表示当前流程使用的 arguments 集合；workspace_root 表示当前步骤使用的 workspace_root 值。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def _write_decision(
    tool_name: str,
    arguments: Mapping[str, object],
    workspace_root: str | Path | None,
) -> ToolRiskDecision:
    # 变量说明：path 表示当前文件或目录路径。
    path = arguments.get("path")
    # 变量说明：path 表示当前文件或目录路径。
    path = _workspace_path_for_risk(workspace_root, path)
    # 变量说明：path_risk 表示当前步骤使用的 path_risk 值。
    path_risk = _file_path_risk(path)
    if path_risk:
        return _approval(f"{path_risk}，智能审批需要你确认这次文件修改。")

    if tool_name in {"edit", "edit_file"}:
        # 变量说明：old_string 表示当前步骤使用的 old_string 值。
        old_string = arguments.get("old_string")
        # 变量说明：new_string 表示当前步骤使用的 new_string 值。
        new_string = arguments.get("new_string")
        if not isinstance(old_string, str) or not isinstance(new_string, str):
            return _allow()
        if _content_contains_secret(new_string):
            return _approval("修改内容看起来包含访问密钥或私钥，智能审批需要你确认。")
        if max(len(old_string), len(new_string)) > _LARGE_WRITE_CHARS:
            return _approval("单次替换内容过大，智能审批需要你确认影响范围。")
        if not new_string and len(old_string) >= _BROAD_REWRITE_MIN_BYTES:
            return _approval("这次编辑会删除大段已有内容，智能审批需要你确认。")
        # 变量说明：existing_size 表示当前步骤使用的 existing_size 值。
        existing_size = _existing_file_size(workspace_root, path)
        if bool(arguments.get("replace_all")) and existing_size and existing_size >= _VERY_LARGE_EXISTING_FILE_BYTES:
            return _approval("将在超大文件中执行全量替换，智能审批需要你确认影响范围。")
        return _allow()

    # 变量说明：content 表示待处理或返回的正文内容。
    content = arguments.get("content")
    if not isinstance(content, str):
        return _allow()
    if _content_contains_secret(content):
        return _approval("写入内容看起来包含访问密钥或私钥，智能审批需要你确认。")
    if len(content) > _LARGE_WRITE_CHARS:
        return _approval("单次写入内容过大，智能审批需要你确认影响范围。")
    # 变量说明：existing_size 表示当前步骤使用的 existing_size 值。
    existing_size = _existing_file_size(workspace_root, path)
    if existing_size is not None:
        if not content and existing_size > 0:
            return _approval("这次写入会清空已有文件，智能审批需要你确认。")
        if existing_size >= _VERY_LARGE_EXISTING_FILE_BYTES:
            return _approval("这次写入会覆盖超大已有文件，智能审批需要你确认。")
        if existing_size >= _BROAD_REWRITE_MIN_BYTES and len(content.encode("utf-8")) * 5 < existing_size:
            return _approval("这次写入会显著缩小已有文件，智能审批需要你确认。")
    return _allow()


# 函数职责：完成 patch_decision 对应的业务处理。
# 参数关系：arguments 表示当前流程使用的 arguments 集合；workspace_root 表示当前步骤使用的 workspace_root 值。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def _patch_decision(
    arguments: Mapping[str, object],
    workspace_root: str | Path | None,
) -> ToolRiskDecision:
    # 变量说明：patch 表示当前步骤使用的 patch 值。
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
            # 变量说明：path 表示当前文件或目录路径。
            path = line.split(": ", 1)[1]
            # 变量说明：path 表示当前文件或目录路径。
            path = _workspace_path_for_risk(workspace_root, path)
            # 变量说明：path_risk 表示当前步骤使用的 path_risk 值。
            path_risk = _file_path_risk(path)
            if path_risk:
                return _approval(f"{path_risk}，智能审批需要你确认这次补丁。")
    return _allow()


# 函数职责：完成 command_parts 对应的业务处理。
# 参数关系：value 表示当前字段或计算值。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def _command_parts(value: object) -> list[str] | None:
    if isinstance(value, (list, tuple)):
        if not value or any(not isinstance(item, str) for item in value):
            return None
        # 变量说明：parts 表示当前流程使用的 parts 集合。
        parts = [item.strip() for item in value]
        # 变量说明：joined 表示当前步骤使用的 joined 值。
        joined = " ".join(parts)
    elif isinstance(value, str):
        # 变量说明：joined 表示当前步骤使用的 joined 值。
        joined = value.strip()
        if not joined:
            return None
        if any(token in joined for token in _SHELL_CONTROL_TOKENS):
            return None
        try:
            # 变量说明：parts 表示当前流程使用的 parts 集合。
            parts = shlex.split(joined, posix=os.name != "nt")
        except ValueError:
            return None
    else:
        return None
    if not parts or not parts[0] or any(token in joined for token in _SHELL_CONTROL_TOKENS):
        return None
    return parts


# 函数职责：完成 command_decision 对应的业务处理。
# 参数关系：arguments 表示当前流程使用的 arguments 集合。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def _command_decision(arguments: Mapping[str, object]) -> ToolRiskDecision:
    # 变量说明：parts 表示当前流程使用的 parts 集合。
    parts = _command_parts(arguments.get("command"))
    if parts is None:
        return _approval("命令无法可靠解析或包含 shell 控制符，智能审批需要你确认。")
    # 变量说明：executable 表示当前步骤使用的 executable 值。
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
        # 变量说明：subcommand 表示当前步骤使用的 subcommand 值。
        subcommand = parts[1].casefold()
        # 变量说明：unsafe_diff_flags 表示当前流程使用的 unsafe_diff_flags 集合。
        unsafe_diff_flags = {"--no-index", "--ext-diff", "--textconv", "--output"}
        if subcommand in _SAFE_GIT_SUBCOMMANDS and not any(item.casefold() in unsafe_diff_flags for item in parts[2:]):
            return _allow()
        return _approval("该 Git 操作可能改写工作区、历史或远端，智能审批需要你确认。")
    return _approval("该命令的副作用无法可靠判断，智能审批需要你确认。")


# 函数职责：完成 smart_decision 对应的业务处理。
# 参数关系：tool_name 表示当前步骤使用的 tool_name 值；arguments 表示当前流程使用的 arguments 集合；workspace_root 表示当前步骤使用的 workspace_root 值。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
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


# 函数职责：完成 assess_tool_call 对应的业务处理。
# 参数关系：tool_name 表示当前步骤使用的 tool_name 值；permission_mode 表示当前步骤使用的 permission_mode 值；arguments 表示当前流程使用的 arguments 集合；approved 表示当前步骤使用的 approved 值；workspace_root 表示当前步骤使用的 workspace_root 值。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
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
    # 变量说明：mode 表示当前步骤使用的 mode 值。
    mode = normalize_permission_mode(permission_mode)
    # 变量说明：normalized_name 表示当前步骤使用的 normalized_name 值。
    normalized_name = str(tool_name or "").strip()
    # 变量说明：payload 表示跨层传递的数据载荷。
    payload: Mapping[str, object] = arguments if isinstance(arguments, Mapping) else {}
    if mode == PERMISSION_FULL:
        return _allow()
    if mode == PERMISSION_ASK:
        if normalized_name in SIDE_EFFECT_OR_NETWORK_TOOLS:
            return _approval("请求批准模式：该操作会修改状态、调用子 Agent 或访问外部网络，需要你确认。")
        return _allow()
    return _smart_decision(normalized_name, payload, workspace_root)


# 函数职责：完成 approval_required 对应的业务处理。
# 参数关系：tool_name 表示当前步骤使用的 tool_name 值；permission_mode 表示当前步骤使用的 permission_mode 值；arguments 表示当前流程使用的 arguments 集合；approved 表示当前步骤使用的 approved 值；workspace_root 表示当前步骤使用的 workspace_root 值。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
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


# 函数职责：完成 approval_reason 对应的业务处理。
# 参数关系：tool_name 表示当前步骤使用的 tool_name 值；permission_mode 表示当前步骤使用的 permission_mode 值；arguments 表示当前流程使用的 arguments 集合；workspace_root 表示当前步骤使用的 workspace_root 值。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def approval_reason(
    tool_name: str,
    permission_mode: object,
    *,
    arguments: Mapping[str, object] | None = None,
    workspace_root: str | Path | None = None,
) -> str:
    """Return the same explainable reason used for the matching decision."""

    # 变量说明：decision 表示当前步骤使用的 decision 值。
    decision = assess_tool_call(
        tool_name,
        permission_mode,
        arguments=arguments,
        approved=False,
        workspace_root=workspace_root,
    )
    return decision.reason or "该操作需要你确认。"
