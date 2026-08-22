"""Provider contracts for the Learn Claude Code and Claw Code tool surface.

The names intentionally retain the casing used by the reference projects so
persisted tool calls and model-generated calls can be replayed verbatim.
"""

from __future__ import annotations

from typing import Any


def _schema(description: str, properties: dict[str, Any] | None = None, required: tuple[str, ...] = ()) -> dict[str, Any]:
    parameters: dict[str, Any] = {"type": "object", "properties": properties or {}}
    if required:
        parameters["required"] = list(required)
    return {"description": description, "parameters": parameters}


_STRING = {"type": "string"}
_BOOL = {"type": "boolean"}
_INTEGER = {"type": "integer"}
_NUMBER = {"type": "number"}
_STRINGS = {"type": "array", "items": _STRING}
_OBJECT = {"type": "object", "additionalProperties": True}


ADVANCED_TOOL_SCHEMAS: dict[str, dict[str, Any]] = {
    # Claw Code contracts.
    "edit_file": _schema("Replace exact text in a workspace file.", {"path": _STRING, "old_string": _STRING, "new_string": _STRING, "replace_all": _BOOL}, ("path", "old_string", "new_string")),
    "glob_search": _schema("Find workspace paths by glob pattern.", {"pattern": _STRING, "path": _STRING, "limit": _INTEGER}, ("pattern",)),
    "grep_search": _schema("Search workspace text with a regular expression.", {"pattern": _STRING, "path": _STRING, "glob": _STRING, "case_sensitive": _BOOL, "head_limit": _INTEGER}, ("pattern",)),
    "WebFetch": _schema("Fetch one public HTTP or HTTPS resource.", {"url": _STRING, "timeout_seconds": _NUMBER}, ("url",)),
    "WebSearch": _schema("Search the public web.", {"query": _STRING, "limit": _INTEGER}, ("query",)),
    "TodoWrite": _schema("Replace the current structured todo list.", {"todos": {"type": "array", "items": {"type": "object", "properties": {"id": _STRING, "content": _STRING, "status": {"type": "string", "enum": ["pending", "in_progress", "completed", "cancelled"]}, "activeForm": _STRING}, "required": ["content", "status"]}}}, ("todos",)),
    "Skill": _schema("Load the full instructions for an enabled skill.", {"skill": _STRING, "skill_id": _STRING, "name": _STRING}),
    "Agent": _schema("Delegate a task to an enabled child agent.", {"prompt": _STRING, "description": _STRING, "subagent_type": _STRING, "name": _STRING, "run_in_background": _BOOL}, ("prompt",)),
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
    "MemoryWrite": _schema("Write a durable, explicit workspace memory file.", {"name": _STRING, "body": _STRING, "memory_type": _STRING, "description": _STRING}, ("name", "body")),
    "MemoryRead": _schema("Read one explicit workspace memory file.", {"name": _STRING}, ("name",)),
    "MemoryList": _schema("List explicit workspace memory files.", {}),

    # Learn Claude Code contracts.
    "load_skill": _schema("Load one enabled skill by id or name.", {"skill_id": _STRING, "name": _STRING}),
    "compress": _schema("Request a full conversation compaction at the next safe boundary.", {"reason": _STRING}),
    "background_run": _schema("Start a bounded background command.", {"command": _STRING, "timeout": _INTEGER, "shell": _STRING}, ("command",)),
    "check_background": _schema("Check one or all background commands.", {"task_id": _STRING}),
    "task_create": _schema("Create a durable shared task.", {"subject": _STRING, "description": _STRING}, ("subject",)),
    "task_get": _schema("Get one durable shared task.", {"task_id": {"oneOf": [_STRING, _INTEGER]}}, ("task_id",)),
    "task_update": _schema("Update one durable shared task.", {"task_id": {"oneOf": [_STRING, _INTEGER]}, "status": _STRING, "message": _STRING, "add_blocked_by": _STRINGS, "remove_blocked_by": _STRINGS, "output": _STRING}, ("task_id",)),
    "task_list": _schema("List durable shared tasks.", {}),
    "spawn_teammate": _schema("Create a durable teammate record.", {"name": _STRING, "role": _STRING, "prompt": _STRING}, ("name", "role", "prompt")),
    "list_teammates": _schema("List durable teammate records.", {}),
    "send_message": _schema("Send a durable teammate message.", {"to": _STRING, "content": _STRING, "sender": _STRING, "msg_type": _STRING}, ("to", "content")),
    "read_inbox": _schema("Read and drain a teammate inbox.", {"recipient": _STRING}),
    "broadcast": _schema("Send one message to all teammates.", {"content": _STRING}, ("content",)),
    "shutdown_request": _schema("Ask a teammate to shut down.", {"teammate": _STRING}, ("teammate",)),
    "plan_approval": _schema("Approve or reject a teammate plan with feedback.", {"request_id": _STRING, "approve": _BOOL, "feedback": _STRING}, ("request_id", "approve")),
    "idle": _schema("Yield while waiting for teammate or background progress.", {"duration_ms": _INTEGER}),
    "claim_task": _schema("Atomically claim an unblocked shared task.", {"task_id": {"oneOf": [_STRING, _INTEGER]}, "owner": _STRING}, ("task_id",)),
}


CLAW_TOOL_NAMES: tuple[str, ...] = (
    "edit_file", "glob_search", "grep_search", "WebFetch", "WebSearch", "TodoWrite", "Skill", "Agent",
    "ToolSearch", "NotebookEdit", "Sleep", "SendUserMessage", "Config", "EnterPlanMode", "ExitPlanMode",
    "StructuredOutput", "REPL", "PowerShell", "AskUserQuestion", "TaskCreate", "RunTaskPacket", "TaskGet",
    "TaskList", "TaskStop", "TaskUpdate", "TaskOutput", "WorkerCreate", "WorkerGet", "WorkerObserve",
    "WorkerResolveTrust", "WorkerAwaitReady", "WorkerSendPrompt", "WorkerRestart", "WorkerTerminate",
    "WorkerObserveCompletion", "TeamCreate", "TeamDelete", "CronCreate", "CronDelete", "CronList", "LSP",
    "ListMcpResources", "ReadMcpResource", "McpAuth", "RemoteTrigger", "MCP", "TestingPermission",
    "GitStatus", "GitDiff", "GitLog", "GitShow", "GitBlame", "MemoryWrite", "MemoryRead", "MemoryList",
)

LEARN_TOOL_NAMES: tuple[str, ...] = (
    "load_skill", "compress", "background_run", "check_background", "task_create", "task_get", "task_update",
    "task_list", "spawn_teammate", "list_teammates", "send_message", "read_inbox", "broadcast",
    "shutdown_request", "plan_approval", "idle", "claim_task",
)
