"""Durable local tools mirrored from Learn Claude Code and Claw Code.

The implementations are intentionally honest: local filesystem, subprocess,
task-board, worker-state and message-bus operations are real; MCP operations
require an explicitly configured local resource catalog and otherwise return a
typed unavailable result instead of pretending that a remote action happened.
"""

from __future__ import annotations

import ast
import hashlib
import json
import re
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence
from uuid import uuid4

import httpx

from . import builtins
from .sandbox import SandboxViolation, WorkspaceSandbox
from .types import ToolResult


_STATE_LOCK = threading.RLock()
_BACKGROUND_LOCK = threading.RLock()
_BACKGROUND: dict[str, dict[str, Any]] = {}


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _state_dir(sandbox: WorkspaceSandbox, *, create: bool = False) -> Path:
    path = sandbox.root / ".pgagent"
    if create:
        path.mkdir(parents=True, exist_ok=True)
    return path


def _state_path(sandbox: WorkspaceSandbox, name: str, *, create_parent: bool = False) -> Path:
    if not re.fullmatch(r"[a-z0-9_-]+\.json", name):
        raise ValueError("invalid state file name")
    return _state_dir(sandbox, create=create_parent) / name


def _read_state(sandbox: WorkspaceSandbox, name: str, default: Any) -> Any:
    path = _state_path(sandbox, name)
    with _STATE_LOCK:
        if not path.is_file():
            return json.loads(json.dumps(default))
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return json.loads(json.dumps(default))


def _write_state(sandbox: WorkspaceSandbox, name: str, value: Any) -> None:
    path = _state_path(sandbox, name, create_parent=True)
    encoded = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True, default=str) + "\n"
    with _STATE_LOCK:
        temporary = path.with_suffix(f".{uuid4().hex}.tmp")
        temporary.write_text(encoded, encoding="utf-8")
        temporary.replace(path)


def _record_result(tool_name: str, value: Any, *, changed: bool = False) -> ToolResult:
    return ToolResult(tool_name, True, _json(value), changed=changed)


def read_file_slice(
    sandbox: WorkspaceSandbox,
    path: str,
    *,
    offset: int = 0,
    limit: int | None = None,
    max_chars: int = 100_000,
) -> ToolResult:
    return builtins.read_file(
        sandbox,
        path,
        offset=offset,
        limit=limit,
        max_chars=max_chars,
    )


def notebook_edit(
    sandbox: WorkspaceSandbox,
    notebook_path: str,
    *,
    cell_id: str | None = None,
    new_source: str = "",
    cell_type: str = "code",
    edit_mode: str = "replace",
) -> ToolResult:
    try:
        target = sandbox.resolve(notebook_path, must_exist=True)
        if target.suffix.lower() != ".ipynb" or not target.is_file():
            return ToolResult("NotebookEdit", False, "目标必须是现有 .ipynb 文件", error_code="invalid_notebook")
        notebook = json.loads(target.read_text(encoding="utf-8"))
        cells = notebook.get("cells")
        if not isinstance(cells, list):
            return ToolResult("NotebookEdit", False, "Notebook 缺少 cells 数组", error_code="invalid_notebook")
        mode = str(edit_mode or "replace").lower()
        if mode not in {"replace", "insert", "delete"}:
            return ToolResult("NotebookEdit", False, "edit_mode 无效", error_code="invalid_arguments")
        index = next(
            (i for i, cell in enumerate(cells) if isinstance(cell, dict) and str(cell.get("id") or "") == str(cell_id or "")),
            None,
        )
        if mode in {"replace", "delete"} and index is None:
            return ToolResult("NotebookEdit", False, f"未找到 cell_id: {cell_id}", error_code="cell_not_found")
        if mode == "delete":
            cells.pop(int(index))
        else:
            if cell_type not in {"code", "markdown"}:
                return ToolResult("NotebookEdit", False, "cell_type 无效", error_code="invalid_arguments")
            source = str(new_source).splitlines(keepends=True)
            if new_source and source and not source[-1].endswith(("\n", "\r")):
                source[-1] += "\n"
            if mode == "replace":
                cell = dict(cells[int(index)])
                cell["source"] = source
                if cell_type:
                    cell["cell_type"] = cell_type
                if cell_type == "code":
                    cell.setdefault("outputs", [])
                    cell.setdefault("execution_count", None)
                cells[int(index)] = cell
            else:
                new_cell: dict[str, Any] = {
                    "id": str(cell_id or uuid4().hex[:8]),
                    "cell_type": cell_type,
                    "metadata": {},
                    "source": source,
                }
                if cell_type == "code":
                    new_cell.update({"outputs": [], "execution_count": None})
                insert_at = len(cells) if index is None else int(index) + 1
                cells.insert(insert_at, new_cell)
        target.write_text(json.dumps(notebook, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
        return _record_result("NotebookEdit", {"path": sandbox.relative(target), "mode": mode, "cell_id": cell_id}, changed=True)
    except (SandboxViolation, OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
        return ToolResult("NotebookEdit", False, str(exc), error_code="notebook_error")


def sleep_tool(_sandbox: WorkspaceSandbox, duration_ms: int) -> ToolResult:
    duration = min(max(int(duration_ms), 0), 60_000)
    time.sleep(duration / 1000)
    return _record_result("Sleep", {"duration_ms": duration})


def structured_output(_sandbox: WorkspaceSandbox, **value: Any) -> ToolResult:
    return _record_result("StructuredOutput", value)


def send_user_message(_sandbox: WorkspaceSandbox, message: str) -> ToolResult:
    """Return an explicit user-facing message without inventing delivery state."""

    return ToolResult("SendUserMessage", True, str(message), metadata={"user_message": True})


def request_compaction(_sandbox: WorkspaceSandbox, reason: str = "model_requested") -> ToolResult:
    """Signal the coordinator; the tool itself never rewrites conversation history."""

    return ToolResult(
        "compress",
        True,
        "Conversation compaction requested at the next safe model boundary.",
        metadata={"force_compaction": True, "reason": str(reason or "model_requested")},
    )


def idle_tool(_sandbox: WorkspaceSandbox, duration_ms: int = 100) -> ToolResult:
    duration = min(max(int(duration_ms), 0), 5_000)
    time.sleep(duration / 1000)
    return _record_result("idle", {"duration_ms": duration})


def repl(
    sandbox: WorkspaceSandbox,
    code: str,
    language: str,
    *,
    timeout_ms: int = 30_000,
) -> ToolResult:
    normalized = str(language).strip().lower()
    if normalized in {"python", "py", "python3"}:
        command = [sys.executable, "-c", str(code)]
    elif normalized in {"javascript", "js", "node"}:
        executable = shutil.which("node")
        if not executable:
            return ToolResult("REPL", False, "Node.js 不可用", error_code="runtime_unavailable")
        command = [executable, "-e", str(code)]
    else:
        return ToolResult("REPL", False, f"不支持的语言: {language}", error_code="runtime_unavailable")
    try:
        completed = subprocess.run(
            command,
            cwd=sandbox.root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=min(max(int(timeout_ms), 1), 120_000) / 1000,
            check=False,
        )
        output = (completed.stdout + completed.stderr)[:100_000]
        return ToolResult(
            "REPL",
            completed.returncode == 0,
            output,
            error_code=None if completed.returncode == 0 else "repl_error",
            metadata={"exit_code": completed.returncode, "language": normalized},
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return ToolResult("REPL", False, str(exc), error_code="timeout" if isinstance(exc, subprocess.TimeoutExpired) else "runtime_unavailable")


def powershell(
    sandbox: WorkspaceSandbox,
    command: str,
    *,
    timeout: int = 30,
    description: str | None = None,
    run_in_background: bool = False,
) -> ToolResult:
    if run_in_background:
        return background_run(sandbox, command=command, timeout=timeout, shell="powershell")
    executable = shutil.which("pwsh") or shutil.which("powershell")
    if not executable:
        return ToolResult("PowerShell", False, "PowerShell 不可用", error_code="runtime_unavailable")
    try:
        completed = subprocess.run(
            [executable, "-NoProfile", "-NonInteractive", "-Command", str(command)],
            cwd=sandbox.root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=min(max(int(timeout), 1), 120),
            check=False,
        )
        output = (completed.stdout + completed.stderr)[:100_000]
        return ToolResult(
            "PowerShell",
            completed.returncode == 0,
            output,
            error_code=None if completed.returncode == 0 else "powershell_error",
            metadata={"exit_code": completed.returncode, "description": description or ""},
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return ToolResult("PowerShell", False, str(exc), error_code="timeout" if isinstance(exc, subprocess.TimeoutExpired) else "runtime_unavailable")


def _background_worker(
    task_id: str,
    sandbox: WorkspaceSandbox,
    command: str,
    timeout: int,
    shell: str,
) -> None:
    with _BACKGROUND_LOCK:
        _BACKGROUND[task_id]["status"] = "running"
        _BACKGROUND[task_id]["started_at"] = _utcnow()
    if shell == "powershell":
        result = powershell(sandbox, command, timeout=timeout, run_in_background=False)
    else:
        result = builtins.run_command(sandbox, command, approved=True, timeout_seconds=timeout, output_limit=100_000)
    with _BACKGROUND_LOCK:
        state = _BACKGROUND[task_id]
        state.update({
            "status": "completed" if result.ok else "failed",
            "finished_at": _utcnow(),
            "result": result.to_dict(),
        })


def background_run(
    sandbox: WorkspaceSandbox,
    command: str,
    *,
    timeout: int = 120,
    shell: str = "command",
    plan_step_id: str = "",
) -> ToolResult:
    task_id = f"bg-{uuid4().hex[:12]}"
    state = {
        "id": task_id,
        "command": str(command),
        "status": "queued",
        "created_at": _utcnow(),
        "workspace": str(sandbox.root),
        "plan_step_id": str(plan_step_id or ""),
    }
    with _BACKGROUND_LOCK:
        _BACKGROUND[task_id] = state
    thread = threading.Thread(
        target=_background_worker,
        args=(task_id, sandbox, str(command), min(max(int(timeout), 1), 120), shell),
        name=f"pgagent-{task_id}",
        daemon=True,
    )
    thread.start()
    return _record_result("background_run", state, changed=True)


def check_background(_sandbox: WorkspaceSandbox, task_id: str | None = None) -> ToolResult:
    with _BACKGROUND_LOCK:
        if task_id:
            state = _BACKGROUND.get(str(task_id))
            if state is None:
                return ToolResult("check_background", False, "后台任务不存在", error_code="task_not_found")
            return _record_result("check_background", dict(state))
        return _record_result("check_background", [dict(item) for item in _BACKGROUND.values()])


def _tasks(sandbox: WorkspaceSandbox) -> list[dict[str, Any]]:
    value = _read_state(sandbox, "tasks.json", [])
    return [dict(item) for item in value if isinstance(item, dict)]


def task_create(
    sandbox: WorkspaceSandbox,
    *,
    subject: str | None = None,
    description: str = "",
    prompt: str | None = None,
    packet: Mapping[str, Any] | None = None,
    tool_name: str = "task_create",
) -> ToolResult:
    title = str(subject or description or prompt or (packet or {}).get("objective") or "").strip()
    if not title:
        return ToolResult(tool_name, False, "任务标题不能为空", error_code="invalid_arguments")
    with _STATE_LOCK:
        tasks = _tasks(sandbox)
        numeric = [int(item["id"]) for item in tasks if str(item.get("id") or "").isdigit()]
        task = {
            "id": max(numeric, default=0) + 1,
            "subject": title[:500],
            "description": str(description or prompt or "")[:10_000],
            "status": "pending",
            "owner": None,
            "blockedBy": [],
            "messages": [],
            "output": "",
            "packet": dict(packet or {}),
            "created_at": _utcnow(),
            "updated_at": _utcnow(),
        }
        tasks.append(task)
        _write_state(sandbox, "tasks.json", tasks)
    return _record_result(tool_name, task, changed=True)


def task_get(sandbox: WorkspaceSandbox, task_id: str | int, *, tool_name: str = "task_get") -> ToolResult:
    task = next((item for item in _tasks(sandbox) if str(item.get("id")) == str(task_id)), None)
    if task is None:
        return ToolResult(tool_name, False, "任务不存在", error_code="task_not_found")
    return _record_result(tool_name, task)


def task_list(sandbox: WorkspaceSandbox, *, tool_name: str = "task_list") -> ToolResult:
    return _record_result(tool_name, _tasks(sandbox))


def task_update(
    sandbox: WorkspaceSandbox,
    task_id: str | int,
    *,
    status: str | None = None,
    message: str | None = None,
    add_blocked_by: Sequence[str | int] | None = None,
    remove_blocked_by: Sequence[str | int] | None = None,
    output: str | None = None,
    tool_name: str = "task_update",
) -> ToolResult:
    allowed = {"pending", "in_progress", "completed", "deleted", "stopped", "failed"}
    with _STATE_LOCK:
        tasks = _tasks(sandbox)
        task = next((item for item in tasks if str(item.get("id")) == str(task_id)), None)
        if task is None:
            return ToolResult(tool_name, False, "任务不存在", error_code="task_not_found")
        if status is not None:
            if status not in allowed:
                return ToolResult(tool_name, False, "任务状态无效", error_code="invalid_arguments")
            task["status"] = status
        blocked = {str(item) for item in task.get("blockedBy") or []}
        blocked.update(str(item) for item in add_blocked_by or [])
        blocked.difference_update(str(item) for item in remove_blocked_by or [])
        task["blockedBy"] = sorted(blocked)
        if message:
            task.setdefault("messages", []).append({"at": _utcnow(), "message": str(message)})
        if output is not None:
            task["output"] = str(output)
        task["updated_at"] = _utcnow()
        _write_state(sandbox, "tasks.json", tasks)
    return _record_result(tool_name, task, changed=True)


def task_claim(sandbox: WorkspaceSandbox, task_id: str | int, owner: str = "lead") -> ToolResult:
    with _STATE_LOCK:
        tasks = _tasks(sandbox)
        task = next((item for item in tasks if str(item.get("id")) == str(task_id)), None)
        if task is None:
            return ToolResult("claim_task", False, "任务不存在", error_code="task_not_found")
        if task.get("owner") not in {None, "", owner}:
            return ToolResult("claim_task", False, f"任务已由 {task['owner']} 领取", error_code="task_claimed")
        if task.get("blockedBy"):
            return ToolResult("claim_task", False, "任务仍有依赖未解除", error_code="task_blocked")
        task.update({"owner": owner, "status": "in_progress", "updated_at": _utcnow()})
        _write_state(sandbox, "tasks.json", tasks)
    return _record_result("claim_task", task, changed=True)


def task_output(sandbox: WorkspaceSandbox, task_id: str | int) -> ToolResult:
    result = task_get(sandbox, task_id, tool_name="TaskOutput")
    if not result.ok:
        return result
    task = json.loads(result.content)
    return _record_result("TaskOutput", {"task_id": task_id, "status": task.get("status"), "output": task.get("output", ""), "messages": task.get("messages", [])})


def config_tool(sandbox: WorkspaceSandbox, setting: str, value: str | bool | float | int | None = None) -> ToolResult:
    settings = _read_state(sandbox, "settings.json", {})
    key = str(setting).strip()
    if not key:
        return ToolResult("Config", False, "setting 不能为空", error_code="invalid_arguments")
    if value is None:
        return _record_result("Config", {"setting": key, "value": settings.get(key), "exists": key in settings})
    settings[key] = value
    _write_state(sandbox, "settings.json", settings)
    return _record_result("Config", {"setting": key, "value": value}, changed=True)


def plan_mode_enabled(sandbox: WorkspaceSandbox) -> bool:
    return bool(_read_state(sandbox, "settings.json", {}).get("plan_mode", False))


def enter_plan_mode(sandbox: WorkspaceSandbox) -> ToolResult:
    settings = _read_state(sandbox, "settings.json", {})
    settings["plan_mode"] = True
    _write_state(sandbox, "settings.json", settings)
    return _record_result("EnterPlanMode", {"plan_mode": True}, changed=True)


def exit_plan_mode(sandbox: WorkspaceSandbox, plan: str = "") -> ToolResult:
    settings = _read_state(sandbox, "settings.json", {})
    settings["plan_mode"] = False
    settings["last_plan"] = str(plan)
    _write_state(sandbox, "settings.json", settings)
    return _record_result("ExitPlanMode", {"plan_mode": False, "plan": str(plan)}, changed=True)


def memory_write(
    sandbox: WorkspaceSandbox,
    name: str,
    body: str,
    *,
    memory_type: str = "project",
    description: str = "",
) -> ToolResult:
    if memory_type not in {"user", "feedback", "project", "reference"}:
        return ToolResult("MemoryWrite", False, "memory_type 无效", error_code="invalid_arguments")
    slug = re.sub(r"[^a-zA-Z0-9_-]+", "-", str(name).strip()).strip("-").lower() or "memory"
    directory = sandbox.root / ".memory"
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / f"{slug}.md"
    text = (
        "---\n"
        f"name: {str(name).strip()}\n"
        f"type: {memory_type}\n"
        f"description: {str(description).strip()}\n"
        "---\n\n"
        f"{str(body).strip()}\n"
    )
    target.write_text(text, encoding="utf-8")
    return _record_result("MemoryWrite", {"name": name, "file": target.name, "type": memory_type}, changed=True)


def memory_read(sandbox: WorkspaceSandbox, name: str) -> ToolResult:
    slug = re.sub(r"[^a-zA-Z0-9_-]+", "-", str(name).strip()).strip("-").lower() or "memory"
    target = sandbox.root / ".memory" / f"{slug}.md"
    if not target.is_file():
        return ToolResult("MemoryRead", False, "记忆不存在", error_code="memory_not_found")
    return ToolResult("MemoryRead", True, target.read_text(encoding="utf-8", errors="replace"))


def memory_list(sandbox: WorkspaceSandbox) -> ToolResult:
    directory = sandbox.root / ".memory"
    records = []
    if directory.is_dir():
        for path in sorted(directory.glob("*.md")):
            records.append({"name": path.stem, "file": path.name, "size": path.stat().st_size})
    return _record_result("MemoryList", records)


def memory_search(sandbox: WorkspaceSandbox, query: str, limit: int = 5) -> ToolResult:
    """Compatibility search for registries not connected to the canonical store."""

    needle = str(query or "").strip().casefold()
    if not needle:
        return ToolResult("MemorySearch", False, "query is required", error_code="invalid_arguments")
    directory = sandbox.root / ".memory"
    records: list[dict[str, Any]] = []
    if directory.is_dir():
        for path in sorted(directory.glob("*.md")):
            content = path.read_text(encoding="utf-8", errors="replace")
            if needle in content.casefold() or needle in path.stem.casefold():
                records.append({"name": path.stem, "content": content})
                if len(records) >= max(1, min(20, int(limit))):
                    break
    return _record_result("MemorySearch", records)


def _message_state(sandbox: WorkspaceSandbox) -> dict[str, list[dict[str, Any]]]:
    value = _read_state(sandbox, "messages.json", {})
    return {str(key): list(items) for key, items in value.items() if isinstance(items, list)}


def send_message(
    sandbox: WorkspaceSandbox,
    to: str,
    content: str,
    *,
    sender: str = "lead",
    msg_type: str = "message",
) -> ToolResult:
    state = _message_state(sandbox)
    message = {"id": uuid4().hex[:12], "from": sender, "to": str(to), "type": msg_type, "content": str(content), "created_at": _utcnow()}
    state.setdefault(str(to), []).append(message)
    _write_state(sandbox, "messages.json", state)
    return _record_result("send_message", message, changed=True)


def read_inbox(sandbox: WorkspaceSandbox, recipient: str = "lead") -> ToolResult:
    state = _message_state(sandbox)
    messages = list(state.get(recipient, []))
    state[recipient] = []
    _write_state(sandbox, "messages.json", state)
    return _record_result("read_inbox", messages, changed=bool(messages))


def _teams(sandbox: WorkspaceSandbox) -> list[dict[str, Any]]:
    value = _read_state(sandbox, "teams.json", [])
    return [dict(item) for item in value if isinstance(item, dict)]


def spawn_teammate(sandbox: WorkspaceSandbox, name: str, role: str, prompt: str) -> ToolResult:
    with _STATE_LOCK:
        teams = _teams(sandbox)
        member = {"id": f"member-{uuid4().hex[:10]}", "name": str(name), "role": str(role), "prompt": str(prompt), "status": "idle", "created_at": _utcnow()}
        lead = next((team for team in teams if team.get("id") == "learn-team"), None)
        if lead is None:
            lead = {"id": "learn-team", "name": "Learn Claude Code team", "members": [], "task_ids": [], "created_at": _utcnow()}
            teams.append(lead)
        lead.setdefault("members", []).append(member)
        _write_state(sandbox, "teams.json", teams)
    return _record_result("spawn_teammate", member, changed=True)


def list_teammates(sandbox: WorkspaceSandbox) -> ToolResult:
    members = [member for team in _teams(sandbox) for member in team.get("members") or []]
    return _record_result("list_teammates", members)


def broadcast(sandbox: WorkspaceSandbox, content: str) -> ToolResult:
    members = [str(member.get("name")) for team in _teams(sandbox) for member in team.get("members") or []]
    sent = []
    for member in members:
        result = send_message(sandbox, member, content)
        if result.ok:
            sent.append(member)
    return _record_result("broadcast", {"sent_to": sent}, changed=bool(sent))


def shutdown_request(sandbox: WorkspaceSandbox, teammate: str) -> ToolResult:
    return send_message(sandbox, teammate, "Please shut down.", msg_type="shutdown_request")


def integrate_teammate(
    _sandbox: WorkspaceSandbox,
    teammate: str,
    commit_message: str = "",
    paths: Sequence[str] = (),
) -> ToolResult:
    return ToolResult(
        "integrate_teammate",
        False,
        "worktree integration requires a durable run-scoped teammate",
        error_code="durable_team_unavailable",
    )


def plan_approval(sandbox: WorkspaceSandbox, request_id: str, approve: bool, feedback: str = "") -> ToolResult:
    state = _read_state(sandbox, "plan_requests.json", {})
    request = dict(state.get(str(request_id)) or {})
    request.update({"request_id": str(request_id), "status": "approved" if approve else "rejected", "feedback": str(feedback), "updated_at": _utcnow()})
    state[str(request_id)] = request
    _write_state(sandbox, "plan_requests.json", state)
    return _record_result("plan_approval", request, changed=True)


def team_create(sandbox: WorkspaceSandbox, name: str, tasks: Sequence[Mapping[str, Any]]) -> ToolResult:
    with _STATE_LOCK:
        teams = _teams(sandbox)
        team_id = f"team-{uuid4().hex[:10]}"
        task_ids: list[int] = []
        for raw in tasks:
            created = task_create(
                sandbox,
                subject=str(raw.get("description") or raw.get("prompt") or "team task"),
                prompt=str(raw.get("prompt") or ""),
                tool_name="TaskCreate",
            )
            if created.ok:
                task_ids.append(int(json.loads(created.content)["id"]))
        team = {"id": team_id, "name": str(name), "members": [], "task_ids": task_ids, "status": "created", "created_at": _utcnow()}
        teams.append(team)
        _write_state(sandbox, "teams.json", teams)
    return _record_result("TeamCreate", team, changed=True)


def team_delete(sandbox: WorkspaceSandbox, team_id: str) -> ToolResult:
    teams = _teams(sandbox)
    team = next((item for item in teams if str(item.get("id")) == str(team_id)), None)
    if team is None:
        return ToolResult("TeamDelete", False, "团队不存在", error_code="team_not_found")
    for task_id in team.get("task_ids") or []:
        task_update(sandbox, task_id, status="stopped", tool_name="TaskStop")
    teams = [item for item in teams if str(item.get("id")) != str(team_id)]
    _write_state(sandbox, "teams.json", teams)
    return _record_result("TeamDelete", {"team_id": team_id, "stopped_tasks": team.get("task_ids", [])}, changed=True)


def worker_create(sandbox: WorkspaceSandbox, cwd: str, trusted_roots: Sequence[str] | None = None, auto_recover_prompt_misdelivery: bool = True) -> ToolResult:
    try:
        resolved = sandbox.resolve(cwd, must_exist=True)
    except (SandboxViolation, FileNotFoundError, OSError) as exc:
        return ToolResult("WorkerCreate", False, str(exc), error_code="path_error")
    workers = _read_state(sandbox, "workers.json", {})
    worker_id = f"worker-{uuid4().hex[:10]}"
    worker = {
        "id": worker_id,
        "cwd": str(resolved),
        "trusted_roots": list(trusted_roots or [str(sandbox.root)]),
        "auto_recover_prompt_misdelivery": bool(auto_recover_prompt_misdelivery),
        "status": "ready_for_prompt",
        "events": [{"type": "created", "at": _utcnow()}],
        "last_error": "",
    }
    workers[worker_id] = worker
    _write_state(sandbox, "workers.json", workers)
    return _record_result("WorkerCreate", worker, changed=True)


def _worker_action(sandbox: WorkspaceSandbox, worker_id: str, action: str, **payload: Any) -> ToolResult:
    workers = _read_state(sandbox, "workers.json", {})
    worker = dict(workers.get(str(worker_id)) or {})
    if not worker:
        return ToolResult(action, False, "Worker 不存在", error_code="worker_not_found")
    changed = False
    if action == "WorkerObserve":
        text = str(payload.get("screen_text") or "")
        worker["last_screen"] = text[-20_000:]
        if re.search(r"trust|trusted", text, re.IGNORECASE):
            worker["status"] = "awaiting_trust"
        elif text.strip():
            worker["status"] = "ready_for_prompt"
        changed = True
    elif action == "WorkerResolveTrust":
        worker["status"] = "ready_for_prompt"
        changed = True
    elif action == "WorkerSendPrompt":
        if worker.get("status") != "ready_for_prompt":
            return ToolResult(action, False, "Worker 尚未 ready_for_prompt", error_code="worker_not_ready")
        prompt = str(payload.get("prompt") or "")
        worker.update({"status": "working", "last_prompt": prompt, "task_receipt": payload.get("task_receipt") or {}})
        changed = True
    elif action == "WorkerRestart":
        worker.update({"status": "ready_for_prompt", "last_error": ""})
        changed = True
    elif action == "WorkerTerminate":
        worker["status"] = "terminated"
        changed = True
    elif action == "WorkerObserveCompletion":
        reason = str(payload.get("finish_reason") or "")
        worker.update({
            "status": "finished" if reason.lower() in {"stop", "completed", "success"} else "failed",
            "finish_reason": reason,
            "tokens_output": max(0, int(payload.get("tokens_output") or 0)),
        })
        changed = True
    worker.setdefault("events", []).append({"type": action, "at": _utcnow(), **payload})
    workers[str(worker_id)] = worker
    if changed:
        _write_state(sandbox, "workers.json", workers)
    return _record_result(action, worker, changed=changed)


def worker_await_ready(sandbox: WorkspaceSandbox, worker_id: str, timeout_ms: int = 5_000) -> ToolResult:
    deadline = time.monotonic() + min(max(int(timeout_ms), 0), 60_000) / 1000
    while True:
        workers = _read_state(sandbox, "workers.json", {})
        worker = dict(workers.get(str(worker_id)) or {})
        if not worker:
            return ToolResult("WorkerAwaitReady", False, "Worker does not exist", error_code="worker_not_found")
        if worker.get("status") in {"ready_for_prompt", "finished", "failed", "terminated"}:
            return _record_result("WorkerAwaitReady", worker)
        if time.monotonic() >= deadline:
            return ToolResult("WorkerAwaitReady", False, _json(worker), error_code="worker_timeout")
        time.sleep(0.05)


def cron_create(sandbox: WorkspaceSandbox, schedule: str, prompt: str, description: str = "") -> ToolResult:
    crons = _read_state(sandbox, "crons.json", [])
    cron = {"id": f"cron-{uuid4().hex[:10]}", "schedule": str(schedule), "prompt": str(prompt), "description": str(description), "enabled": True, "created_at": _utcnow()}
    crons.append(cron)
    _write_state(sandbox, "crons.json", crons)
    return _record_result("CronCreate", cron, changed=True)


def cron_list(sandbox: WorkspaceSandbox) -> ToolResult:
    return _record_result("CronList", _read_state(sandbox, "crons.json", []))


def cron_delete(sandbox: WorkspaceSandbox, cron_id: str) -> ToolResult:
    crons = _read_state(sandbox, "crons.json", [])
    kept = [item for item in crons if str(item.get("id")) != str(cron_id)]
    if len(kept) == len(crons):
        return ToolResult("CronDelete", False, "定时任务不存在", error_code="cron_not_found")
    _write_state(sandbox, "crons.json", kept)
    return _record_result("CronDelete", {"cron_id": cron_id}, changed=True)


def lsp_query(
    sandbox: WorkspaceSandbox,
    action: str,
    *,
    path: str | None = None,
    line: int = 0,
    character: int = 0,
    query: str = "",
) -> ToolResult:
    action = str(action)
    if action == "diagnostics" and path:
        try:
            target = sandbox.resolve(path, must_exist=True)
            source = target.read_text(encoding="utf-8", errors="replace")
            diagnostics: list[dict[str, Any]] = []
            if target.suffix.lower() == ".py":
                try:
                    ast.parse(source, filename=str(target))
                except SyntaxError as exc:
                    diagnostics.append({"line": exc.lineno, "character": exc.offset, "message": exc.msg, "severity": "error"})
            elif target.suffix.lower() == ".json":
                try:
                    json.loads(source)
                except json.JSONDecodeError as exc:
                    diagnostics.append({"line": exc.lineno, "character": exc.colno, "message": exc.msg, "severity": "error"})
            return _record_result("LSP", {"action": action, "path": path, "diagnostics": diagnostics, "backend": "local-parser"})
        except (SandboxViolation, OSError) as exc:
            return ToolResult("LSP", False, str(exc), error_code="path_error")
    if action == "symbols" and path:
        result = builtins.read_file(sandbox, path, max_chars=500_000)
        if not result.ok:
            result.tool_name = "LSP"
            return result
        symbols = []
        for index, text in enumerate(result.content.splitlines(), start=1):
            match = re.match(r"\s*(?:async\s+def|def|class|function|const|let|var)\s+([A-Za-z_$][\w$]*)", text)
            if match and (not query or query.lower() in match.group(1).lower()):
                symbols.append({"name": match.group(1), "line": index - 1})
        return _record_result("LSP", {"action": action, "symbols": symbols, "backend": "local-index"})
    if action in {"references", "definition", "hover"} and query:
        result = builtins.grep_files(sandbox, re.escape(query), path=path or ".", limit=200)
        result.tool_name = "LSP"
        result.metadata.update({"action": action, "line": line, "character": character, "backend": "local-index"})
        return result
    return ToolResult("LSP", False, "该查询需要 path 或 query", error_code="invalid_arguments")


def mcp_list_resources(sandbox: WorkspaceSandbox, server: str | None = None) -> ToolResult:
    catalog = _read_state(sandbox, "mcp_resources.json", [])
    resources = [item for item in catalog if isinstance(item, dict) and (not server or item.get("server") == server)]
    return _record_result("ListMcpResources", resources)


def mcp_read_resource(sandbox: WorkspaceSandbox, uri: str, server: str | None = None) -> ToolResult:
    catalog = _read_state(sandbox, "mcp_resources.json", [])
    resource = next((item for item in catalog if isinstance(item, dict) and item.get("uri") == uri and (not server or item.get("server") == server)), None)
    if resource is None:
        return ToolResult("ReadMcpResource", False, "MCP resource 未配置", error_code="mcp_resource_not_found")
    if "content" in resource:
        return _record_result("ReadMcpResource", resource)
    path = resource.get("path")
    if path:
        result = builtins.read_file(sandbox, str(path), max_chars=1_000_000)
        result.tool_name = "ReadMcpResource"
        return result
    return ToolResult("ReadMcpResource", False, "MCP resource 没有可读取内容", error_code="mcp_resource_invalid")


def mcp_unavailable(_sandbox: WorkspaceSandbox, **_kwargs: Any) -> ToolResult:
    return ToolResult("MCP", False, "当前工作区没有已连接的可执行 MCP server", error_code="mcp_unavailable")


def remote_trigger(
    sandbox: WorkspaceSandbox,
    url: str,
    *,
    method: str = "GET",
    headers: Mapping[str, Any] | None = None,
    body: str | None = None,
) -> ToolResult:
    del sandbox
    try:
        normalized, _host = builtins._validate_public_http_url(url)
        response = httpx.request(
            str(method or "GET").upper(),
            normalized,
            headers={str(key): str(value) for key, value in (headers or {}).items()},
            content=body,
            follow_redirects=False,
            timeout=20,
        )
        if not builtins._response_peer_is_public(response):
            return ToolResult("RemoteTrigger", False, "远端连接解析到非公网地址", error_code="unsafe_url")
        content = response.text[:100_000]
        return ToolResult(
            "RemoteTrigger",
            response.is_success,
            content,
            changed=response.is_success and str(method).upper() != "GET",
            error_code=None if response.is_success else "http_error",
            metadata={"status_code": response.status_code, "url": normalized},
        )
    except Exception as exc:
        return ToolResult("RemoteTrigger", False, str(exc), error_code="network_error")


def _readonly_git(sandbox: WorkspaceSandbox, arguments: list[str], tool_name: str, max_chars: int = 100_000) -> ToolResult:
    result = builtins._run_readonly_git(sandbox, arguments, tool_name=tool_name, max_chars=max_chars)
    result.tool_name = tool_name
    return result


def git_log(sandbox: WorkspaceSandbox, *, path: str | None = None, count: int = 20, oneline: bool = True, author: str | None = None, since: str | None = None, until: str | None = None) -> ToolResult:
    args = ["git", "--no-pager", "log", f"-{min(max(int(count), 1), 200)}"]
    if oneline:
        args.append("--oneline")
    if author:
        args.extend(["--author", str(author)])
    if since:
        args.extend(["--since", str(since)])
    if until:
        args.extend(["--until", str(until)])
    if path:
        args.extend(["--", str(path)])
    return _readonly_git(sandbox, args, "GitLog")


def git_show(sandbox: WorkspaceSandbox, commit: str, *, path: str | None = None, stat: bool = False, format: str = "patch") -> ToolResult:
    args = ["git", "--no-pager", "show"]
    selected = "stat" if stat else str(format or "patch")
    if selected == "stat":
        args.append("--stat")
    elif selected == "metadata":
        args.extend(["--no-patch", "--format=fuller"])
    elif selected != "patch":
        return ToolResult("GitShow", False, "format 无效", error_code="invalid_arguments")
    args.append(f"{commit}:{path}" if path else str(commit))
    return _readonly_git(sandbox, args, "GitShow")


def git_blame(sandbox: WorkspaceSandbox, path: str, *, start_line: int | None = None, end_line: int | None = None) -> ToolResult:
    args = ["git", "--no-pager", "blame"]
    if start_line is not None or end_line is not None:
        start = max(1, int(start_line or 1))
        end = max(start, int(end_line or start))
        args.extend(["-L", f"{start},{end}"])
    args.extend(["--", str(path)])
    return _readonly_git(sandbox, args, "GitBlame")


def tool_search(_sandbox: WorkspaceSandbox, query: str, catalog: Mapping[str, Mapping[str, Any]], max_results: int = 20) -> ToolResult:
    raw = str(query).strip()
    selected = raw[len("select:"):] if raw.lower().startswith("select:") else ""
    terms = [item.strip().lower() for item in (selected.split(",") if selected else re.split(r"\s+", raw)) if item.strip()]
    scored = []
    for name, schema in catalog.items():
        haystack = f"{name} {schema.get('description', '')}".lower()
        score = sum(3 if term == name.lower() else 1 for term in terms if term in haystack)
        if score:
            scored.append((score, name, str(schema.get("description") or "")))
    scored.sort(key=lambda item: (-item[0], item[1].lower()))
    return _record_result("ToolSearch", [
        {"name": name, "description": description}
        for _score, name, description in scored[: min(max(int(max_results), 1), 100)]
    ])
