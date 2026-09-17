"""Provider contracts for the Learn Claude Code and Claw Code tool surface.

The names intentionally retain the casing used by the reference projects so
persisted tool calls and model-generated calls can be replayed verbatim.
"""
# 文件职责：负责工具定义、授权、注册、调度与执行中的 advanced_contract 子模块。
# 逻辑关系：上层通过 tools/advanced_contract.py 使用本模块；本模块把处理结果交给同领域服务、持久化层或 API 响应层。

from __future__ import annotations

from typing import Any


# 函数职责：完成 schema 对应的业务处理。
# 参数关系：description 表示当前步骤使用的 description 值；properties 表示当前流程使用的 properties 集合；required 表示当前步骤使用的 required 值。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def _schema(description: str, properties: dict[str, Any] | None = None, required: tuple[str, ...] = ()) -> dict[str, Any]:
    # 变量说明：parameters 表示当前流程使用的 parameters 集合。
    parameters: dict[str, Any] = {"type": "object", "properties": properties or {}}
    if required:
        parameters["required"] = list(required)
    return {"description": description, "parameters": parameters}


# 变量说明：_STRING 表示当前步骤使用的 _STRING 值。
_STRING = {"type": "string"}
# 变量说明：_BOOL 表示当前步骤使用的 _BOOL 值。
_BOOL = {"type": "boolean"}
# 变量说明：_INTEGER 表示当前步骤使用的 _INTEGER 值。
_INTEGER = {"type": "integer"}
# 变量说明：_NUMBER 表示当前步骤使用的 _NUMBER 值。
_NUMBER = {"type": "number"}
# 变量说明：_STRINGS 表示当前流程使用的 _STRINGS 集合。
_STRINGS = {"type": "array", "items": _STRING}
# 变量说明：_OBJECT 表示当前步骤使用的 _OBJECT 值。
_OBJECT = {"type": "object", "additionalProperties": True}


# 变量说明：ADVANCED_TOOL_SCHEMAS 表示当前流程使用的 ADVANCED_TOOL_SCHEMAS 集合。
ADVANCED_TOOL_SCHEMAS: dict[str, dict[str, Any]] = {
    # Claw Code contracts.
    "edit_file": _schema("Replace exact text in a workspace file.", {"path": _STRING, "old_string": _STRING, "new_string": _STRING, "replace_all": _BOOL}, ("path", "old_string", "new_string")),
    "glob_search": _schema("Find workspace paths by glob pattern.", {"pattern": _STRING, "path": _STRING, "limit": _INTEGER}, ("pattern",)),
    "grep_search": _schema("Search workspace text with a regular expression.", {"pattern": _STRING, "path": _STRING, "glob": _STRING, "case_sensitive": _BOOL, "head_limit": _INTEGER}, ("pattern",)),
    "WebFetch": _schema("Fetch one public HTTP or HTTPS resource.", {"url": _STRING, "timeout_seconds": _NUMBER}, ("url",)),
    "WebSearch": _schema("Search the public web.", {"query": _STRING, "limit": _INTEGER}, ("query",)),
    "TodoWrite": _schema("Replace the current structured todo list. Keep each id stable across updates.", {"todos": {"type": "array", "items": {"type": "object", "properties": {"id": _STRING, "content": _STRING, "status": {"type": "string", "enum": ["pending", "in_progress", "completed", "cancelled"]}, "activeForm": _STRING}, "required": ["id", "content", "status"]}}}, ("todos",)),
    "Skill": _schema("Load the full instructions for an enabled skill.", {"skill": _STRING, "skill_id": _STRING, "name": _STRING}),
    "Agent": _schema("Delegate a task to an enabled child agent.", {"prompt": _STRING, "description": _STRING, "subagent_type": _STRING, "name": _STRING, "run_in_background": _BOOL, "model_id": _STRING, "thinking_level": {"type": "string", "enum": ["low", "medium", "high", "xhigh"]}}, ("prompt",)),
    "ToolSearch": _schema("Search the enabled tool catalog by name or description.", {"query": _STRING, "max_results": _INTEGER}, ("query",)),
    "NotebookEdit": _schema("Insert, replace, or delete a Jupyter notebook cell.", {"notebook_path": _STRING, "cell_id": _STRING, "new_source": _STRING, "cell_type": {"type": "string", "enum": ["code", "markdown"]}, "edit_mode": {"type": "string", "enum": ["replace", "insert", "delete"]}}, ("notebook_path",)),
    "Sleep": _schema("Wait for a bounded number of milliseconds.", {"duration_ms": _INTEGER}, ("duration_ms",)),
    "SendUserMessage": _schema("Record an explicit message intended for the user.", {"message": _STRING}, ("message",)),
    "Config": _schema("Read or update a durable workspace setting.", {"setting": _STRING, "value": {}}, ("setting",)),
    "EnterPlanMode": _schema("Enter read-only planning mode.", {}),
    "ExitPlanMode": _schema("Leave planning mode and submit the plan.", {"plan": _STRING}),
    "StructuredOutput": _schema("Return a JSON-serializable structured payload.", {"value": {}}),
    "REPL": _schema("Execute bounded Python or JavaScript code in the workspace.", {"code": _STRING, "language": _STRING, "timeout_ms": _INTEGER}, ("code", "language")),
    "PowerShell": _schema("Execute a bounded PowerShell command in the workspace.", {"command": _STRING, "timeout": _INTEGER, "description": _STRING, "run_in_background": _BOOL}, ("command",)),
    "AskUserQuestion": _schema("Ask the user for missing information and pause.", {"question": _STRING, "context": _STRING}, ("question",)),
    "TaskCreate": _schema("Create a durable task-board item.", {"subject": _STRING, "description": _STRING}, ("subject",)),
    "RunTaskPacket": _schema("Create a task from a structured execution packet.", {"packet": _OBJECT}, ("packet",)),
    "TaskGet": _schema("Get one durable task-board item.", {"task_id": {"oneOf": [_STRING, _INTEGER]}}, ("task_id",)),
    "TaskList": _schema("List durable task-board items.", {}),
    "TaskStop": _schema("Stop one durable task-board item.", {"task_id": {"oneOf": [_STRING, _INTEGER]}, "message": _STRING}, ("task_id",)),
    "TaskUpdate": _schema("Update task status, blockers, messages, or output.", {"task_id": {"oneOf": [_STRING, _INTEGER]}, "status": _STRING, "message": _STRING, "add_blocked_by": _STRINGS, "remove_blocked_by": _STRINGS, "output": _STRING}, ("task_id",)),
    "TaskOutput": _schema("Read a task's latest output and messages.", {"task_id": {"oneOf": [_STRING, _INTEGER]}}, ("task_id",)),
    "WorkerCreate": _schema("Create a durable worker controller record.", {"cwd": _STRING, "trusted_roots": _STRINGS, "auto_recover_prompt_misdelivery": _BOOL}, ("cwd",)),
    "WorkerGet": _schema("Get one worker controller record.", {"worker_id": _STRING}, ("worker_id",)),
    "WorkerObserve": _schema("Record and classify a worker screen observation.", {"worker_id": _STRING, "screen_text": _STRING}, ("worker_id",)),
    "WorkerResolveTrust": _schema("Resolve a worker trust prompt.", {"worker_id": _STRING}, ("worker_id",)),
    "WorkerAwaitReady": _schema("Wait until a worker is ready or timeout expires.", {"worker_id": _STRING, "timeout_ms": _INTEGER}, ("worker_id",)),
    "WorkerSendPrompt": _schema("Send a prompt and optional receipt to a ready worker.", {"worker_id": _STRING, "prompt": _STRING, "task_receipt": _OBJECT}, ("worker_id", "prompt")),
    "WorkerRestart": _schema("Restart a worker controller record.", {"worker_id": _STRING}, ("worker_id",)),
    "WorkerTerminate": _schema("Terminate a worker controller record.", {"worker_id": _STRING}, ("worker_id",)),
    "WorkerObserveCompletion": _schema("Record a worker completion observation.", {"worker_id": _STRING, "finish_reason": _STRING, "tokens_output": _INTEGER}, ("worker_id", "finish_reason")),
    "TeamCreate": _schema("Create a team and its initial tasks.", {"name": _STRING, "tasks": {"type": "array", "items": _OBJECT}}, ("name", "tasks")),
    "TeamDelete": _schema("Delete a team and stop its tasks.", {"team_id": _STRING}, ("team_id",)),
    "CronCreate": _schema("Create a durable cron definition.", {"schedule": _STRING, "prompt": _STRING, "description": _STRING}, ("schedule", "prompt")),
    "CronDelete": _schema("Delete a durable cron definition.", {"cron_id": _STRING}, ("cron_id",)),
    "CronList": _schema("List durable cron definitions.", {}),
    "LSP": _schema("Run a local diagnostics, symbols, definition, reference, or hover query.", {"action": _STRING, "path": _STRING, "line": _INTEGER, "character": _INTEGER, "query": _STRING}, ("action",)),
    "ListMcpResources": _schema("List configured MCP resources.", {"server": _STRING}),
    "ReadMcpResource": _schema("Read one configured MCP resource.", {"uri": _STRING, "server": _STRING}, ("uri",)),
    "McpAuth": _schema("Request MCP authentication when a connector supports it.", {"server": _STRING}),
    "RemoteTrigger": _schema("Call an explicit public HTTP endpoint.", {"url": _STRING, "method": _STRING, "headers": _OBJECT, "body": _STRING}, ("url",)),
    "MCP": _schema("Call a configured executable MCP operation.", {"server": _STRING, "tool": _STRING, "arguments": _OBJECT}, ("server", "tool")),
    "TestingPermission": _schema("Return the current effective permission mode for tests.", {}),
    "GitStatus": _schema("Read Git working-tree status.", {"include_untracked": _BOOL, "max_chars": _INTEGER}),
    "GitDiff": _schema("Read a bounded Git diff.", {"staged": _BOOL, "path": _STRING, "max_chars": _INTEGER}),
    "GitLog": _schema("Read Git history.", {"path": _STRING, "count": _INTEGER, "oneline": _BOOL, "author": _STRING, "since": _STRING, "until": _STRING}),
    "GitShow": _schema("Read a Git commit, object, or path.", {"commit": _STRING, "path": _STRING, "stat": _BOOL, "format": _STRING}, ("commit",)),
    "GitBlame": _schema("Read Git blame for a path and optional line range.", {"path": _STRING, "start_line": _INTEGER, "end_line": _INTEGER}, ("path",)),
    "MemoryWrite": _schema("Write a durable memory to the canonical database.", {"name": _STRING, "body": _STRING, "memory_type": {"type": "string", "enum": ["user", "feedback", "project", "reference"]}, "description": _STRING, "scope": {"type": "string", "enum": ["global", "workspace", "session"]}, "tags": _STRINGS, "pinned": _BOOL}, ("name", "body")),
    "MemoryRead": {"description": "Read one visible durable memory by id/name, one supporting rollout summary, or one promoted memory skill.", "parameters": {"type": "object", "properties": {"name": _STRING, "memory_id": _STRING, "rollout_id": _STRING, "skill_id": _STRING}, "oneOf": [{"required": ["name"]}, {"required": ["memory_id"]}, {"required": ["rollout_id"]}, {"required": ["skill_id"]}]}},
    "MemoryList": _schema("List active visible durable memory metadata.", {}),
    "MemorySearch": _schema("Search visible durable memories relevant to a query.", {"query": _STRING, "limit": _INTEGER}, ("query",)),

    # Learn Claude Code contracts.
    "load_skill": _schema("Load one enabled skill by id or name.", {"skill_id": _STRING, "name": _STRING}),
    "compress": _schema("Request a full conversation compaction at the next safe boundary.", {"reason": _STRING}),
    "background_run": _schema("Start a durable bounded background command and return immediately.", {"command": _STRING, "timeout": _INTEGER, "shell": {"type": "string", "enum": ["command", "powershell"]}, "plan_step_id": _STRING}, ("command",)),
    "check_background": _schema("Check one or all background commands. Waiting only yields control; a wait timeout never stops the process.", {"task_id": _STRING, "wait": _BOOL, "wait_timeout": {"type": "number", "minimum": 0}, "output_offset": {"type": "integer", "minimum": 0}}),
    "write_stdin": _schema("Send input to a running background command, then optionally wait for and read new output.", {"task_id": _STRING, "input": _STRING, "close": _BOOL, "wait_ms": {"type": "integer", "minimum": 0, "maximum": 300000}, "output_offset": {"type": "integer", "minimum": 0}}, ("task_id", "input")),
    "task_create": _schema("Create a durable shared task.", {"subject": _STRING, "description": _STRING}, ("subject",)),
    "task_get": _schema("Get one durable shared task.", {"task_id": {"oneOf": [_STRING, _INTEGER]}}, ("task_id",)),
    "task_update": _schema("Update one durable shared task.", {"task_id": {"oneOf": [_STRING, _INTEGER]}, "status": _STRING, "message": _STRING, "add_blocked_by": _STRINGS, "remove_blocked_by": _STRINGS, "output": _STRING}, ("task_id",)),
    "task_list": _schema("List durable shared tasks.", {}),
    "spawn_teammate": _schema("Create or reuse a durable teammate identity backed by a configured child Agent.", {"name": _STRING, "role": _STRING, "prompt": _STRING, "agent_id": _STRING, "workspace_mode": {"type": "string", "enum": ["shared", "worktree"]}}, ("name", "role", "prompt", "agent_id")),
    "list_teammates": _schema("List durable teammate records.", {}),
    "send_message": _schema("Send a durable teammate message.", {"to": _STRING, "content": _STRING, "sender": _STRING, "msg_type": _STRING}, ("to", "content")),
    "read_inbox": _schema("Read and drain a teammate inbox.", {"recipient": _STRING}),
    "broadcast": _schema("Send one message to all teammates.", {"content": _STRING}, ("content",)),
    "shutdown_request": _schema("Ask a teammate to shut down.", {"teammate": _STRING}, ("teammate",)),
    "integrate_teammate": _schema("Commit only the explicitly selected paths from one isolated teammate worktree, then merge its branch into the parent branch.", {"teammate": _STRING, "commit_message": _STRING, "paths": _STRINGS}, ("teammate",)),
    "plan_approval": _schema("Approve or reject a teammate plan with feedback.", {"request_id": _STRING, "approve": _BOOL, "feedback": _STRING}, ("request_id", "approve")),
    "idle": _schema("Yield while waiting for teammate or background progress.", {"duration_ms": _INTEGER}),
    "claim_task": _schema("Atomically claim an unblocked shared task.", {"task_id": {"oneOf": [_STRING, _INTEGER]}, "owner": _STRING}, ("task_id",)),
}


# 变量说明：CLAW_TOOL_NAMES 表示当前流程使用的 CLAW_TOOL_NAMES 集合。
CLAW_TOOL_NAMES: tuple[str, ...] = (
    "edit_file", "glob_search", "grep_search", "WebFetch", "WebSearch", "TodoWrite", "Skill", "Agent",
    "ToolSearch", "NotebookEdit", "Sleep", "SendUserMessage", "Config", "EnterPlanMode", "ExitPlanMode",
    "StructuredOutput", "REPL", "PowerShell", "AskUserQuestion", "TaskCreate", "RunTaskPacket", "TaskGet",
    "TaskList", "TaskStop", "TaskUpdate", "TaskOutput", "WorkerCreate", "WorkerGet", "WorkerObserve",
    "WorkerResolveTrust", "WorkerAwaitReady", "WorkerSendPrompt", "WorkerRestart", "WorkerTerminate",
    "WorkerObserveCompletion", "TeamCreate", "TeamDelete", "CronCreate", "CronDelete", "CronList", "LSP",
    "ListMcpResources", "ReadMcpResource", "McpAuth", "RemoteTrigger", "MCP", "TestingPermission",
    "GitStatus", "GitDiff", "GitLog", "GitShow", "GitBlame", "MemoryWrite", "MemoryRead", "MemoryList", "MemorySearch",
)

# 变量说明：LEARN_TOOL_NAMES 表示当前流程使用的 LEARN_TOOL_NAMES 集合。
LEARN_TOOL_NAMES: tuple[str, ...] = (
    "load_skill", "compress", "background_run", "check_background", "write_stdin", "task_create", "task_get", "task_update",
    "task_list", "spawn_teammate", "list_teammates", "send_message", "read_inbox", "broadcast",
    "shutdown_request", "integrate_teammate", "plan_approval", "idle", "claim_task",
)
