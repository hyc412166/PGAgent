"""Durable local tools mirrored from Learn Claude Code and Claw Code.

The implementations are intentionally honest: local filesystem, subprocess,
task-board, worker-state and message-bus operations are real; MCP operations
require an explicitly configured local resource catalog and otherwise return a
typed unavailable result instead of pretending that a remote action happened.
"""
# 文件职责：负责工具定义、授权、注册、调度与执行中的 advanced 子模块。
# 逻辑关系：上层通过 tools/advanced.py 使用本模块；本模块把处理结果交给同领域服务、持久化层或 API 响应层。

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


# 变量说明：_STATE_LOCK 表示当前步骤使用的 _STATE_LOCK 值。
_STATE_LOCK = threading.RLock()
# 变量说明：_BACKGROUND_LOCK 表示当前步骤使用的 _BACKGROUND_LOCK 值。
_BACKGROUND_LOCK = threading.RLock()
# 变量说明：_BACKGROUND 表示当前步骤使用的 _BACKGROUND 值。
_BACKGROUND: dict[str, dict[str, Any]] = {}


# 函数职责：完成 utcnow 对应的业务处理。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


# 函数职责：完成 json 对应的业务处理。
# 参数关系：value 表示当前字段或计算值。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


# 函数职责：完成 state_dir 对应的业务处理。
# 参数关系：sandbox 表示当前步骤使用的 sandbox 值；create 表示当前步骤使用的 create 值。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def _state_dir(sandbox: WorkspaceSandbox, *, create: bool = False) -> Path:
    # 变量说明：path 表示当前文件或目录路径。
    path = sandbox.root / ".pgagent"
    if create:
        path.mkdir(parents=True, exist_ok=True)
    return path


# 函数职责：完成 state_path 对应的业务处理。
# 参数关系：sandbox 表示当前步骤使用的 sandbox 值；name 表示当前对象名称；create_parent 表示当前步骤使用的 create_parent 值。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def _state_path(sandbox: WorkspaceSandbox, name: str, *, create_parent: bool = False) -> Path:
    if not re.fullmatch(r"[a-z0-9_-]+\.json", name):
        raise ValueError("invalid state file name")
    return _state_dir(sandbox, create=create_parent) / name


# 函数职责：完成 read_state 对应的业务处理。
# 参数关系：sandbox 表示当前步骤使用的 sandbox 值；name 表示当前对象名称；default 表示当前步骤使用的 default 值。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def _read_state(sandbox: WorkspaceSandbox, name: str, default: Any) -> Any:
    # 变量说明：path 表示当前文件或目录路径。
    path = _state_path(sandbox, name)
    with _STATE_LOCK:
        if not path.is_file():
            return json.loads(json.dumps(default))
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return json.loads(json.dumps(default))


# 函数职责：完成 write_state 对应的业务处理。
# 参数关系：sandbox 表示当前步骤使用的 sandbox 值；name 表示当前对象名称；value 表示当前字段或计算值。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def _write_state(sandbox: WorkspaceSandbox, name: str, value: Any) -> None:
    # 变量说明：path 表示当前文件或目录路径。
    path = _state_path(sandbox, name, create_parent=True)
    # 变量说明：encoded 表示当前步骤使用的 encoded 值。
    encoded = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True, default=str) + "\n"
    with _STATE_LOCK:
        # 变量说明：temporary 表示当前步骤使用的 temporary 值。
        temporary = path.with_suffix(f".{uuid4().hex}.tmp")
        temporary.write_text(encoded, encoding="utf-8")
        temporary.replace(path)


# 函数职责：记录 result 对应的数据或流程。
# 参数关系：tool_name 表示当前步骤使用的 tool_name 值；value 表示当前字段或计算值；changed 表示当前步骤使用的 changed 值。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def _record_result(tool_name: str, value: Any, *, changed: bool = False) -> ToolResult:
    return ToolResult(tool_name, True, _json(value), changed=changed)


# 函数职责：完成 read_file_slice 对应的业务处理。
# 参数关系：sandbox 表示当前步骤使用的 sandbox 值；path 表示当前文件或目录路径；offset 表示当前步骤使用的 offset 值；limit 表示当前步骤使用的 limit 值；max_chars 表示当前流程使用的 max_chars 集合。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
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


# 函数职责：完成 notebook_edit 对应的业务处理。
# 参数关系：sandbox 表示当前步骤使用的 sandbox 值；notebook_path 表示notebook_path 对应的文件系统位置；cell_id 表示cell 对象的唯一标识；new_source 表示当前步骤使用的 new_source 值；cell_type 表示当前步骤使用的 cell_type 值；edit_mode 表示当前步骤使用的 edit_mode 值。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
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
        # 变量说明：target 表示当前步骤使用的 target 值。
        target = sandbox.resolve(notebook_path, must_exist=True)
        if target.suffix.lower() != ".ipynb" or not target.is_file():
            return ToolResult("NotebookEdit", False, "目标必须是现有 .ipynb 文件", error_code="invalid_notebook")
        # 变量说明：notebook 表示当前步骤使用的 notebook 值。
        notebook = json.loads(target.read_text(encoding="utf-8"))
        # 变量说明：cells 表示当前流程使用的 cells 集合。
        cells = notebook.get("cells")
        if not isinstance(cells, list):
            return ToolResult("NotebookEdit", False, "Notebook 缺少 cells 数组", error_code="invalid_notebook")
        # 变量说明：mode 表示当前步骤使用的 mode 值。
        mode = str(edit_mode or "replace").lower()
        if mode not in {"replace", "insert", "delete"}:
            return ToolResult("NotebookEdit", False, "edit_mode 无效", error_code="invalid_arguments")
        # 变量说明：index 表示当前元素的位置索引。
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
            # 变量说明：source 表示当前步骤使用的 source 值。
            source = str(new_source).splitlines(keepends=True)
            if new_source and source and not source[-1].endswith(("\n", "\r")):
                source[-1] += "\n"
            if mode == "replace":
                # 变量说明：cell 表示当前步骤使用的 cell 值。
                cell = dict(cells[int(index)])
                cell["source"] = source
                if cell_type:
                    # 变量说明：cell 的索引项 表示该语句创建或更新的目标数据。
                    cell["cell_type"] = cell_type
                if cell_type == "code":
                    cell.setdefault("outputs", [])
                    cell.setdefault("execution_count", None)
                # 变量说明：cells 的索引项 表示该语句创建或更新的目标数据。
                cells[int(index)] = cell
            else:
                # 变量说明：new_cell 表示当前步骤使用的 new_cell 值。
                new_cell: dict[str, Any] = {
                    "id": str(cell_id or uuid4().hex[:8]),
                    "cell_type": cell_type,
                    "metadata": {},
                    "source": source,
                }
                if cell_type == "code":
                    new_cell.update({"outputs": [], "execution_count": None})
                # 变量说明：insert_at 表示insert_at 对应的时间信息。
                insert_at = len(cells) if index is None else int(index) + 1
                cells.insert(insert_at, new_cell)
        target.write_text(json.dumps(notebook, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
        return _record_result("NotebookEdit", {"path": sandbox.relative(target), "mode": mode, "cell_id": cell_id}, changed=True)
    except (SandboxViolation, OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
        return ToolResult("NotebookEdit", False, str(exc), error_code="notebook_error")


# 函数职责：完成 sleep_tool 对应的业务处理。
# 参数关系：_sandbox 表示当前步骤使用的 _sandbox 值；duration_ms 表示当前流程使用的 duration_ms 集合。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def sleep_tool(_sandbox: WorkspaceSandbox, duration_ms: int) -> ToolResult:
    # 变量说明：duration 表示当前步骤使用的 duration 值。
    duration = min(max(int(duration_ms), 0), 60_000)
    time.sleep(duration / 1000)
    return _record_result("Sleep", {"duration_ms": duration})


# 函数职责：完成 structured_output 对应的业务处理。
# 参数关系：_sandbox 表示当前步骤使用的 _sandbox 值；value 表示当前字段或计算值。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def structured_output(_sandbox: WorkspaceSandbox, **value: Any) -> ToolResult:
    return _record_result("StructuredOutput", value)


# 函数职责：完成 send_user_message 对应的业务处理。
# 参数关系：_sandbox 表示当前步骤使用的 _sandbox 值；message 表示当前消息。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def send_user_message(_sandbox: WorkspaceSandbox, message: str) -> ToolResult:
    """Return an explicit user-facing message without inventing delivery state."""

    return ToolResult("SendUserMessage", True, str(message), metadata={"user_message": True})


# 函数职责：完成 request_compaction 对应的业务处理。
# 参数关系：_sandbox 表示当前步骤使用的 _sandbox 值；reason 表示当前步骤使用的 reason 值。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def request_compaction(_sandbox: WorkspaceSandbox, reason: str = "model_requested") -> ToolResult:
    """Signal the coordinator; the tool itself never rewrites conversation history."""

    return ToolResult(
        "compress",
        True,
        "Conversation compaction requested at the next safe model boundary.",
        metadata={"force_compaction": True, "reason": str(reason or "model_requested")},
    )


# 函数职责：完成 idle_tool 对应的业务处理。
# 参数关系：_sandbox 表示当前步骤使用的 _sandbox 值；duration_ms 表示当前流程使用的 duration_ms 集合。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def idle_tool(_sandbox: WorkspaceSandbox, duration_ms: int = 100) -> ToolResult:
    # 变量说明：duration 表示当前步骤使用的 duration 值。
    duration = min(max(int(duration_ms), 0), 5_000)
    time.sleep(duration / 1000)
    return _record_result("idle", {"duration_ms": duration})


# 函数职责：完成 repl 对应的业务处理。
# 参数关系：sandbox 表示当前步骤使用的 sandbox 值；code 表示当前步骤使用的 code 值；language 表示当前步骤使用的 language 值；timeout_ms 表示当前流程使用的 timeout_ms 集合。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def repl(
    sandbox: WorkspaceSandbox,
    code: str,
    language: str,
    *,
    timeout_ms: int = 30_000,
) -> ToolResult:
    # 变量说明：normalized 表示当前步骤使用的 normalized 值。
    normalized = str(language).strip().lower()
    if normalized in {"python", "py", "python3"}:
        # 变量说明：command 表示当前步骤使用的 command 值。
        command = [sys.executable, "-c", str(code)]
    elif normalized in {"javascript", "js", "node"}:
        # 变量说明：executable 表示当前步骤使用的 executable 值。
        executable = shutil.which("node")
        if not executable:
            return ToolResult("REPL", False, "Node.js 不可用", error_code="runtime_unavailable")
        # 变量说明：command 表示当前步骤使用的 command 值。
        command = [executable, "-e", str(code)]
    else:
        return ToolResult("REPL", False, f"不支持的语言: {language}", error_code="runtime_unavailable")
    try:
        # 变量说明：completed 表示当前步骤使用的 completed 值。
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
        # 变量说明：output 表示当前步骤使用的 output 值。
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


# 函数职责：完成 powershell 对应的业务处理。
# 参数关系：sandbox 表示当前步骤使用的 sandbox 值；command 表示当前步骤使用的 command 值；timeout 表示当前步骤使用的 timeout 值；description 表示当前步骤使用的 description 值；run_in_background 表示当前步骤使用的 run_in_background 值。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
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
    # 变量说明：executable 表示当前步骤使用的 executable 值。
    executable = shutil.which("pwsh") or shutil.which("powershell")
    if not executable:
        return ToolResult("PowerShell", False, "PowerShell 不可用", error_code="runtime_unavailable")
    try:
        # 变量说明：completed 表示当前步骤使用的 completed 值。
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
        # 变量说明：output 表示当前步骤使用的 output 值。
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


# 函数职责：完成 background_worker 对应的业务处理。
# 参数关系：task_id 表示任务标识；sandbox 表示当前步骤使用的 sandbox 值；command 表示当前步骤使用的 command 值；timeout 表示当前步骤使用的 timeout 值；shell 表示当前步骤使用的 shell 值。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def _background_worker(
    task_id: str,
    sandbox: WorkspaceSandbox,
    command: str,
    timeout: int,
    shell: str,
) -> None:
    with _BACKGROUND_LOCK:
        # 变量说明：映射 的索引项 表示该语句创建或更新的目标数据。
        _BACKGROUND[task_id]["status"] = "running"
        # 变量说明：映射 的索引项 表示该语句创建或更新的目标数据。
        _BACKGROUND[task_id]["started_at"] = _utcnow()
    if shell == "powershell":
        # 变量说明：result 表示本步骤产生的结果。
        result = powershell(sandbox, command, timeout=timeout, run_in_background=False)
    else:
        # 变量说明：result 表示本步骤产生的结果。
        result = builtins.run_command(sandbox, command, approved=True, timeout_seconds=timeout, output_limit=100_000)
    with _BACKGROUND_LOCK:
        # 变量说明：state 表示当前流程的可变状态。
        state = _BACKGROUND[task_id]
        state.update({
            "status": "completed" if result.ok else "failed",
            "finished_at": _utcnow(),
            "result": result.to_dict(),
        })


# 函数职责：完成 background_run 对应的业务处理。
# 参数关系：sandbox 表示当前步骤使用的 sandbox 值；command 表示当前步骤使用的 command 值；timeout 表示当前步骤使用的 timeout 值；shell 表示当前步骤使用的 shell 值；plan_step_id 表示plan_step 对象的唯一标识。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def background_run(
    sandbox: WorkspaceSandbox,
    command: str,
    *,
    timeout: int = 120,
    shell: str = "command",
    plan_step_id: str = "",
) -> ToolResult:
    # 变量说明：task_id 表示任务标识。
    task_id = f"bg-{uuid4().hex[:12]}"
    # 变量说明：state 表示当前流程的可变状态。
    state = {
        "id": task_id,
        "command": str(command),
        "status": "queued",
        "created_at": _utcnow(),
        "workspace": str(sandbox.root),
        "plan_step_id": str(plan_step_id or ""),
    }
    with _BACKGROUND_LOCK:
        # 变量说明：_BACKGROUND 的索引项 表示该语句创建或更新的目标数据。
        _BACKGROUND[task_id] = state
    # 变量说明：thread 表示当前步骤使用的 thread 值。
    thread = threading.Thread(
        target=_background_worker,
        args=(task_id, sandbox, str(command), min(max(int(timeout), 1), 120), shell),
        name=f"pgagent-{task_id}",
        daemon=True,
    )
    thread.start()
    return _record_result("background_run", state, changed=True)


# 函数职责：检查 background 对应的数据或流程。
# 参数关系：_sandbox 表示当前步骤使用的 _sandbox 值；task_id 表示任务标识。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def check_background(_sandbox: WorkspaceSandbox, task_id: str | None = None) -> ToolResult:
    with _BACKGROUND_LOCK:
        if task_id:
            # 变量说明：state 表示当前流程的可变状态。
            state = _BACKGROUND.get(str(task_id))
            if state is None:
                return ToolResult("check_background", False, "后台任务不存在", error_code="task_not_found")
            return _record_result("check_background", dict(state))
        return _record_result("check_background", [dict(item) for item in _BACKGROUND.values()])


# 函数职责：完成 tasks 对应的业务处理。
# 参数关系：sandbox 表示当前步骤使用的 sandbox 值。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def _tasks(sandbox: WorkspaceSandbox) -> list[dict[str, Any]]:
    # 变量说明：value 表示当前字段或计算值。
    value = _read_state(sandbox, "tasks.json", [])
    return [dict(item) for item in value if isinstance(item, dict)]


# 函数职责：完成 task_create 对应的业务处理。
# 参数关系：sandbox 表示当前步骤使用的 sandbox 值；subject 表示当前步骤使用的 subject 值；description 表示当前步骤使用的 description 值；prompt 表示当前步骤使用的 prompt 值；packet 表示当前步骤使用的 packet 值；tool_name 表示当前步骤使用的 tool_name 值。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def task_create(
    sandbox: WorkspaceSandbox,
    *,
    subject: str | None = None,
    description: str = "",
    prompt: str | None = None,
    packet: Mapping[str, Any] | None = None,
    tool_name: str = "task_create",
) -> ToolResult:
    # 变量说明：title 表示当前步骤使用的 title 值。
    title = str(subject or description or prompt or (packet or {}).get("objective") or "").strip()
    if not title:
        return ToolResult(tool_name, False, "任务标题不能为空", error_code="invalid_arguments")
    with _STATE_LOCK:
        # 变量说明：tasks 表示当前流程使用的 tasks 集合。
        tasks = _tasks(sandbox)
        # 变量说明：numeric 表示当前步骤使用的 numeric 值。
        numeric = [int(item["id"]) for item in tasks if str(item.get("id") or "").isdigit()]
        # 变量说明：task 表示当前步骤使用的 task 值。
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


# 函数职责：完成 task_get 对应的业务处理。
# 参数关系：sandbox 表示当前步骤使用的 sandbox 值；task_id 表示任务标识；tool_name 表示当前步骤使用的 tool_name 值。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def task_get(sandbox: WorkspaceSandbox, task_id: str | int, *, tool_name: str = "task_get") -> ToolResult:
    # 变量说明：task 表示当前步骤使用的 task 值。
    task = next((item for item in _tasks(sandbox) if str(item.get("id")) == str(task_id)), None)
    if task is None:
        return ToolResult(tool_name, False, "任务不存在", error_code="task_not_found")
    return _record_result(tool_name, task)


# 函数职责：完成 task_list 对应的业务处理。
# 参数关系：sandbox 表示当前步骤使用的 sandbox 值；tool_name 表示当前步骤使用的 tool_name 值。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def task_list(sandbox: WorkspaceSandbox, *, tool_name: str = "task_list") -> ToolResult:
    return _record_result(tool_name, _tasks(sandbox))


# 函数职责：完成 task_update 对应的业务处理。
# 参数关系：sandbox 表示当前步骤使用的 sandbox 值；task_id 表示任务标识；status 表示当前对象或运行的状态；message 表示当前消息；add_blocked_by 表示当前步骤使用的 add_blocked_by 值；remove_blocked_by 表示当前步骤使用的 remove_blocked_by 值；output 表示当前步骤使用的 output 值；tool_name 表示当前步骤使用的 tool_name 值。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
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
    # 变量说明：allowed 表示当前步骤使用的 allowed 值。
    allowed = {"pending", "in_progress", "completed", "deleted", "stopped", "failed"}
    with _STATE_LOCK:
        # 变量说明：tasks 表示当前流程使用的 tasks 集合。
        tasks = _tasks(sandbox)
        # 变量说明：task 表示当前步骤使用的 task 值。
        task = next((item for item in tasks if str(item.get("id")) == str(task_id)), None)
        if task is None:
            return ToolResult(tool_name, False, "任务不存在", error_code="task_not_found")
        if status is not None:
            if status not in allowed:
                return ToolResult(tool_name, False, "任务状态无效", error_code="invalid_arguments")
            # 变量说明：task 的索引项 表示该语句创建或更新的目标数据。
            task["status"] = status
        # 变量说明：blocked 表示当前步骤使用的 blocked 值。
        blocked = {str(item) for item in task.get("blockedBy") or []}
        blocked.update(str(item) for item in add_blocked_by or [])
        blocked.difference_update(str(item) for item in remove_blocked_by or [])
        # 变量说明：task 的索引项 表示该语句创建或更新的目标数据。
        task["blockedBy"] = sorted(blocked)
        if message:
            task.setdefault("messages", []).append({"at": _utcnow(), "message": str(message)})
        if output is not None:
            # 变量说明：task 的索引项 表示该语句创建或更新的目标数据。
            task["output"] = str(output)
        # 变量说明：task 的索引项 表示该语句创建或更新的目标数据。
        task["updated_at"] = _utcnow()
        _write_state(sandbox, "tasks.json", tasks)
    return _record_result(tool_name, task, changed=True)


# 函数职责：完成 task_claim 对应的业务处理。
# 参数关系：sandbox 表示当前步骤使用的 sandbox 值；task_id 表示任务标识；owner 表示当前步骤使用的 owner 值。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def task_claim(sandbox: WorkspaceSandbox, task_id: str | int, owner: str = "lead") -> ToolResult:
    with _STATE_LOCK:
        # 变量说明：tasks 表示当前流程使用的 tasks 集合。
        tasks = _tasks(sandbox)
        # 变量说明：task 表示当前步骤使用的 task 值。
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


# 函数职责：完成 task_output 对应的业务处理。
# 参数关系：sandbox 表示当前步骤使用的 sandbox 值；task_id 表示任务标识。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def task_output(sandbox: WorkspaceSandbox, task_id: str | int) -> ToolResult:
    # 变量说明：result 表示本步骤产生的结果。
    result = task_get(sandbox, task_id, tool_name="TaskOutput")
    if not result.ok:
        return result
    # 变量说明：task 表示当前步骤使用的 task 值。
    task = json.loads(result.content)
    return _record_result("TaskOutput", {"task_id": task_id, "status": task.get("status"), "output": task.get("output", ""), "messages": task.get("messages", [])})


# 函数职责：完成 config_tool 对应的业务处理。
# 参数关系：sandbox 表示当前步骤使用的 sandbox 值；setting 表示当前步骤使用的 setting 值；value 表示当前字段或计算值。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def config_tool(sandbox: WorkspaceSandbox, setting: str, value: str | bool | float | int | None = None) -> ToolResult:
    # 变量说明：settings 表示应用设置集合。
    settings = _read_state(sandbox, "settings.json", {})
    # 变量说明：key 表示用于查找或映射的键。
    key = str(setting).strip()
    if not key:
        return ToolResult("Config", False, "setting 不能为空", error_code="invalid_arguments")
    if value is None:
        return _record_result("Config", {"setting": key, "value": settings.get(key), "exists": key in settings})
    # 变量说明：settings 的索引项 表示该语句创建或更新的目标数据。
    settings[key] = value
    _write_state(sandbox, "settings.json", settings)
    return _record_result("Config", {"setting": key, "value": value}, changed=True)


# 函数职责：完成 plan_mode_enabled 对应的业务处理。
# 参数关系：sandbox 表示当前步骤使用的 sandbox 值。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def plan_mode_enabled(sandbox: WorkspaceSandbox) -> bool:
    return bool(_read_state(sandbox, "settings.json", {}).get("plan_mode", False))


# 函数职责：完成 enter_plan_mode 对应的业务处理。
# 参数关系：sandbox 表示当前步骤使用的 sandbox 值。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def enter_plan_mode(sandbox: WorkspaceSandbox) -> ToolResult:
    # 变量说明：settings 表示应用设置集合。
    settings = _read_state(sandbox, "settings.json", {})
    settings["plan_mode"] = True
    _write_state(sandbox, "settings.json", settings)
    return _record_result("EnterPlanMode", {"plan_mode": True}, changed=True)


# 函数职责：完成 exit_plan_mode 对应的业务处理。
# 参数关系：sandbox 表示当前步骤使用的 sandbox 值；plan 表示当前步骤使用的 plan 值。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def exit_plan_mode(sandbox: WorkspaceSandbox, plan: str = "") -> ToolResult:
    # 变量说明：settings 表示应用设置集合。
    settings = _read_state(sandbox, "settings.json", {})
    settings["plan_mode"] = False
    settings["last_plan"] = str(plan)
    _write_state(sandbox, "settings.json", settings)
    return _record_result("ExitPlanMode", {"plan_mode": False, "plan": str(plan)}, changed=True)


# 函数职责：完成 memory_write 对应的业务处理。
# 参数关系：sandbox 表示当前步骤使用的 sandbox 值；name 表示当前对象名称；body 表示当前步骤使用的 body 值；memory_type 表示当前步骤使用的 memory_type 值；description 表示当前步骤使用的 description 值。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
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
    # 变量说明：slug 表示当前步骤使用的 slug 值。
    slug = re.sub(r"[^a-zA-Z0-9_-]+", "-", str(name).strip()).strip("-").lower() or "memory"
    # 变量说明：directory 表示当前步骤使用的 directory 值。
    directory = sandbox.root / ".memory"
    directory.mkdir(parents=True, exist_ok=True)
    # 变量说明：target 表示当前步骤使用的 target 值。
    target = directory / f"{slug}.md"
    # 变量说明：text 表示当前步骤使用的 text 值。
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


# 函数职责：完成 memory_read 对应的业务处理。
# 参数关系：sandbox 表示当前步骤使用的 sandbox 值；name 表示当前对象名称。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def memory_read(sandbox: WorkspaceSandbox, name: str) -> ToolResult:
    # 变量说明：slug 表示当前步骤使用的 slug 值。
    slug = re.sub(r"[^a-zA-Z0-9_-]+", "-", str(name).strip()).strip("-").lower() or "memory"
    # 变量说明：target 表示当前步骤使用的 target 值。
    target = sandbox.root / ".memory" / f"{slug}.md"
    if not target.is_file():
        return ToolResult("MemoryRead", False, "记忆不存在", error_code="memory_not_found")
    return ToolResult("MemoryRead", True, target.read_text(encoding="utf-8", errors="replace"))


# 函数职责：完成 memory_list 对应的业务处理。
# 参数关系：sandbox 表示当前步骤使用的 sandbox 值。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def memory_list(sandbox: WorkspaceSandbox) -> ToolResult:
    # 变量说明：directory 表示当前步骤使用的 directory 值。
    directory = sandbox.root / ".memory"
    # 变量说明：records 表示当前流程使用的 records 集合。
    records = []
    if directory.is_dir():
        for path in sorted(directory.glob("*.md")):
            records.append({"name": path.stem, "file": path.name, "size": path.stat().st_size})
    return _record_result("MemoryList", records)


# 函数职责：完成 memory_search 对应的业务处理。
# 参数关系：sandbox 表示当前步骤使用的 sandbox 值；query 表示当前步骤使用的 query 值；limit 表示当前步骤使用的 limit 值。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def memory_search(sandbox: WorkspaceSandbox, query: str, limit: int = 5) -> ToolResult:
    """Compatibility search for registries not connected to the canonical store."""

    # 变量说明：needle 表示当前步骤使用的 needle 值。
    needle = str(query or "").strip().casefold()
    if not needle:
        return ToolResult("MemorySearch", False, "query is required", error_code="invalid_arguments")
    # 变量说明：directory 表示当前步骤使用的 directory 值。
    directory = sandbox.root / ".memory"
    # 变量说明：records 表示当前流程使用的 records 集合。
    records: list[dict[str, Any]] = []
    if directory.is_dir():
        for path in sorted(directory.glob("*.md")):
            # 变量说明：content 表示待处理或返回的正文内容。
            content = path.read_text(encoding="utf-8", errors="replace")
            if needle in content.casefold() or needle in path.stem.casefold():
                records.append({"name": path.stem, "content": content})
                if len(records) >= max(1, min(20, int(limit))):
                    break
    return _record_result("MemorySearch", records)


# 函数职责：完成 message_state 对应的业务处理。
# 参数关系：sandbox 表示当前步骤使用的 sandbox 值。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def _message_state(sandbox: WorkspaceSandbox) -> dict[str, list[dict[str, Any]]]:
    # 变量说明：value 表示当前字段或计算值。
    value = _read_state(sandbox, "messages.json", {})
    return {str(key): list(items) for key, items in value.items() if isinstance(items, list)}


# 函数职责：完成 send_message 对应的业务处理。
# 参数关系：sandbox 表示当前步骤使用的 sandbox 值；to 表示当前步骤使用的 to 值；content 表示待处理或返回的正文内容；sender 表示当前步骤使用的 sender 值；msg_type 表示当前步骤使用的 msg_type 值。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def send_message(
    sandbox: WorkspaceSandbox,
    to: str,
    content: str,
    *,
    sender: str = "lead",
    msg_type: str = "message",
) -> ToolResult:
    # 变量说明：state 表示当前流程的可变状态。
    state = _message_state(sandbox)
    # 变量说明：message 表示当前消息。
    message = {"id": uuid4().hex[:12], "from": sender, "to": str(to), "type": msg_type, "content": str(content), "created_at": _utcnow()}
    state.setdefault(str(to), []).append(message)
    _write_state(sandbox, "messages.json", state)
    return _record_result("send_message", message, changed=True)


# 函数职责：完成 read_inbox 对应的业务处理。
# 参数关系：sandbox 表示当前步骤使用的 sandbox 值；recipient 表示当前步骤使用的 recipient 值。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def read_inbox(sandbox: WorkspaceSandbox, recipient: str = "lead") -> ToolResult:
    # 变量说明：state 表示当前流程的可变状态。
    state = _message_state(sandbox)
    # 变量说明：messages 表示发送给模型或客户端的消息序列。
    messages = list(state.get(recipient, []))
    state[recipient] = []
    _write_state(sandbox, "messages.json", state)
    return _record_result("read_inbox", messages, changed=bool(messages))


# 函数职责：完成 teams 对应的业务处理。
# 参数关系：sandbox 表示当前步骤使用的 sandbox 值。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def _teams(sandbox: WorkspaceSandbox) -> list[dict[str, Any]]:
    # 变量说明：value 表示当前字段或计算值。
    value = _read_state(sandbox, "teams.json", [])
    return [dict(item) for item in value if isinstance(item, dict)]


# 函数职责：完成 spawn_teammate 对应的业务处理。
# 参数关系：sandbox 表示当前步骤使用的 sandbox 值；name 表示当前对象名称；role 表示当前步骤使用的 role 值；prompt 表示当前步骤使用的 prompt 值。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def spawn_teammate(sandbox: WorkspaceSandbox, name: str, role: str, prompt: str) -> ToolResult:
    with _STATE_LOCK:
        # 变量说明：teams 表示当前流程使用的 teams 集合。
        teams = _teams(sandbox)
        # 变量说明：member 表示当前步骤使用的 member 值。
        member = {"id": f"member-{uuid4().hex[:10]}", "name": str(name), "role": str(role), "prompt": str(prompt), "status": "idle", "created_at": _utcnow()}
        # 变量说明：lead 表示当前步骤使用的 lead 值。
        lead = next((team for team in teams if team.get("id") == "learn-team"), None)
        if lead is None:
            # 变量说明：lead 表示当前步骤使用的 lead 值。
            lead = {"id": "learn-team", "name": "Learn Claude Code team", "members": [], "task_ids": [], "created_at": _utcnow()}
            teams.append(lead)
        lead.setdefault("members", []).append(member)
        _write_state(sandbox, "teams.json", teams)
    return _record_result("spawn_teammate", member, changed=True)


# 函数职责：列出 teammates 对应的数据或流程。
# 参数关系：sandbox 表示当前步骤使用的 sandbox 值。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def list_teammates(sandbox: WorkspaceSandbox) -> ToolResult:
    # 变量说明：members 表示当前流程使用的 members 集合。
    members = [member for team in _teams(sandbox) for member in team.get("members") or []]
    return _record_result("list_teammates", members)


# 函数职责：完成 broadcast 对应的业务处理。
# 参数关系：sandbox 表示当前步骤使用的 sandbox 值；content 表示待处理或返回的正文内容。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def broadcast(sandbox: WorkspaceSandbox, content: str) -> ToolResult:
    # 变量说明：members 表示当前流程使用的 members 集合。
    members = [str(member.get("name")) for team in _teams(sandbox) for member in team.get("members") or []]
    # 变量说明：sent 表示当前步骤使用的 sent 值。
    sent = []
    for member in members:
        # 变量说明：result 表示本步骤产生的结果。
        result = send_message(sandbox, member, content)
        if result.ok:
            sent.append(member)
    return _record_result("broadcast", {"sent_to": sent}, changed=bool(sent))


# 函数职责：完成 shutdown_request 对应的业务处理。
# 参数关系：sandbox 表示当前步骤使用的 sandbox 值；teammate 表示当前步骤使用的 teammate 值。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def shutdown_request(sandbox: WorkspaceSandbox, teammate: str) -> ToolResult:
    return send_message(sandbox, teammate, "Please shut down.", msg_type="shutdown_request")


# 函数职责：完成 integrate_teammate 对应的业务处理。
# 参数关系：_sandbox 表示当前步骤使用的 _sandbox 值；teammate 表示当前步骤使用的 teammate 值；commit_message 表示当前步骤使用的 commit_message 值；paths 表示当前流程使用的 paths 集合。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
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


# 函数职责：完成 plan_approval 对应的业务处理。
# 参数关系：sandbox 表示当前步骤使用的 sandbox 值；request_id 表示request 对象的唯一标识；approve 表示当前步骤使用的 approve 值；feedback 表示当前步骤使用的 feedback 值。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def plan_approval(sandbox: WorkspaceSandbox, request_id: str, approve: bool, feedback: str = "") -> ToolResult:
    # 变量说明：state 表示当前流程的可变状态。
    state = _read_state(sandbox, "plan_requests.json", {})
    # 变量说明：request 表示调用方传入的请求数据。
    request = dict(state.get(str(request_id)) or {})
    request.update({"request_id": str(request_id), "status": "approved" if approve else "rejected", "feedback": str(feedback), "updated_at": _utcnow()})
    state[str(request_id)] = request
    _write_state(sandbox, "plan_requests.json", state)
    return _record_result("plan_approval", request, changed=True)


# 函数职责：完成 team_create 对应的业务处理。
# 参数关系：sandbox 表示当前步骤使用的 sandbox 值；name 表示当前对象名称；tasks 表示当前流程使用的 tasks 集合。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def team_create(sandbox: WorkspaceSandbox, name: str, tasks: Sequence[Mapping[str, Any]]) -> ToolResult:
    with _STATE_LOCK:
        # 变量说明：teams 表示当前流程使用的 teams 集合。
        teams = _teams(sandbox)
        # 变量说明：team_id 表示team 对象的唯一标识。
        team_id = f"team-{uuid4().hex[:10]}"
        # 变量说明：task_ids 表示task 对象标识集合。
        task_ids: list[int] = []
        for raw in tasks:
            # 变量说明：created 表示当前步骤使用的 created 值。
            created = task_create(
                sandbox,
                subject=str(raw.get("description") or raw.get("prompt") or "team task"),
                prompt=str(raw.get("prompt") or ""),
                tool_name="TaskCreate",
            )
            if created.ok:
                task_ids.append(int(json.loads(created.content)["id"]))
        # 变量说明：team 表示当前步骤使用的 team 值。
        team = {"id": team_id, "name": str(name), "members": [], "task_ids": task_ids, "status": "created", "created_at": _utcnow()}
        teams.append(team)
        _write_state(sandbox, "teams.json", teams)
    return _record_result("TeamCreate", team, changed=True)


# 函数职责：完成 team_delete 对应的业务处理。
# 参数关系：sandbox 表示当前步骤使用的 sandbox 值；team_id 表示team 对象的唯一标识。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def team_delete(sandbox: WorkspaceSandbox, team_id: str) -> ToolResult:
    # 变量说明：teams 表示当前流程使用的 teams 集合。
    teams = _teams(sandbox)
    # 变量说明：team 表示当前步骤使用的 team 值。
    team = next((item for item in teams if str(item.get("id")) == str(team_id)), None)
    if team is None:
        return ToolResult("TeamDelete", False, "团队不存在", error_code="team_not_found")
    for task_id in team.get("task_ids") or []:
        task_update(sandbox, task_id, status="stopped", tool_name="TaskStop")
    # 变量说明：teams 表示当前流程使用的 teams 集合。
    teams = [item for item in teams if str(item.get("id")) != str(team_id)]
    _write_state(sandbox, "teams.json", teams)
    return _record_result("TeamDelete", {"team_id": team_id, "stopped_tasks": team.get("task_ids", [])}, changed=True)


# 函数职责：完成 worker_create 对应的业务处理。
# 参数关系：sandbox 表示当前步骤使用的 sandbox 值；cwd 表示当前步骤使用的 cwd 值；trusted_roots 表示当前流程使用的 trusted_roots 集合；auto_recover_prompt_misdelivery 表示当前步骤使用的 auto_recover_prompt_misdelivery 值。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def worker_create(sandbox: WorkspaceSandbox, cwd: str, trusted_roots: Sequence[str] | None = None, auto_recover_prompt_misdelivery: bool = True) -> ToolResult:
    try:
        # 变量说明：resolved 表示当前步骤使用的 resolved 值。
        resolved = sandbox.resolve(cwd, must_exist=True)
    except (SandboxViolation, FileNotFoundError, OSError) as exc:
        return ToolResult("WorkerCreate", False, str(exc), error_code="path_error")
    # 变量说明：workers 表示当前流程使用的 workers 集合。
    workers = _read_state(sandbox, "workers.json", {})
    # 变量说明：worker_id 表示worker 对象的唯一标识。
    worker_id = f"worker-{uuid4().hex[:10]}"
    # 变量说明：worker 表示当前步骤使用的 worker 值。
    worker = {
        "id": worker_id,
        "cwd": str(resolved),
        "trusted_roots": list(trusted_roots or [str(sandbox.root)]),
        "auto_recover_prompt_misdelivery": bool(auto_recover_prompt_misdelivery),
        "status": "ready_for_prompt",
        "events": [{"type": "created", "at": _utcnow()}],
        "last_error": "",
    }
    # 变量说明：workers 的索引项 表示该语句创建或更新的目标数据。
    workers[worker_id] = worker
    _write_state(sandbox, "workers.json", workers)
    return _record_result("WorkerCreate", worker, changed=True)


# 函数职责：完成 worker_action 对应的业务处理。
# 参数关系：sandbox 表示当前步骤使用的 sandbox 值；worker_id 表示worker 对象的唯一标识；action 表示当前步骤使用的 action 值；payload 表示跨层传递的数据载荷。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def _worker_action(sandbox: WorkspaceSandbox, worker_id: str, action: str, **payload: Any) -> ToolResult:
    # 变量说明：workers 表示当前流程使用的 workers 集合。
    workers = _read_state(sandbox, "workers.json", {})
    # 变量说明：worker 表示当前步骤使用的 worker 值。
    worker = dict(workers.get(str(worker_id)) or {})
    if not worker:
        return ToolResult(action, False, "Worker 不存在", error_code="worker_not_found")
    # 变量说明：changed 表示当前步骤使用的 changed 值。
    changed = False
    if action == "WorkerObserve":
        # 变量说明：text 表示当前步骤使用的 text 值。
        text = str(payload.get("screen_text") or "")
        worker["last_screen"] = text[-20_000:]
        if re.search(r"trust|trusted", text, re.IGNORECASE):
            # 变量说明：worker 的索引项 表示该语句创建或更新的目标数据。
            worker["status"] = "awaiting_trust"
        elif text.strip():
            # 变量说明：worker 的索引项 表示该语句创建或更新的目标数据。
            worker["status"] = "ready_for_prompt"
        # 变量说明：changed 表示当前步骤使用的 changed 值。
        changed = True
    elif action == "WorkerResolveTrust":
        worker["status"] = "ready_for_prompt"
        # 变量说明：changed 表示当前步骤使用的 changed 值。
        changed = True
    elif action == "WorkerSendPrompt":
        if worker.get("status") != "ready_for_prompt":
            return ToolResult(action, False, "Worker 尚未 ready_for_prompt", error_code="worker_not_ready")
        # 变量说明：prompt 表示当前步骤使用的 prompt 值。
        prompt = str(payload.get("prompt") or "")
        worker.update({"status": "working", "last_prompt": prompt, "task_receipt": payload.get("task_receipt") or {}})
        # 变量说明：changed 表示当前步骤使用的 changed 值。
        changed = True
    elif action == "WorkerRestart":
        worker.update({"status": "ready_for_prompt", "last_error": ""})
        # 变量说明：changed 表示当前步骤使用的 changed 值。
        changed = True
    elif action == "WorkerTerminate":
        worker["status"] = "terminated"
        # 变量说明：changed 表示当前步骤使用的 changed 值。
        changed = True
    elif action == "WorkerObserveCompletion":
        # 变量说明：reason 表示当前步骤使用的 reason 值。
        reason = str(payload.get("finish_reason") or "")
        worker.update({
            "status": "finished" if reason.lower() in {"stop", "completed", "success"} else "failed",
            "finish_reason": reason,
            "tokens_output": max(0, int(payload.get("tokens_output") or 0)),
        })
        # 变量说明：changed 表示当前步骤使用的 changed 值。
        changed = True
    worker.setdefault("events", []).append({"type": action, "at": _utcnow(), **payload})
    workers[str(worker_id)] = worker
    if changed:
        _write_state(sandbox, "workers.json", workers)
    return _record_result(action, worker, changed=changed)


# 函数职责：完成 worker_await_ready 对应的业务处理。
# 参数关系：sandbox 表示当前步骤使用的 sandbox 值；worker_id 表示worker 对象的唯一标识；timeout_ms 表示当前流程使用的 timeout_ms 集合。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def worker_await_ready(sandbox: WorkspaceSandbox, worker_id: str, timeout_ms: int = 5_000) -> ToolResult:
    # 变量说明：deadline 表示当前步骤使用的 deadline 值。
    deadline = time.monotonic() + min(max(int(timeout_ms), 0), 60_000) / 1000
    while True:
        # 变量说明：workers 表示当前流程使用的 workers 集合。
        workers = _read_state(sandbox, "workers.json", {})
        # 变量说明：worker 表示当前步骤使用的 worker 值。
        worker = dict(workers.get(str(worker_id)) or {})
        if not worker:
            return ToolResult("WorkerAwaitReady", False, "Worker does not exist", error_code="worker_not_found")
        if worker.get("status") in {"ready_for_prompt", "finished", "failed", "terminated"}:
            return _record_result("WorkerAwaitReady", worker)
        if time.monotonic() >= deadline:
            return ToolResult("WorkerAwaitReady", False, _json(worker), error_code="worker_timeout")
        time.sleep(0.05)


# 函数职责：完成 cron_create 对应的业务处理。
# 参数关系：sandbox 表示当前步骤使用的 sandbox 值；schedule 表示当前步骤使用的 schedule 值；prompt 表示当前步骤使用的 prompt 值；description 表示当前步骤使用的 description 值。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def cron_create(sandbox: WorkspaceSandbox, schedule: str, prompt: str, description: str = "") -> ToolResult:
    # 变量说明：crons 表示当前流程使用的 crons 集合。
    crons = _read_state(sandbox, "crons.json", [])
    # 变量说明：cron 表示当前步骤使用的 cron 值。
    cron = {"id": f"cron-{uuid4().hex[:10]}", "schedule": str(schedule), "prompt": str(prompt), "description": str(description), "enabled": True, "created_at": _utcnow()}
    crons.append(cron)
    _write_state(sandbox, "crons.json", crons)
    return _record_result("CronCreate", cron, changed=True)


# 函数职责：完成 cron_list 对应的业务处理。
# 参数关系：sandbox 表示当前步骤使用的 sandbox 值。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def cron_list(sandbox: WorkspaceSandbox) -> ToolResult:
    return _record_result("CronList", _read_state(sandbox, "crons.json", []))


# 函数职责：完成 cron_delete 对应的业务处理。
# 参数关系：sandbox 表示当前步骤使用的 sandbox 值；cron_id 表示cron 对象的唯一标识。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def cron_delete(sandbox: WorkspaceSandbox, cron_id: str) -> ToolResult:
    # 变量说明：crons 表示当前流程使用的 crons 集合。
    crons = _read_state(sandbox, "crons.json", [])
    # 变量说明：kept 表示当前步骤使用的 kept 值。
    kept = [item for item in crons if str(item.get("id")) != str(cron_id)]
    if len(kept) == len(crons):
        return ToolResult("CronDelete", False, "定时任务不存在", error_code="cron_not_found")
    _write_state(sandbox, "crons.json", kept)
    return _record_result("CronDelete", {"cron_id": cron_id}, changed=True)


# 函数职责：完成 lsp_query 对应的业务处理。
# 参数关系：sandbox 表示当前步骤使用的 sandbox 值；action 表示当前步骤使用的 action 值；path 表示当前文件或目录路径；line 表示当前步骤使用的 line 值；character 表示当前步骤使用的 character 值；query 表示当前步骤使用的 query 值。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def lsp_query(
    sandbox: WorkspaceSandbox,
    action: str,
    *,
    path: str | None = None,
    line: int = 0,
    character: int = 0,
    query: str = "",
) -> ToolResult:
    # 变量说明：action 表示当前步骤使用的 action 值。
    action = str(action)
    if action == "diagnostics" and path:
        try:
            # 变量说明：target 表示当前步骤使用的 target 值。
            target = sandbox.resolve(path, must_exist=True)
            # 变量说明：source 表示当前步骤使用的 source 值。
            source = target.read_text(encoding="utf-8", errors="replace")
            # 变量说明：diagnostics 表示当前流程使用的 diagnostics 集合。
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
        # 变量说明：result 表示本步骤产生的结果。
        result = builtins.read_file(sandbox, path, max_chars=500_000)
        if not result.ok:
            # 变量说明：tool_name 表示当前步骤使用的 tool_name 值。
            result.tool_name = "LSP"
            return result
        # 变量说明：symbols 表示当前流程使用的 symbols 集合。
        symbols = []
        for index, text in enumerate(result.content.splitlines(), start=1):
            # 变量说明：match 表示当前步骤使用的 match 值。
            match = re.match(r"\s*(?:async\s+def|def|class|function|const|let|var)\s+([A-Za-z_$][\w$]*)", text)
            if match and (not query or query.lower() in match.group(1).lower()):
                symbols.append({"name": match.group(1), "line": index - 1})
        return _record_result("LSP", {"action": action, "symbols": symbols, "backend": "local-index"})
    if action in {"references", "definition", "hover"} and query:
        # 变量说明：result 表示本步骤产生的结果。
        result = builtins.grep_files(sandbox, re.escape(query), path=path or ".", limit=200)
        # 变量说明：tool_name 表示当前步骤使用的 tool_name 值。
        result.tool_name = "LSP"
        result.metadata.update({"action": action, "line": line, "character": character, "backend": "local-index"})
        return result
    return ToolResult("LSP", False, "该查询需要 path 或 query", error_code="invalid_arguments")


# 函数职责：完成 mcp_list_resources 对应的业务处理。
# 参数关系：sandbox 表示当前步骤使用的 sandbox 值；server 表示当前步骤使用的 server 值。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def mcp_list_resources(sandbox: WorkspaceSandbox, server: str | None = None) -> ToolResult:
    # 变量说明：catalog 表示当前步骤使用的 catalog 值。
    catalog = _read_state(sandbox, "mcp_resources.json", [])
    # 变量说明：resources 表示当前流程使用的 resources 集合。
    resources = [item for item in catalog if isinstance(item, dict) and (not server or item.get("server") == server)]
    return _record_result("ListMcpResources", resources)


# 函数职责：完成 mcp_read_resource 对应的业务处理。
# 参数关系：sandbox 表示当前步骤使用的 sandbox 值；uri 表示当前步骤使用的 uri 值；server 表示当前步骤使用的 server 值。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def mcp_read_resource(sandbox: WorkspaceSandbox, uri: str, server: str | None = None) -> ToolResult:
    # 变量说明：catalog 表示当前步骤使用的 catalog 值。
    catalog = _read_state(sandbox, "mcp_resources.json", [])
    # 变量说明：resource 表示当前步骤使用的 resource 值。
    resource = next((item for item in catalog if isinstance(item, dict) and item.get("uri") == uri and (not server or item.get("server") == server)), None)
    if resource is None:
        return ToolResult("ReadMcpResource", False, "MCP resource 未配置", error_code="mcp_resource_not_found")
    if "content" in resource:
        return _record_result("ReadMcpResource", resource)
    # 变量说明：path 表示当前文件或目录路径。
    path = resource.get("path")
    if path:
        # 变量说明：result 表示本步骤产生的结果。
        result = builtins.read_file(sandbox, str(path), max_chars=1_000_000)
        # 变量说明：tool_name 表示当前步骤使用的 tool_name 值。
        result.tool_name = "ReadMcpResource"
        return result
    return ToolResult("ReadMcpResource", False, "MCP resource 没有可读取内容", error_code="mcp_resource_invalid")


# 函数职责：完成 mcp_unavailable 对应的业务处理。
# 参数关系：_sandbox 表示当前步骤使用的 _sandbox 值；_kwargs 表示当前流程使用的 _kwargs 集合。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def mcp_unavailable(_sandbox: WorkspaceSandbox, **_kwargs: Any) -> ToolResult:
    return ToolResult("MCP", False, "当前工作区没有已连接的可执行 MCP server", error_code="mcp_unavailable")


# 函数职责：完成 remote_trigger 对应的业务处理。
# 参数关系：sandbox 表示当前步骤使用的 sandbox 值；url 表示当前步骤使用的 url 值；method 表示当前步骤使用的 method 值；headers 表示当前流程使用的 headers 集合；body 表示当前步骤使用的 body 值。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
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
        # 变量说明：normalized 表示当前步骤使用的 normalized 值；_host 表示当前步骤使用的 _host 值。
        normalized, _host = builtins._validate_public_http_url(url)
        # 变量说明：response 表示下游返回的响应。
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
        # 变量说明：content 表示待处理或返回的正文内容。
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


# 函数职责：完成 readonly_git 对应的业务处理。
# 参数关系：sandbox 表示当前步骤使用的 sandbox 值；arguments 表示当前流程使用的 arguments 集合；tool_name 表示当前步骤使用的 tool_name 值；max_chars 表示当前流程使用的 max_chars 集合。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def _readonly_git(sandbox: WorkspaceSandbox, arguments: list[str], tool_name: str, max_chars: int = 100_000) -> ToolResult:
    # 变量说明：result 表示本步骤产生的结果。
    result = builtins._run_readonly_git(sandbox, arguments, tool_name=tool_name, max_chars=max_chars)
    # 变量说明：tool_name 表示当前步骤使用的 tool_name 值。
    result.tool_name = tool_name
    return result


# 函数职责：完成 git_log 对应的业务处理。
# 参数关系：sandbox 表示当前步骤使用的 sandbox 值；path 表示当前文件或目录路径；count 表示当前步骤使用的 count 值；oneline 表示当前步骤使用的 oneline 值；author 表示当前步骤使用的 author 值；since 表示当前步骤使用的 since 值；until 表示当前步骤使用的 until 值。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def git_log(sandbox: WorkspaceSandbox, *, path: str | None = None, count: int = 20, oneline: bool = True, author: str | None = None, since: str | None = None, until: str | None = None) -> ToolResult:
    # 变量说明：args 表示当前流程使用的 args 集合。
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


# 函数职责：完成 git_show 对应的业务处理。
# 参数关系：sandbox 表示当前步骤使用的 sandbox 值；commit 表示当前步骤使用的 commit 值；path 表示当前文件或目录路径；stat 表示当前步骤使用的 stat 值；format 表示当前步骤使用的 format 值。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def git_show(sandbox: WorkspaceSandbox, commit: str, *, path: str | None = None, stat: bool = False, format: str = "patch") -> ToolResult:
    # 变量说明：args 表示当前流程使用的 args 集合。
    args = ["git", "--no-pager", "show"]
    # 变量说明：selected 表示当前步骤使用的 selected 值。
    selected = "stat" if stat else str(format or "patch")
    if selected == "stat":
        args.append("--stat")
    elif selected == "metadata":
        args.extend(["--no-patch", "--format=fuller"])
    elif selected != "patch":
        return ToolResult("GitShow", False, "format 无效", error_code="invalid_arguments")
    args.append(f"{commit}:{path}" if path else str(commit))
    return _readonly_git(sandbox, args, "GitShow")


# 函数职责：完成 git_blame 对应的业务处理。
# 参数关系：sandbox 表示当前步骤使用的 sandbox 值；path 表示当前文件或目录路径；start_line 表示当前步骤使用的 start_line 值；end_line 表示当前步骤使用的 end_line 值。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def git_blame(sandbox: WorkspaceSandbox, path: str, *, start_line: int | None = None, end_line: int | None = None) -> ToolResult:
    # 变量说明：args 表示当前流程使用的 args 集合。
    args = ["git", "--no-pager", "blame"]
    if start_line is not None or end_line is not None:
        # 变量说明：start 表示当前步骤使用的 start 值。
        start = max(1, int(start_line or 1))
        # 变量说明：end 表示当前步骤使用的 end 值。
        end = max(start, int(end_line or start))
        args.extend(["-L", f"{start},{end}"])
    args.extend(["--", str(path)])
    return _readonly_git(sandbox, args, "GitBlame")


# 函数职责：完成 tool_search 对应的业务处理。
# 参数关系：_sandbox 表示当前步骤使用的 _sandbox 值；query 表示当前步骤使用的 query 值；catalog 表示当前步骤使用的 catalog 值；max_results 表示当前流程使用的 max_results 集合。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def tool_search(_sandbox: WorkspaceSandbox, query: str, catalog: Mapping[str, Mapping[str, Any]], max_results: int = 20) -> ToolResult:
    # 变量说明：raw 表示当前步骤使用的 raw 值。
    raw = str(query).strip()
    # 变量说明：selected 表示当前步骤使用的 selected 值。
    selected = raw[len("select:"):] if raw.lower().startswith("select:") else ""
    # 变量说明：terms 表示当前流程使用的 terms 集合。
    terms = [item.strip().lower() for item in (selected.split(",") if selected else re.split(r"\s+", raw)) if item.strip()]
    # 变量说明：scored 表示当前步骤使用的 scored 值。
    scored = []
    for name, schema in catalog.items():
        # 变量说明：haystack 表示当前步骤使用的 haystack 值。
        haystack = f"{name} {schema.get('description', '')}".lower()
        # 变量说明：score 表示当前步骤使用的 score 值。
        score = sum(3 if term == name.lower() else 1 for term in terms if term in haystack)
        if score:
            scored.append((score, name, str(schema.get("description") or "")))
    scored.sort(key=lambda item: (-item[0], item[1].lower()))
    return _record_result("ToolSearch", [
        {"name": name, "description": description}
        for _score, name, description in scored[: min(max(int(max_results), 1), 100)]
    ])
