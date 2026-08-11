"""Tool registry and JSON schemas passed to model providers."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from . import builtins
from .sandbox import WorkspaceSandbox
from .types import ToolResult


TOOL_SCHEMAS: dict[str, dict[str, Any]] = {
    "list_files": {
        "description": "列出工作区内的文件和目录",
        "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "recursive": {"type": "boolean"}}},
    },
    "read_file": {
        "description": "读取工作区内的 UTF-8 文本文件",
        "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
    },
    "search_files": {
        "description": "搜索工作区文本文件中的内容",
        "parameters": {
            "type": "object",
            "properties": {"query": {"type": "string"}, "path": {"type": "string"}, "pattern": {"type": "string"}},
            "required": ["query"],
        },
    },
    "get_current_time": {
        "description": "获取本机当前时间和时区",
        "parameters": {"type": "object", "properties": {"timezone_name": {"type": "string"}}},
    },
    "write_file": {
        "description": "写入工作区文件（需要用户批准）",
        "parameters": {
            "type": "object",
            "properties": {"path": {"type": "string"}, "content": {"type": "string"}, "overwrite": {"type": "boolean"}},
            "required": ["path", "content"],
        },
    },
    "run_command": {
        "description": "以当前用户本机权限、工作区为 cwd 执行允许列表命令（需要用户批准，进程并非文件沙箱）",
        "parameters": {
            "type": "object",
            "properties": {"command": {"oneOf": [{"type": "string"}, {"type": "array", "items": {"type": "string"}}]}, "timeout_seconds": {"type": "number"}},
            "required": ["command"],
        },
    },
}


class ToolRegistry:
    def __init__(self, sandbox: WorkspaceSandbox) -> None:
        self.sandbox = sandbox
        self._tools: dict[str, Callable[..., ToolResult]] = {}

    def register(self, name: str, function: Callable[..., ToolResult]) -> None:
        self._tools[name] = function

    @property
    def schemas(self) -> list[dict[str, Any]]:
        return [
            {"type": "function", "function": {"name": name, **TOOL_SCHEMAS[name]}}
            for name in self._tools
        ]

    def execute(self, name: str, arguments: dict[str, Any] | None = None, *, approved: bool = False) -> ToolResult:
        function = self._tools.get(name)
        if function is None:
            return ToolResult(name, False, f"未知工具: {name}", error_code="unknown_tool")
        kwargs = dict(arguments or {})
        if name in {"write_file", "run_command"}:
            kwargs["approved"] = approved
        try:
            return function(self.sandbox, **kwargs)
        except TypeError as exc:
            return ToolResult(name, False, f"工具参数无效: {exc}", error_code="invalid_arguments")
        except Exception as exc:  # A tool failure must never crash the whole agent loop.
            return ToolResult(name, False, f"工具执行失败: {exc}", error_code="tool_error")


def create_default_registry(workspace_root: str) -> ToolRegistry:
    registry = ToolRegistry(WorkspaceSandbox(workspace_root))
    registry.register("list_files", builtins.list_files)
    registry.register("read_file", builtins.read_file)
    registry.register("search_files", builtins.search_files)

    # get_current_time does not need a sandbox argument; adapt it to the common signature.
    registry.register("get_current_time", lambda _sandbox, **kwargs: builtins.get_current_time(**kwargs))
    registry.register("write_file", builtins.write_file)
    registry.register("run_command", builtins.run_command)
    return registry
