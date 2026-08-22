"""Tool registry, executable capability selection, and provider schemas."""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable, Iterable, Mapping
from typing import Any

from . import advanced, builtins
from .advanced_contract import ADVANCED_TOOL_SCHEMAS, CLAW_TOOL_NAMES, LEARN_TOOL_NAMES
from .policy import assess_tool_call, normalize_permission_mode
from .sandbox import WorkspaceSandbox
from .types import ApprovalRequest, ToolResult


# The familiar Claude-Code-style names are the public API.  Legacy names stay
# registered only for existing persisted runs and tests; production calls pass
# an explicit DB-backed ``allowed_tool_names`` list so unselected tools never
# appear in the provider schema.
TOOL_SCHEMAS: dict[str, dict[str, Any]] = {
    "bash": {
        "description": "在工作区中运行受控 allowlist 命令；不会启动 shell，仍受命令安全限制。",
        "parameters": {
            "type": "object",
            "properties": {
                "command": {"oneOf": [{"type": "string"}, {"type": "array", "items": {"type": "string"}}]},
                "timeout_seconds": {"type": "number"},
            },
            "required": ["command"],
        },
    },
    "read": {
        "description": "读取工作区内一个 UTF-8 文本文件。",
        "parameters": {
            "type": "object",
            "properties": {"path": {"type": "string"}, "max_chars": {"type": "integer"}},
            "required": ["path"],
        },
    },
    "write": {
        "description": "创建或覆盖工作区内文本文件。",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "content": {"type": "string"},
                "overwrite": {"type": "boolean"},
            },
            "required": ["path", "content"],
        },
    },
    "delete": {
        "description": "删除工作区内一个普通文件；不支持目录、递归删除或符号链接。",
        "parameters": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
    },
    "edit": {
        "description": "精确替换工作区文本文件中的一段内容；默认要求唯一匹配。",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "old_string": {"type": "string"},
                "new_string": {"type": "string"},
                "replace_all": {"type": "boolean"},
            },
            "required": ["path", "old_string", "new_string"],
        },
    },
    "glob": {
        "description": "按 glob pattern 查找工作区文件与目录，不会跟随越界链接。",
        "parameters": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string"},
                "path": {"type": "string"},
                "limit": {"type": "integer"},
            },
            "required": ["pattern"],
        },
    },
    "grep": {
        "description": "在工作区文本文件中用受限正则搜索内容。",
        "parameters": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string"},
                "path": {"type": "string"},
                "file_pattern": {"type": "string"},
                "case_sensitive": {"type": "boolean"},
                "limit": {"type": "integer"},
            },
            "required": ["pattern"],
        },
    },
    "webfetch": {
        "description": "抓取一个公开 http/https URL；拒绝私网/回环地址、自动重定向和超大响应。",
        "parameters": {
            "type": "object",
            "properties": {"url": {"type": "string"}, "timeout_seconds": {"type": "number"}},
            "required": ["url"],
        },
    },
    "websearch": {
        "description": "使用公开 DuckDuckGo HTML 搜索；服务不可用时会明确返回错误而不编造结果。",
        "parameters": {
            "type": "object",
            "properties": {"query": {"type": "string"}, "limit": {"type": "integer"}},
            "required": ["query"],
        },
    },
    "task": {
        "description": "把一个或多个相互独立的子任务并行委派给已启用的子 Agent。单任务使用 task+agent_id；多个任务使用 tasks 数组，系统会并行启动并在全部结束后返回结构化结果。子 Agent 只能使用它自身被勾选且不超过当前会话权限的工具，且不能再次委派。",
        "parameters": {
            "type": "object",
            "properties": {
                "task": {"type": "string"},
                "agent_id": {"type": "string"},
                "tasks": {
                    "type": "array",
                    "maxItems": 8,
                    "items": {
                        "type": "object",
                        "properties": {
                            "task": {"type": "string"},
                            "agent_id": {"type": "string"},
                        },
                        "required": ["task", "agent_id"],
                    },
                },
            },
            "oneOf": [
                {"required": ["task", "agent_id"]},
                {"required": ["tasks"]},
            ],
        },
    },
    "todowrite": {
        "description": "更新本次运行的结构化任务清单。",
        "parameters": {
            "type": "object",
            "properties": {
                "todos": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "id": {"type": "string"},
                            "content": {"type": "string"},
                            "status": {"type": "string", "enum": ["pending", "in_progress", "completed", "cancelled"]},
                            "active_form": {"type": "string"},
                        },
                        "required": ["content", "status"],
                    },
                }
            },
            "required": ["todos"],
        },
    },
    "question": {
        "description": "需要用户澄清时提出一个问题；本轮会安全结束，等待用户下一条消息回答。",
        "parameters": {
            "type": "object",
            "properties": {"question": {"type": "string"}, "context": {"type": "string"}},
            "required": ["question"],
        },
    },
    "skill": {
        "description": "加载本会话已选择 Skill 的文字指令；不会自动执行 Skill 中的脚本。",
        "parameters": {
            "type": "object",
            "properties": {"skill_id": {"type": "string"}, "name": {"type": "string"}},
        },
    },
    "git_status": {
        "description": "Read-only Git branch and working-tree status for the current workspace.",
        "parameters": {
            "type": "object",
            "properties": {
                "include_untracked": {"type": "boolean"},
                "max_chars": {"type": "integer"},
            },
        },
    },
    "git_diff": {
        "description": "Read-only, bounded Git diff for the current workspace or one relative path.",
        "parameters": {
            "type": "object",
            "properties": {
                "staged": {"type": "boolean"},
                "path": {"type": "string"},
                "max_chars": {"type": "integer"},
            },
        },
    },
    "file_info": {
        "description": "Read-only metadata for a workspace-relative file or directory.",
        "parameters": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
        },
    },
    "get_current_time": {
        "description": "获取本机当前时间和时区。",
        "parameters": {"type": "object", "properties": {"timezone_name": {"type": "string"}}},
    },
    # Compatibility names.  They are not part of the user-facing catalog.
    "list_files": {
        "description": "列出工作区内文件和目录。",
        "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "recursive": {"type": "boolean"}}},
    },
    "read_file": {
        "description": "读取工作区内 UTF-8 文本文件。",
        "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
    },
    "search_files": {
        "description": "在工作区文本文件中按普通文本搜索内容。",
        "parameters": {
            "type": "object",
            "properties": {"query": {"type": "string"}, "path": {"type": "string"}, "pattern": {"type": "string"}},
            "required": ["query"],
        },
    },
    "write_file": {
        "description": "写入工作区文件。",
        "parameters": {
            "type": "object",
            "properties": {"path": {"type": "string"}, "content": {"type": "string"}, "overwrite": {"type": "boolean"}},
            "required": ["path", "content"],
        },
    },
    "run_command": {
        "description": "在工作区中运行受控 allowlist 命令，不会启动 shell。",
        "parameters": {
            "type": "object",
            "properties": {"command": {"oneOf": [{"type": "string"}, {"type": "array", "items": {"type": "string"}}]}, "timeout_seconds": {"type": "number"}},
            "required": ["command"],
        },
    },
}

# Keep the compact PGAgent aliases above and add the exact public contracts
# exposed by the two reference implementations.
TOOL_SCHEMAS.update(ADVANCED_TOOL_SCHEMAS)


PUBLIC_TOOL_NAMES: tuple[str, ...] = (
    "bash",
    "read",
    "write",
    "delete",
    "edit",
    "glob",
    "grep",
    "webfetch",
    "websearch",
    "task",
    "todowrite",
    "question",
    "skill",
    "git_status",
    "git_diff",
    "file_info",
    *CLAW_TOOL_NAMES,
    *LEARN_TOOL_NAMES,
)
LEGACY_TOOL_NAMES: tuple[str, ...] = (
    "list_files",
    "read_file",
    "search_files",
    "get_current_time",
    "write_file",
    "run_command",
)
ALL_TOOL_NAMES: tuple[str, ...] = (*PUBLIC_TOOL_NAMES, *LEGACY_TOOL_NAMES)

# A provider batch containing only these tools is safe to execute concurrently:
# none mutates workspace/runtime state and result ordering is restored to the
# assistant's original tool-call order before messages are appended.
PARALLEL_READ_ONLY_TOOL_NAMES = frozenset({
    "read",
    "glob",
    "grep",
    "webfetch",
    "websearch",
    "skill",
    "git_status",
    "git_diff",
    "file_info",
    "get_current_time",
    "list_files",
    "read_file",
    "search_files",
    "glob_search",
    "grep_search",
    "WebFetch",
    "WebSearch",
    "Skill",
    "load_skill",
    "ToolSearch",
    "Sleep",
    "idle",
    "check_background",
    "task_get",
    "task_list",
    "list_teammates",
    "MemoryRead",
    "MemoryList",
    "TaskGet",
    "TaskList",
    "TaskOutput",
    "WorkerGet",
    "WorkerAwaitReady",
    "CronList",
    "LSP",
    "ListMcpResources",
    "ReadMcpResource",
    "TestingPermission",
    "GitStatus",
    "GitDiff",
    "GitLog",
    "GitShow",
    "GitBlame",
})


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


def _normalize_skill_instructions(value: Iterable[Mapping[str, Any] | str] | None) -> dict[str, dict[str, Any]]:
    catalog: dict[str, dict[str, Any]] = {}
    for index, raw in enumerate(value or (), start=1):
        if isinstance(raw, str):
            item = {"id": f"skill-{index}", "name": f"Skill {index}", "content": raw}
        elif isinstance(raw, Mapping):
            item = dict(raw)
        else:
            continue
        item_id = str(item.get("id") or item.get("slug") or item.get("name") or f"skill-{index}").strip()
        if not item_id:
            continue
        item["id"] = item_id
        item["name"] = str(item.get("name") or item.get("slug") or item_id).strip()
        content = str(item.get("content", item.get("instructions", "")) or "")
        item["content"] = content[:builtins.MAX_SKILL_INSTRUCTION_CHARS]
        catalog[item_id] = item
    return catalog


def _normalize_claw_todos(todos: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for raw in todos:
        item = dict(raw)
        if "activeForm" in item and "active_form" not in item:
            item["active_form"] = item.pop("activeForm")
        normalized.append(item)
    return normalized


PLAN_MODE_MUTATING_TOOLS = frozenset({
    "write", "write_file", "edit", "edit_file", "delete", "bash", "run_command",
    "PowerShell", "REPL", "NotebookEdit", "RemoteTrigger", "MCP", "MemoryWrite",
    "TodoWrite", "todowrite", "read_inbox",
    "task", "Agent", "TaskCreate", "RunTaskPacket", "TaskStop", "TaskUpdate", "task_create", "task_update",
    "claim_task", "spawn_teammate", "send_message", "broadcast", "shutdown_request",
    "plan_approval", "TeamCreate", "TeamDelete", "WorkerCreate", "WorkerObserve",
    "WorkerResolveTrust", "WorkerSendPrompt", "WorkerRestart", "WorkerTerminate",
    "WorkerObserveCompletion", "CronCreate", "CronDelete", "Config", "background_run",
})


class ToolRegistry:
    """One run's explicit capability boundary.

    A registry instance is immutable in its list of enabled tools.  It owns
    only small, JSON-serializable state (the todo list), which the coordinator
    can freeze into a run snapshot before an approval pause.
    """

    def __init__(
        self,
        sandbox: WorkspaceSandbox,
        *,
        allowed_tool_names: Iterable[str] | None = None,
        permission_mode: str = "smart",
        skill_instructions: Iterable[Mapping[str, Any] | str] | None = None,
        todo_state: Iterable[Mapping[str, Any]] | None = None,
        task_delegate: Callable[..., ToolResult | Any] | None = None,
    ) -> None:
        self.sandbox = sandbox
        self.permission_mode = normalize_permission_mode(permission_mode)
        self._tools: dict[str, Callable[..., ToolResult]] = {}
        self._known_tools = set(TOOL_SCHEMAS)
        self._task_delegate = task_delegate
        self._active_cancel_lock = threading.RLock()
        self._active_cancel_events: dict[str, threading.Event] = {}
        self._skill_instructions = _normalize_skill_instructions(skill_instructions)
        self._todo_state: list[dict[str, str]] = []
        if todo_state is not None:
            try:
                normalized = builtins._normalize_todos(list(todo_state))
            except (TypeError, ValueError):
                # A corrupt old snapshot must not make a user conversation
                # unresumable.  New writes are strictly validated below.
                normalized = []
            self._todo_state[:] = normalized
        source_names = ALL_TOOL_NAMES if allowed_tool_names is None else allowed_tool_names
        selected = tuple(str(name).strip() for name in source_names if str(name).strip())
        for name in selected:
            self._register_default(name)

    def _register_default(self, name: str) -> None:
        mapping: dict[str, Callable[..., ToolResult]] = {
            "bash": builtins.run_command,
            "read": builtins.read_file,
            "write": builtins.write_file,
            "delete": builtins.delete_file,
            "edit": builtins.edit_file,
            "glob": builtins.glob_files,
            "grep": builtins.grep_files,
            "webfetch": builtins.web_fetch,
            "websearch": builtins.web_search,
            "task": lambda sandbox, **kwargs: builtins.delegate_task(sandbox, delegate=self._task_delegate, **kwargs),
            "todowrite": lambda sandbox, **kwargs: builtins.todo_write(sandbox, todo_state=self._todo_state, **kwargs),
            "question": builtins.ask_question,
            "skill": lambda sandbox, **kwargs: builtins.load_skill(sandbox, skill_instructions=self._skill_instructions, **kwargs),
            "git_status": builtins.git_status,
            "git_diff": builtins.git_diff,
            "file_info": builtins.file_info,
            "get_current_time": lambda _sandbox, **kwargs: builtins.get_current_time(**kwargs),
            "list_files": builtins.list_files,
            "read_file": advanced.read_file_slice,
            "search_files": builtins.search_files,
            "write_file": builtins.write_file,
            "run_command": builtins.run_command,
            "edit_file": builtins.edit_file,
            "glob_search": builtins.glob_files,
            "grep_search": lambda sandbox, pattern, path=".", glob="*", case_sensitive=False, head_limit=100, **_kwargs: builtins.grep_files(
                sandbox,
                pattern,
                path=path,
                file_pattern=glob,
                case_sensitive=case_sensitive,
                limit=head_limit,
            ),
            "WebFetch": builtins.web_fetch,
            "WebSearch": builtins.web_search,
            "TodoWrite": lambda sandbox, todos: builtins.todo_write(
                sandbox,
                todos=_normalize_claw_todos(todos),
                todo_state=self._todo_state,
            ),
            "Skill": lambda sandbox, skill="", skill_id="", name="": builtins.load_skill(
                sandbox,
                skill_id=skill_id or skill,
                name=name,
                skill_instructions=self._skill_instructions,
            ),
            "Agent": lambda sandbox, prompt, subagent_type="", name="", **_kwargs: builtins.delegate_task(
                sandbox,
                task=prompt,
                agent_id=subagent_type or name,
                delegate=self._task_delegate,
            ),
            "ToolSearch": lambda sandbox, query, max_results=20: advanced.tool_search(
                sandbox,
                query,
                TOOL_SCHEMAS,
                max_results=max_results,
            ),
            "NotebookEdit": advanced.notebook_edit,
            "Sleep": advanced.sleep_tool,
            "SendUserMessage": advanced.send_user_message,
            "Config": advanced.config_tool,
            "EnterPlanMode": advanced.enter_plan_mode,
            "ExitPlanMode": advanced.exit_plan_mode,
            "StructuredOutput": advanced.structured_output,
            "REPL": advanced.repl,
            "PowerShell": advanced.powershell,
            "AskUserQuestion": builtins.ask_question,
            "TaskCreate": lambda sandbox, **kwargs: advanced.task_create(sandbox, tool_name="TaskCreate", **kwargs),
            "RunTaskPacket": lambda sandbox, packet: advanced.task_create(sandbox, packet=packet, tool_name="RunTaskPacket"),
            "TaskGet": lambda sandbox, **kwargs: advanced.task_get(sandbox, tool_name="TaskGet", **kwargs),
            "TaskList": lambda sandbox: advanced.task_list(sandbox, tool_name="TaskList"),
            "TaskStop": lambda sandbox, task_id, message=None: advanced.task_update(
                sandbox,
                task_id,
                status="stopped",
                message=message,
                tool_name="TaskStop",
            ),
            "TaskUpdate": lambda sandbox, **kwargs: advanced.task_update(sandbox, tool_name="TaskUpdate", **kwargs),
            "TaskOutput": advanced.task_output,
            "WorkerCreate": advanced.worker_create,
            "WorkerGet": lambda sandbox, worker_id: advanced._worker_action(sandbox, worker_id, "WorkerGet"),
            "WorkerObserve": lambda sandbox, worker_id, **kwargs: advanced._worker_action(sandbox, worker_id, "WorkerObserve", **kwargs),
            "WorkerResolveTrust": lambda sandbox, worker_id: advanced._worker_action(sandbox, worker_id, "WorkerResolveTrust"),
            "WorkerAwaitReady": advanced.worker_await_ready,
            "WorkerSendPrompt": lambda sandbox, worker_id, **kwargs: advanced._worker_action(sandbox, worker_id, "WorkerSendPrompt", **kwargs),
            "WorkerRestart": lambda sandbox, worker_id: advanced._worker_action(sandbox, worker_id, "WorkerRestart"),
            "WorkerTerminate": lambda sandbox, worker_id: advanced._worker_action(sandbox, worker_id, "WorkerTerminate"),
            "WorkerObserveCompletion": lambda sandbox, worker_id, **kwargs: advanced._worker_action(sandbox, worker_id, "WorkerObserveCompletion", **kwargs),
            "TeamCreate": advanced.team_create,
            "TeamDelete": advanced.team_delete,
            "CronCreate": advanced.cron_create,
            "CronDelete": advanced.cron_delete,
            "CronList": advanced.cron_list,
            "LSP": advanced.lsp_query,
            "ListMcpResources": advanced.mcp_list_resources,
            "ReadMcpResource": advanced.mcp_read_resource,
            "McpAuth": advanced.mcp_unavailable,
            "RemoteTrigger": advanced.remote_trigger,
            "MCP": advanced.mcp_unavailable,
            "TestingPermission": lambda _sandbox: ToolResult(
                "TestingPermission",
                True,
                self.permission_mode,
                metadata={"permission_mode": self.permission_mode},
            ),
            "GitStatus": builtins.git_status,
            "GitDiff": builtins.git_diff,
            "GitLog": advanced.git_log,
            "GitShow": advanced.git_show,
            "GitBlame": advanced.git_blame,
            "MemoryWrite": advanced.memory_write,
            "MemoryRead": advanced.memory_read,
            "MemoryList": advanced.memory_list,
            "load_skill": lambda sandbox, **kwargs: builtins.load_skill(
                sandbox,
                skill_instructions=self._skill_instructions,
                **kwargs,
            ),
            "compress": advanced.request_compaction,
            "background_run": advanced.background_run,
            "check_background": advanced.check_background,
            "task_create": advanced.task_create,
            "task_get": advanced.task_get,
            "task_update": advanced.task_update,
            "task_list": advanced.task_list,
            "spawn_teammate": advanced.spawn_teammate,
            "list_teammates": advanced.list_teammates,
            "send_message": advanced.send_message,
            "read_inbox": advanced.read_inbox,
            "broadcast": advanced.broadcast,
            "shutdown_request": advanced.shutdown_request,
            "plan_approval": advanced.plan_approval,
            "idle": advanced.idle_tool,
            "claim_task": advanced.task_claim,
        }
        function = mapping.get(name)
        if function is not None:
            self.register(name, function)

    def register(self, name: str, function: Callable[..., ToolResult]) -> None:
        if name not in TOOL_SCHEMAS:
            raise ValueError(f"工具没有 provider schema: {name}")
        self._tools[name] = function

    @property
    def enabled_tool_names(self) -> tuple[str, ...]:
        return tuple(self._tools)

    def can_execute_batch_in_parallel(self, names: Iterable[str]) -> bool:
        normalized = tuple(str(name) for name in names)
        return len(normalized) > 1 and all(
            name in self._tools and name in PARALLEL_READ_ONLY_TOOL_NAMES
            for name in normalized
        )

    @property
    def schemas(self) -> list[dict[str, Any]]:
        return [
            {"type": "function", "function": {"name": name, **TOOL_SCHEMAS[name]}}
            for name in self._tools
        ]

    @property
    def skill_catalog_prompt(self) -> str:
        if "skill" not in self._tools or not self._skill_instructions:
            return ""
        lines = [
            "本会话已选择以下 Skill。仅在需要其详细工作流时调用 skill 工具按 id 加载；不要猜测或执行 Skill 中未明确允许的脚本："
        ]
        for item in self._skill_instructions.values():
            description = str(item.get("description") or "").strip().replace("\n", " ")[:300]
            suffix = f" — {description}" if description else ""
            lines.append(f"- {item['id']}: {item['name']}{suffix}")
        return "\n".join(lines)

    def runtime_state(self) -> dict[str, Any]:
        """Return only JSON-safe state that must survive approval/resume."""

        return {
            "allowed_tool_names": list(self.enabled_tool_names),
            "permission_mode": self.permission_mode,
            "skill_instructions": [dict(item) for item in self._skill_instructions.values()],
            "todo_state": [dict(item) for item in self._todo_state],
        }

    def cancel_active(self) -> None:
        """Signal synchronous tools currently running in worker threads.

        Python cannot force-kill an arbitrary thread.  Built-ins that own an
        external process (notably ``run_command``) receive this event and
        terminate their process tree; other bounded file/network helpers stop
        being awaited by the cancelled runtime task and their late result is
        discarded by the coordinator.
        """

        with self._active_cancel_lock:
            events = tuple(self._active_cancel_events.values())
        for event in events:
            event.set()

    @staticmethod
    def _rename_result(result: ToolResult, tool_name: str) -> ToolResult:
        """Keep provider call, ToolResult and approval resume names identical."""

        result.tool_name = tool_name
        if result.approval_request is not None:
            result.approval_request.tool_name = tool_name
        return result

    def execute(
        self,
        name: str,
        arguments: dict[str, Any] | None = None,
        *,
        approved: bool = False,
        _cancel_event: threading.Event | None = None,
    ) -> ToolResult:
        function = self._tools.get(name)
        if function is None:
            error_code = "tool_not_enabled" if name in self._known_tools else "unknown_tool"
            return ToolResult(name, False, f"当前会话未启用工具: {name}", error_code=error_code)
        kwargs = dict(arguments or {})
        if "_raw" in kwargs or "_invalid_json" in kwargs:
            raw_value = kwargs.get("_raw")
            raw_chars = len(raw_value) if isinstance(raw_value, str) else int(kwargs.get("_argument_chars") or kwargs.get("_raw_chars") or 0)
            return ToolResult(
                name,
                False,
                "工具参数不是有效的 JSON 对象，已拒绝执行",
                error_code="invalid_tool_arguments",
                metadata={"raw_chars": raw_chars},
            )
        if (
            name in PLAN_MODE_MUTATING_TOOLS
            and name != "ExitPlanMode"
            and advanced.plan_mode_enabled(self.sandbox)
        ):
            return ToolResult(
                name,
                False,
                "Plan mode is active; mutating and execution tools are disabled until ExitPlanMode.",
                error_code="plan_mode_read_only",
            )
        # A catalog checkbox alone must never make the model believe it has
        # delegated work.  Until a real delegate is injected, return the
        # explicit unavailable result without asking the user to approve a
        # no-op first.
        if name in {"task", "Agent"} and self._task_delegate is None:
            return self._rename_result(function(self.sandbox, **kwargs), name)
        decision = assess_tool_call(
            name,
            self.permission_mode,
            arguments=kwargs,
            approved=approved,
            workspace_root=self.sandbox.root,
        )
        if decision.requires_approval:
            return _approval(name, kwargs, decision.reason)
        # Side-effecting builtins retain their own approval primitive for direct
        # callers.  The registry is the only runtime entry point and passes the
        # effective grant after policy has checked it.
        if name in {"write", "write_file", "edit", "edit_file", "delete", "bash", "run_command"}:
            kwargs["approved"] = True
        if _cancel_event is not None and name in {"bash", "run_command"}:
            # This is an in-process cancellation signal, not a model/tool
            # argument.  Inject it only after policy approval so it can never
            # leak into ApprovalRequest JSON or the persisted run snapshot.
            kwargs["_cancel_event"] = _cancel_event
        try:
            return self._rename_result(function(self.sandbox, **kwargs), name)
        except TypeError as exc:
            return ToolResult(name, False, f"工具参数无效: {exc}", error_code="invalid_arguments")
        except Exception as exc:  # A tool failure must never crash the model loop.
            return ToolResult(name, False, f"工具执行失败: {type(exc).__name__}", error_code="tool_error")

    async def execute_async(
        self,
        name: str,
        arguments: dict[str, Any] | None = None,
        *,
        approved: bool = False,
        call_id: str | None = None,
    ) -> ToolResult:
        """Execute a tool without blocking a child-Agent model invocation.

        All ordinary built-ins remain synchronous and retain the exact policy
        path in :meth:`execute`.  ``task`` is the one exception: a genuine
        delegate needs to await another ``AgentRuntime``.  Keeping that async
        boundary here avoids nesting an event loop or faking a background
        result while preserving the same approval checks.
        """

        if name not in {"task", "Agent"}:
            # Built-in tools are synchronous (filesystem, subprocess and
            # bounded network helpers).  Running them directly here would
            # block FastAPI's event loop and make the user's stop request
            # wait until a long command returns.  A worker thread keeps the
            # stop endpoint responsive; the coordinator cancels the awaiting
            # runtime task and discards the eventual worker result.
            cancel_key = str(call_id or f"tool-{id(arguments)}")
            cancel_event = threading.Event()
            with self._active_cancel_lock:
                self._active_cancel_events[cancel_key] = cancel_event
            try:
                return await asyncio.to_thread(
                    self.execute,
                    name,
                    dict(arguments or {}),
                    approved=approved,
                    _cancel_event=cancel_event,
                )
            finally:
                with self._active_cancel_lock:
                    self._active_cancel_events.pop(cancel_key, None)
        function = self._tools.get(name)
        if function is None:
            error_code = "tool_not_enabled" if name in self._known_tools else "unknown_tool"
            return ToolResult(name, False, f"当前会话未启用工具: {name}", error_code=error_code)
        provider_kwargs = dict(arguments or {})
        kwargs = dict(provider_kwargs)
        if name == "Agent":
            kwargs = {
                "task": kwargs.get("prompt"),
                "agent_id": kwargs.get("subagent_type") or kwargs.get("name"),
            }
        if name in PLAN_MODE_MUTATING_TOOLS and advanced.plan_mode_enabled(self.sandbox):
            return ToolResult(
                name,
                False,
                "Plan mode is active; delegation is disabled until ExitPlanMode.",
                error_code="plan_mode_read_only",
            )
        # Reject malformed calls before an approval UI is created for a task
        # that cannot possibly be dispatched.
        _, validation_error_code, validation_error = builtins.normalize_delegate_requests(
            kwargs.get("task"), kwargs.get("agent_id"), kwargs.get("tasks")
        )
        if validation_error_code and validation_error:
            return ToolResult(name, False, validation_error, error_code=validation_error_code)
        if self._task_delegate is None:
            try:
                return self._rename_result(function(self.sandbox, **provider_kwargs), name)
            except TypeError as exc:
                return ToolResult(name, False, f"工具参数无效: {exc}", error_code="invalid_arguments")
        decision = assess_tool_call(
            name,
            self.permission_mode,
            arguments=provider_kwargs,
            approved=approved,
            workspace_root=self.sandbox.root,
        )
        if decision.requires_approval:
            return _approval(name, provider_kwargs, decision.reason)
        try:
            result = await builtins.delegate_task_async(
                self.sandbox,
                delegate=self._task_delegate,
                call_id=call_id,
                **kwargs,
            )
            return self._rename_result(result, name)
        except TypeError as exc:
            return ToolResult(name, False, f"工具参数无效: {exc}", error_code="invalid_arguments")
        except Exception as exc:  # Delegate faults must obey normal loop recovery.
            return ToolResult(name, False, f"工具执行失败: {type(exc).__name__}", error_code="tool_error")


def create_default_registry(
    workspace_root: str,
    *,
    allowed_tool_names: Iterable[str] | None = None,
    permission_mode: str = "smart",
    skill_instructions: Iterable[Mapping[str, Any] | str] | None = None,
    todo_state: Iterable[Mapping[str, Any]] | None = None,
    task_delegate: Callable[..., ToolResult | Any] | None = None,
) -> ToolRegistry:
    """Create a sandboxed registry with an explicit, frozen capability list."""

    return ToolRegistry(
        WorkspaceSandbox(workspace_root),
        allowed_tool_names=allowed_tool_names,
        permission_mode=permission_mode,
        skill_instructions=skill_instructions,
        todo_state=todo_state,
        task_delegate=task_delegate,
    )
