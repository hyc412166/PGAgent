"""Tool registry, executable capability selection, and provider schemas."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from typing import Any

from . import builtins
from .policy import approval_reason, approval_required, normalize_permission_mode
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
        "description": "把一个明确子任务委派给已启用的子 Agent。必须使用当前子 Agent 列表中的 agent_id；子 Agent 只能使用它自身被勾选且不超过当前会话权限的工具，且不能再次委派。",
        "parameters": {
            "type": "object",
            "properties": {
                "task": {"type": "string"},
                "agent_id": {"type": "string"},
            },
            "required": ["task", "agent_id"],
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


PUBLIC_TOOL_NAMES: tuple[str, ...] = (
    "bash",
    "read",
    "write",
    "edit",
    "glob",
    "grep",
    "webfetch",
    "websearch",
    "task",
    "todowrite",
    "question",
    "skill",
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
        selected = tuple(str(name).strip() for name in (allowed_tool_names or ALL_TOOL_NAMES) if str(name).strip())
        for name in selected:
            self._register_default(name)

    def _register_default(self, name: str) -> None:
        mapping: dict[str, Callable[..., ToolResult]] = {
            "bash": builtins.run_command,
            "read": builtins.read_file,
            "write": builtins.write_file,
            "edit": builtins.edit_file,
            "glob": builtins.glob_files,
            "grep": builtins.grep_files,
            "webfetch": builtins.web_fetch,
            "websearch": builtins.web_search,
            "task": lambda sandbox, **kwargs: builtins.delegate_task(sandbox, delegate=self._task_delegate, **kwargs),
            "todowrite": lambda sandbox, **kwargs: builtins.todo_write(sandbox, todo_state=self._todo_state, **kwargs),
            "question": builtins.ask_question,
            "skill": lambda sandbox, **kwargs: builtins.load_skill(sandbox, skill_instructions=self._skill_instructions, **kwargs),
            "get_current_time": lambda _sandbox, **kwargs: builtins.get_current_time(**kwargs),
            "list_files": builtins.list_files,
            "read_file": builtins.read_file,
            "search_files": builtins.search_files,
            "write_file": builtins.write_file,
            "run_command": builtins.run_command,
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
    ) -> ToolResult:
        function = self._tools.get(name)
        if function is None:
            error_code = "tool_not_enabled" if name in self._known_tools else "unknown_tool"
            return ToolResult(name, False, f"当前会话未启用工具: {name}", error_code=error_code)
        kwargs = dict(arguments or {})
        # A catalog checkbox alone must never make the model believe it has
        # delegated work.  Until a real delegate is injected, return the
        # explicit unavailable result without asking the user to approve a
        # no-op first.
        if name == "task" and self._task_delegate is None:
            return self._rename_result(function(self.sandbox, **kwargs), name)
        if approval_required(name, self.permission_mode, approved=approved):
            return _approval(name, kwargs, approval_reason(name, self.permission_mode))
        # Side-effecting builtins retain their own approval primitive for direct
        # callers.  The registry is the only runtime entry point and passes the
        # effective grant after policy has checked it.
        if name in {"write", "write_file", "edit", "bash", "run_command"}:
            kwargs["approved"] = True
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

        if name != "task":
            return self.execute(name, arguments, approved=approved)
        function = self._tools.get(name)
        if function is None:
            error_code = "tool_not_enabled" if name in self._known_tools else "unknown_tool"
            return ToolResult(name, False, f"当前会话未启用工具: {name}", error_code=error_code)
        kwargs = dict(arguments or {})
        # Reject malformed calls before an approval UI is created for a task
        # that cannot possibly be dispatched.
        request = str(kwargs.get("task") or "").strip()
        target = str(kwargs.get("agent_id") or "").strip()
        if not request or len(request) > 8_000:
            return ToolResult(name, False, "task 不能为空或过长", error_code="invalid_task")
        if not target or len(target) > 80:
            return ToolResult(name, False, "必须提供有效的子 Agent ID", error_code="invalid_delegate_agent")
        if self._task_delegate is None:
            return self._rename_result(function(self.sandbox, **kwargs), name)
        if approval_required(name, self.permission_mode, approved=approved):
            return _approval(name, kwargs, approval_reason(name, self.permission_mode))
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
