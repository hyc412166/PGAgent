"""Durable long-running command jobs owned by Agent runs."""
# 文件职责：负责后台任务图及状态调度中的 background 子模块。
# 逻辑关系：上层通过 tasks/background.py 使用本模块；本模块把处理结果交给同领域服务、持久化层或 API 响应层。

from __future__ import annotations

import json
import logging
import os
import shlex
import shutil
import signal
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from sqlalchemy import select, text, update

from src.persistence import database as database_module
from src.config import settings
from src.persistence.database import BackgroundJob, CollaborationEvent, ConversationTurn, PlanStep, Run
from src.observability import bind_observability_context
from src.tasks.graph import ready_steps, refresh_task_state
from src.tools import builtins
from src.tools.types import ToolResult


# 变量说明：TERMINAL_BACKGROUND_STATUSES 表示当前流程使用的 TERMINAL_BACKGROUND_STATUSES 集合。
TERMINAL_BACKGROUND_STATUSES = frozenset({"completed", "failed", "cancelled"})
# 变量说明：MAX_BACKGROUND_TIMEOUT_SECONDS 表示当前流程使用的 MAX_BACKGROUND_TIMEOUT_SECONDS 集合。
MAX_BACKGROUND_TIMEOUT_SECONDS = 7 * 24 * 60 * 60
# 变量说明：MAX_BACKGROUND_OUTPUT_CHARS 表示当前流程使用的 MAX_BACKGROUND_OUTPUT_CHARS 集合。
MAX_BACKGROUND_OUTPUT_CHARS = 100_000
# 变量说明：MAX_BACKGROUND_OUTPUT_BYTES 表示当前流程使用的 MAX_BACKGROUND_OUTPUT_BYTES 集合。
MAX_BACKGROUND_OUTPUT_BYTES = MAX_BACKGROUND_OUTPUT_CHARS * 4
# 变量说明：logger 表示本模块日志记录器。
logger = logging.getLogger(__name__)


# 函数职责：完成 utcnow 对应的业务处理。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# 函数职责：完成 terminate_process_tree 对应的业务处理。
# 参数关系：process 表示当前流程使用的 process 集合。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def _terminate_process_tree(process: subprocess.Popen[Any]) -> None:
    if process.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(
            ["taskkill.exe", "/PID", str(process.pid), "/T", "/F"],
            capture_output=True,
            check=False,
            timeout=3,
        )
    else:
        os.killpg(os.getpgid(process.pid), signal.SIGKILL)
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=2)


# 函数职责：完成 read_log_tail 对应的业务处理。
# 参数关系：path 表示当前文件或目录路径。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def _read_log_tail(path: Path) -> str:
    if not path.exists():
        return ""
    with path.open("rb") as stream:
        # 变量说明：size 表示当前步骤使用的 size 值。
        size = stream.seek(0, os.SEEK_END)
        stream.seek(max(0, size - MAX_BACKGROUND_OUTPUT_CHARS * 4))
        return stream.read().decode("utf-8", errors="replace")[-MAX_BACKGROUND_OUTPUT_CHARS:]


# 函数职责：完成 read_log_slice 对应的业务处理。
# 参数关系：path 表示当前文件或目录路径；offset 表示当前步骤使用的 offset 值。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def _read_log_slice(path: Path, offset: int) -> tuple[str, int, bool]:
    """Read output produced after a caller-owned byte offset."""

    if not path.exists():
        return "", 0, False
    with path.open("rb") as stream:
        # 变量说明：size 表示当前步骤使用的 size 值。
        size = stream.seek(0, os.SEEK_END)
        # 变量说明：requested 表示当前步骤使用的 requested 值。
        requested = min(size, max(0, int(offset)))
        # 变量说明：truncated 表示当前步骤使用的 truncated 值。
        truncated = size - requested > MAX_BACKGROUND_OUTPUT_BYTES
        # 变量说明：start 表示当前步骤使用的 start 值。
        start = max(requested, size - MAX_BACKGROUND_OUTPUT_BYTES)
        stream.seek(start)
        # 变量说明：output 表示当前步骤使用的 output 值。
        output = stream.read().decode("utf-8", errors="replace")[-MAX_BACKGROUND_OUTPUT_CHARS:]
    return output, size, truncated


# 函数职责：完成 background_job_payload 对应的业务处理。
# 参数关系：job 表示当前步骤使用的 job 值；output_offset 表示当前步骤使用的 output_offset 值。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def background_job_payload(job: BackgroundJob, *, output_offset: int | None = None) -> dict[str, Any]:
    # 变量说明：output 表示当前步骤使用的 output 值。
    output = job.output_preview
    # 变量说明：next_output_offset 表示当前步骤使用的 next_output_offset 值。
    next_output_offset: int | None = None
    # 变量说明：output_truncated 表示当前步骤使用的 output_truncated 值。
    output_truncated = False
    if output_offset is not None and job.log_path:
        # 变量说明：output 表示当前步骤使用的 output 值；next_output_offset 表示当前步骤使用的 next_output_offset 值；output_truncated 表示当前步骤使用的 output_truncated 值。
        output, next_output_offset, output_truncated = _read_log_slice(Path(job.log_path), output_offset)
    # 变量说明：payload 表示跨层传递的数据载荷。
    payload = {
        "id": job.id,
        "session_id": job.id,
        "run_id": job.run_id,
        "status": job.status,
        "command": job.command,
        "shell": job.shell,
        "timeout_seconds": job.timeout_seconds,
        "pid": job.pid,
        "exit_code": job.exit_code,
        "log_path": job.log_path,
        "output": output,
        "error": job.error,
        "created_at": job.created_at.isoformat() if job.created_at else None,
        "started_at": job.started_at.isoformat() if job.started_at else None,
        "finished_at": job.finished_at.isoformat() if job.finished_at else None,
        "observed": job.observed_at is not None,
        "observed_by_run_id": job.observed_by_run_id,
        "waiting_run_id": job.waiting_run_id,
    }
    if next_output_offset is not None:
        # 变量说明：payload 的索引项 表示该语句创建或更新的目标数据。
        payload["next_output_offset"] = next_output_offset
        # 变量说明：payload 的索引项 表示该语句创建或更新的目标数据。
        payload["output_truncated"] = output_truncated
    return payload


# 类职责：协调 BackgroundJobManager 负责的业务流程与依赖。
class BackgroundJobManager:
    """Runs bounded host commands while durable state remains in SQLite."""

    # 函数职责：初始化实例依赖与初始状态。
    # 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
    def __init__(self) -> None:
        # 变量说明：_lock 表示当前步骤使用的 _lock 值。
        self._lock = threading.RLock()
        # 变量说明：_threads 表示当前流程使用的 _threads 集合。
        self._threads: dict[str, threading.Thread] = {}
        # 变量说明：_cancel_events 表示当前流程使用的 _cancel_events 集合。
        self._cancel_events: dict[str, threading.Event] = {}
        # 变量说明：_processes 表示当前流程使用的 _processes 集合。
        self._processes: dict[str, subprocess.Popen[Any]] = {}
        # 变量说明：_shutting_down 表示当前步骤使用的 _shutting_down 值。
        self._shutting_down = False
        # 变量说明：_terminal_listener 表示当前步骤使用的 _terminal_listener 值。
        self._terminal_listener: Callable[[str], None] | None = None

    # 函数职责：完成 set_terminal_listener 对应的业务处理。
    # 参数关系：listener 表示当前步骤使用的 listener 值。
    # 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
    def set_terminal_listener(self, listener: Callable[[str], None] | None) -> None:
        # 变量说明：_terminal_listener 表示当前步骤使用的 _terminal_listener 值。
        self._terminal_listener = listener

    # 函数职责：完成 launch 对应的业务处理。
    # 参数关系：job_id 表示job 对象的唯一标识。
    # 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
    def launch(self, job_id: str) -> bool:
        with self._lock:
            # 变量说明：current 表示当前步骤使用的 current 值。
            current = self._threads.get(job_id)
            if current is not None and current.is_alive():
                return False
            if self._shutting_down:
                return False
            # 变量说明：cancel_event 表示当前步骤使用的 cancel_event 值。
            cancel_event = threading.Event()
            # 变量说明：thread 表示当前步骤使用的 thread 值。
            thread = threading.Thread(
                target=self._run,
                args=(job_id, cancel_event),
                name=f"pgagent-background-{job_id}",
                daemon=True,
            )
            # 变量说明：映射 的索引项 表示该语句创建或更新的目标数据。
            self._threads[job_id] = thread
            # 变量说明：映射 的索引项 表示该语句创建或更新的目标数据。
            self._cancel_events[job_id] = cancel_event
            thread.start()
            return True

    # 函数职责：完成 command_parts 对应的业务处理。
    # 参数关系：job 表示当前步骤使用的 job 值。
    # 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
    def _command_parts(self, job: BackgroundJob) -> list[str]:
        if job.shell == "powershell":
            # 变量说明：executable 表示当前步骤使用的 executable 值。
            executable = shutil.which("pwsh") or shutil.which("powershell")
            if not executable:
                raise RuntimeError("PowerShell is unavailable")
            return [executable, "-NoProfile", "-NonInteractive", "-Command", job.command]
        if job.shell != "command":
            raise ValueError("shell must be command or powershell")
        return builtins.parse_command_argv(job.command)

    # 函数职责：完成 run 对应的业务处理。
    # 参数关系：job_id 表示job 对象的唯一标识；cancel_event 表示当前步骤使用的 cancel_event 值。
    # 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
    def _run(self, job_id: str, cancel_event: threading.Event) -> None:
        with database_module.SessionLocal() as db:
            job = db.get(BackgroundJob, job_id)
            run = db.get(Run, job.run_id) if job is not None and job.run_id else None
            turn = db.get(ConversationTurn, run.turn_id) if run is not None and run.turn_id else None
            context_fields = {
                "trace_id": turn.trace_id if turn is not None else (run.id if run is not None else job_id),
                "run_id": run.id if run is not None else None,
                "turn_id": turn.id if turn is not None else None,
                "background_job_id": job_id,
            }
        with bind_observability_context(**context_fields):
            self._run_bound(job_id, cancel_event)

    def _run_bound(self, job_id: str, cancel_event: threading.Event) -> None:
        # 变量说明：process 表示当前流程使用的 process 集合。
        process: subprocess.Popen[Any] | None = None
        try:
            with database_module.SessionLocal() as db:
                # 变量说明：claimed 表示当前步骤使用的 claimed 值。
                claimed = db.execute(
                    update(BackgroundJob)
                    .where(BackgroundJob.id == job_id, BackgroundJob.status == "queued")
                    .values(status="running", started_at=_utcnow(), error=None)
                )
                db.commit()
                if claimed.rowcount != 1:
                    return
                # 变量说明：job 表示当前步骤使用的 job 值。
                job = db.get(BackgroundJob, job_id)
                if job is None:
                    return
                # 变量说明：parts 表示当前流程使用的 parts 集合。
                parts = self._command_parts(job)
                # 变量说明：workspace_root 表示当前步骤使用的 workspace_root 值。
                workspace_root = Path(job.workspace_root)
                # 变量说明：log_path 表示log_path 对应的文件系统位置。
                log_path = Path(job.log_path)
                # 变量说明：timeout_seconds 表示当前流程使用的 timeout_seconds 集合。
                timeout_seconds = job.timeout_seconds

            log_path.parent.mkdir(parents=True, exist_ok=True)
            # 变量说明：popen_kwargs 表示当前流程使用的 popen_kwargs 集合。
            popen_kwargs: dict[str, Any] = {}
            if os.name == "nt":
                popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
            else:
                # 变量说明：popen_kwargs 的索引项 表示该语句创建或更新的目标数据。
                popen_kwargs["start_new_session"] = True
            with log_path.open("ab") as output:
                # 变量说明：process 表示当前流程使用的 process 集合。
                process = subprocess.Popen(
                    parts,
                    cwd=workspace_root,
                    stdin=subprocess.PIPE,
                    stdout=output,
                    stderr=subprocess.STDOUT,
                    shell=False,
                    **popen_kwargs,
                )
                with self._lock:
                    # 变量说明：映射 的索引项 表示该语句创建或更新的目标数据。
                    self._processes[job_id] = process
                with database_module.SessionLocal() as db:
                    db.execute(
                        update(BackgroundJob)
                        .where(BackgroundJob.id == job_id, BackgroundJob.status == "running")
                        .values(pid=process.pid)
                    )
                    db.commit()

                # 变量说明：deadline 表示当前步骤使用的 deadline 值。
                deadline = time.monotonic() + timeout_seconds
                while process.poll() is None and not cancel_event.wait(0.25):
                    if time.monotonic() >= deadline:
                        _terminate_process_tree(process)
                        self._finish(job_id, "failed", process.returncode, "background job timed out", log_path)
                        return
                if process.poll() is not None:
                    # 变量说明：exit_code 表示当前步骤使用的 exit_code 值。
                    exit_code = int(process.returncode or 0)
                    self._finish(
                        job_id,
                        "completed" if exit_code == 0 else "failed",
                        exit_code,
                        None if exit_code == 0 else f"command exited with code {exit_code}",
                        log_path,
                    )
                    return
                if cancel_event.is_set() and process.poll() is None:
                    _terminate_process_tree(process)
                if cancel_event.is_set():
                    with self._lock:
                        # 变量说明：shutting_down 表示当前步骤使用的 shutting_down 值。
                        shutting_down = self._shutting_down
                    self._finish(
                        job_id,
                        "failed" if shutting_down else "cancelled",
                        process.returncode,
                        (
                            "backend shutdown interrupted the background job; verify state before retrying"
                            if shutting_down else "background job cancelled"
                        ),
                        log_path,
                    )
                    return
        except Exception as exc:
            logger.exception("Background job failed")
            self._finish(job_id, "failed", process.returncode if process else None, str(exc), None)
        finally:
            if process is not None and process.stdin is not None and not process.stdin.closed:
                process.stdin.close()
            with self._lock:
                self._processes.pop(job_id, None)
                self._cancel_events.pop(job_id, None)
                self._threads.pop(job_id, None)

    # 函数职责：完成 finish 对应的业务处理。
    # 参数关系：job_id 表示job 对象的唯一标识；status 表示当前对象或运行的状态；exit_code 表示当前步骤使用的 exit_code 值；error 表示当前捕获或准备上报的错误；log_path 表示log_path 对应的文件系统位置；terminal 表示当前步骤使用的 terminal 值；expected_statuses 表示当前流程使用的 expected_statuses 集合。
    # 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
    def _finish(
        self,
        job_id: str,
        status: str,
        exit_code: int | None,
        error: str | None,
        log_path: Path | None,
        *,
        terminal: bool = True,
        expected_statuses: tuple[str, ...] = ("running",),
    ) -> bool:
        # 变量说明：preview 表示当前步骤使用的 preview 值。
        preview = _read_log_tail(log_path) if log_path is not None else ""
        # 变量说明：changed 表示当前步骤使用的 changed 值。
        changed = False
        with database_module.SessionLocal() as db:
            # 变量说明：settled 表示当前步骤使用的 settled 值。
            settled = db.execute(
                update(BackgroundJob)
                .where(BackgroundJob.id == job_id, BackgroundJob.status.in_(expected_statuses))
                .values(
                    status=status,
                    exit_code=exit_code,
                    output_preview=preview,
                    error=error,
                    pid=None,
                    finished_at=_utcnow() if terminal else None,
                )
            )
            # 变量说明：changed 表示当前步骤使用的 changed 值。
            changed = settled.rowcount == 1
            if changed and terminal:
                # 变量说明：job 表示当前步骤使用的 job 值。
                job = db.get(BackgroundJob, job_id)
                # 变量说明：target_run_id 表示target_run 对象的唯一标识。
                target_run_id = (job.waiting_run_id or job.run_id) if job is not None else None
                # 变量说明：owner_run 表示当前步骤使用的 owner_run 值。
                owner_run = db.get(Run, target_run_id) if target_run_id else None
                if job is not None:
                    if job.plan_step_id:
                        # 变量说明：step 表示当前步骤使用的 step 值。
                        step = db.get(PlanStep, job.plan_step_id)
                        if step is not None:
                            # 变量说明：status 表示当前对象或运行的状态。
                            step.status = "completed" if status == "completed" else "failed"
                            # 变量说明：result 表示本步骤产生的结果。
                            step.result = preview
                            # 变量说明：error 表示当前捕获或准备上报的错误。
                            step.error = error
                            # 变量说明：completed_at 表示completed_at 对应的时间信息。
                            step.completed_at = _utcnow()
                            # 变量说明：remaining_work 表示当前步骤使用的 remaining_work 值。
                            step.remaining_work = [] if status == "completed" else list(
                                step.remaining_work or [step.title]
                            )
                            if status == "completed":
                                # 变量说明：completed_work 表示当前步骤使用的 completed_work 值。
                                step.completed_work = list(step.completed_work or [step.title])
                            refresh_task_state(db, step.task_id)
                    db.add(CollaborationEvent(
                        task_id=owner_run.task_id if owner_run is not None else None,
                        plan_step_id=job.plan_step_id,
                        run_id=target_run_id,
                        source_kind="background_job",
                        source_id=job.id,
                        event_type=f"background_job_{status}",
                        payload={
                            "job_id": job.id,
                            "status": status,
                            "exit_code": exit_code,
                            "output": preview,
                            "error": error,
                        },
                    ))
            db.commit()
        if changed and terminal and self._terminal_listener is not None:
            try:
                self._terminal_listener(job_id)
            except Exception:
                logger.exception("Background terminal listener failed for %s", job_id)
        return changed

    # 函数职责：完成 cancel 对应的业务处理。
    # 参数关系：job_id 表示job 对象的唯一标识。
    # 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
    def cancel(self, job_id: str) -> bool:
        with self._lock:
            # 变量说明：event 表示当前运行事件。
            event = self._cancel_events.get(job_id)
            if event is not None:
                event.set()
                return True
        with database_module.SessionLocal() as db:
            # 变量说明：status 表示当前对象或运行的状态。
            status = db.scalar(select(BackgroundJob.status).where(BackgroundJob.id == job_id))
        if status != "queued":
            return False
        return self._finish(
            job_id,
            "cancelled",
            None,
            "background job cancelled",
            None,
            expected_statuses=("queued",),
        )

    # 函数职责：完成 send_input 对应的业务处理。
    # 参数关系：job_id 表示job 对象的唯一标识；input_text 表示input 的文本表示；close 表示当前步骤使用的 close 值。
    # 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
    def send_input(self, job_id: str, input_text: str, *, close: bool = False) -> str | None:
        with self._lock:
            # 变量说明：process 表示当前流程使用的 process 集合。
            process = self._processes.get(job_id)
        if process is None or process.poll() is not None or process.stdin is None:
            return "background job is not accepting input"
        try:
            process.stdin.write(str(input_text).encode("utf-8"))
            process.stdin.flush()
            if close:
                process.stdin.close()
        except (BrokenPipeError, OSError, ValueError):
            return "background job stdin is closed"
        return None

    # 函数职责：完成 cancel_for_runs 对应的业务处理。
    # 参数关系：run_ids 表示run 对象标识集合。
    # 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
    def cancel_for_runs(self, run_ids: list[str]) -> list[str]:
        """Cancel active jobs owned or awaited by the stopped runs."""

        # 变量说明：normalized 表示当前步骤使用的 normalized 值。
        normalized = list(dict.fromkeys(str(run_id) for run_id in run_ids if str(run_id)))
        if not normalized:
            return []
        with database_module.SessionLocal() as db:
            # 变量说明：job_ids 表示job 对象标识集合。
            job_ids = [str(job_id) for job_id in db.scalars(
                select(BackgroundJob.id).where(
                    BackgroundJob.status.in_({"queued", "running"}),
                    (BackgroundJob.run_id.in_(normalized))
                    | (BackgroundJob.waiting_run_id.in_(normalized)),
                )
            )]
        return [job_id for job_id in job_ids if self.cancel(job_id)]

    # 函数职责：完成 recover 对应的业务处理。
    # 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
    def recover(self) -> list[str]:
        """Relaunch safely paused jobs and settle unverifiable crash remnants."""

        with self._lock:
            # 变量说明：_shutting_down 表示当前步骤使用的 _shutting_down 值。
            self._shutting_down = False
        with database_module.SessionLocal() as db:
            # 变量说明：stale_jobs 表示当前流程使用的 stale_jobs 集合。
            stale_jobs = list(
                db.execute(
                    select(BackgroundJob.id, BackgroundJob.log_path).where(
                        BackgroundJob.status == "running"
                    )
                )
            )
            # 变量说明：queued 表示当前步骤使用的 queued 值。
            queued = [str(item) for item in db.scalars(
                select(BackgroundJob.id).where(BackgroundJob.status == "queued")
            )]
            db.commit()
        for job_id, log_path in stale_jobs:
            self._finish(
                str(job_id),
                "failed",
                None,
                "backend restarted while the background job was running",
                Path(log_path) if log_path else None,
            )
        for job_id in queued:
            self.launch(job_id)
        return queued

    # 函数职责：完成 shutdown 对应的业务处理。
    # 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
    def shutdown(self) -> None:
        with self._lock:
            # 变量说明：_shutting_down 表示当前步骤使用的 _shutting_down 值。
            self._shutting_down = True
            # 变量说明：events 表示运行事件集合。
            events = list(self._cancel_events.values())
            # 变量说明：threads 表示当前流程使用的 threads 集合。
            threads = list(self._threads.values())
        for event in events:
            event.set()
        for thread in threads:
            thread.join(timeout=15)


# 变量说明：background_job_manager 表示当前步骤使用的 background_job_manager 值。
background_job_manager = BackgroundJobManager()


# 类职责：封装 BackgroundJobToolStore 的持久化访问。
class BackgroundJobToolStore:
    """Run-scoped provider-tool facade for durable background jobs."""

    # 函数职责：初始化实例依赖与初始状态。
    # 参数关系：run_id 表示当前运行标识；session_id 表示所属会话标识；workspace_id 表示工作区标识；workspace_root 表示当前步骤使用的 workspace_root 值；include_session_jobs 表示当前流程使用的 include_session_jobs 集合。
    # 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
    def __init__(
        self,
        *,
        run_id: str,
        session_id: str | None,
        workspace_id: str | None,
        workspace_root: str,
        include_session_jobs: bool = False,
    ) -> None:
        # 变量说明：run_id 表示当前运行标识。
        self.run_id = run_id
        # 变量说明：session_id 表示所属会话标识。
        self.session_id = session_id
        # 变量说明：workspace_id 表示工作区标识。
        self.workspace_id = workspace_id
        # 变量说明：workspace_root 表示当前步骤使用的 workspace_root 值。
        self.workspace_root = str(Path(workspace_root).resolve())
        # 变量说明：include_session_jobs 表示当前流程使用的 include_session_jobs 集合。
        self.include_session_jobs = include_session_jobs
        # 变量说明：_delivered_terminal_ids 表示_delivered_terminal 对象标识集合。
        self._delivered_terminal_ids: set[str] = set()

    # 函数职责：完成 ownership_clause 对应的业务处理。
    # 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
    def _ownership_clause(self):
        if self.include_session_jobs and self.session_id is not None:
            return BackgroundJob.session_id == self.session_id
        return BackgroundJob.run_id == self.run_id

    # 函数职责：完成 start 对应的业务处理。
    # 参数关系：command 表示当前步骤使用的 command 值；timeout 表示当前步骤使用的 timeout 值；shell 表示当前步骤使用的 shell 值；plan_step_id 表示plan_step 对象的唯一标识；cwd 表示当前步骤使用的 cwd 值。
    # 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
    def start(
        self,
        *,
        command: str | list[str],
        timeout: int = 3600,
        shell: str = "command",
        plan_step_id: str = "",
        cwd: str = ".",
    ) -> ToolResult:
        # 变量说明：normalized_shell 表示当前步骤使用的 normalized_shell 值。
        normalized_shell = str(shell or "command").strip().lower()
        if normalized_shell == "powershell":
            if not isinstance(command, str):
                return ToolResult("background_run", False, "PowerShell command must be a string", error_code="invalid_arguments")
            # 变量说明：normalized_command 表示当前步骤使用的 normalized_command 值。
            normalized_command = command.strip()
        else:
            try:
                # 变量说明：command_parts 表示当前流程使用的 command_parts 集合。
                command_parts = builtins.parse_command_argv(command)
            except ValueError as exc:
                return ToolResult("background_run", False, str(exc), error_code="invalid_arguments")
            # 变量说明：normalized_command 表示当前步骤使用的 normalized_command 值。
            normalized_command = (
                subprocess.list2cmdline(command_parts)
                if os.name == "nt"
                else shlex.join(command_parts)
            )
        if not normalized_command:
            return ToolResult("background_run", False, "command is required", error_code="invalid_arguments")
        if normalized_shell not in {"command", "powershell"}:
            return ToolResult("background_run", False, "shell must be command or powershell", error_code="invalid_arguments")
        # 变量说明：workspace_root 表示当前步骤使用的 workspace_root 值。
        workspace_root = Path(self.workspace_root)
        # 变量说明：working_directory 表示当前步骤使用的 working_directory 值。
        working_directory = (workspace_root / str(cwd or ".")).resolve()
        try:
            working_directory.relative_to(workspace_root)
        except ValueError:
            return ToolResult("background_run", False, "cwd escapes workspace", error_code="path_outside_workspace")
        if not working_directory.is_dir():
            return ToolResult("background_run", False, "cwd is not a directory", error_code="not_directory")
        # 变量说明：timeout_seconds 表示当前流程使用的 timeout_seconds 集合。
        timeout_seconds = min(MAX_BACKGROUND_TIMEOUT_SECONDS, max(1, int(timeout)))
        with database_module.SessionLocal() as db:
            if db.get_bind().dialect.name == "sqlite":
                db.execute(text("BEGIN IMMEDIATE"))
            # 变量说明：linked_step 表示当前步骤使用的 linked_step 值。
            linked_step = None
            # 变量说明：requested_step_id 表示requested_step 对象的唯一标识。
            requested_step_id = str(plan_step_id or "").strip()
            if requested_step_id:
                # 变量说明：owner_run 表示当前步骤使用的 owner_run 值。
                owner_run = db.get(Run, self.run_id)
                if owner_run is None or not owner_run.task_id:
                    db.rollback()
                    return ToolResult(
                        "background_run", False, "run has no durable task graph", error_code="task_not_found"
                    )
                # 变量说明：linked_step 表示当前步骤使用的 linked_step 值。
                linked_step = db.get(PlanStep, requested_step_id)
                if linked_step is None or linked_step.task_id != owner_run.task_id:
                    # 变量说明：linked_step 表示当前步骤使用的 linked_step 值。
                    linked_step = db.scalar(select(PlanStep).where(
                        PlanStep.task_id == owner_run.task_id,
                        PlanStep.external_id == requested_step_id,
                    ))
                # 变量说明：ready_ids 表示ready 对象标识集合。
                ready_ids = {step.id for step in ready_steps(db, owner_run.task_id)}
                if linked_step is None or linked_step.id not in ready_ids:
                    db.rollback()
                    return ToolResult(
                        "background_run",
                        False,
                        "background plan step is not ready or is already claimed",
                        error_code="task_blocked",
                    )
                # 变量说明：status 表示当前对象或运行的状态。
                linked_step.status = "in_progress"
                # 变量说明：executor_kind 表示当前步骤使用的 executor_kind 值。
                linked_step.executor_kind = "background"
                # 变量说明：assigned_run_id 表示assigned_run 对象的唯一标识。
                linked_step.assigned_run_id = self.run_id
                # 变量说明：started_at 表示started_at 对应的时间信息。
                linked_step.started_at = linked_step.started_at or _utcnow()
                # 变量说明：attempt 表示当前步骤使用的 attempt 值。
                linked_step.attempt = int(linked_step.attempt or 0) + 1
                # 变量说明：error 表示当前捕获或准备上报的错误。
                linked_step.error = None
            # 变量说明：job 表示当前步骤使用的 job 值。
            job = BackgroundJob(
                run_id=self.run_id,
                session_id=self.session_id,
                workspace_id=self.workspace_id,
                workspace_root=str(working_directory),
                command=normalized_command,
                shell=normalized_shell,
                status="queued",
                timeout_seconds=timeout_seconds,
                plan_step_id=linked_step.id if linked_step is not None else None,
            )
            db.add(job)
            db.flush()
            # 变量说明：log_path 表示log_path 对应的文件系统位置。
            job.log_path = str((settings.data_dir / "background-jobs" / f"{job.id}.log").resolve())
            db.commit()
            db.refresh(job)
            # 变量说明：payload 表示跨层传递的数据载荷。
            payload = background_job_payload(job)
        background_job_manager.launch(job.id)
        return ToolResult(
            "background_run",
            True,
            json.dumps(payload, ensure_ascii=False),
            changed=True,
            metadata={
                "background_job_id": job.id,
                "session_id": job.id,
                "background_job_active": True,
                "plan_step_id": job.plan_step_id or "",
            },
        )

    # 函数职责：完成 check 对应的业务处理。
    # 参数关系：task_id 表示任务标识；wait 表示当前步骤使用的 wait 值；wait_timeout 表示当前步骤使用的 wait_timeout 值；output_offset 表示当前步骤使用的 output_offset 值；_cancel_event 表示当前步骤使用的 _cancel_event 值。
    # 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
    def check(
        self,
        *,
        task_id: str | None = None,
        wait: bool = False,
        wait_timeout: float = MAX_BACKGROUND_TIMEOUT_SECONDS,
        output_offset: int = 0,
        _cancel_event: threading.Event | None = None,
    ) -> ToolResult:
        # 变量说明：wait_started_at 表示wait_started_at 对应的时间信息。
        wait_started_at = time.monotonic()
        # 变量说明：deadline 表示当前步骤使用的 deadline 值。
        deadline = time.monotonic() + min(MAX_BACKGROUND_TIMEOUT_SECONDS, max(0.0, float(wait_timeout)))
        while True:
            with database_module.SessionLocal() as db:
                # 变量说明：ownership 表示当前步骤使用的 ownership 值。
                ownership = self._ownership_clause()
                if task_id and self.session_id is not None:
                    # A recovery Run may explicitly adopt a Job created by an
                    # earlier Run in the same conversation.
                    # 变量说明：ownership 表示当前步骤使用的 ownership 值。
                    ownership = BackgroundJob.session_id == self.session_id
                # 变量说明：query 表示当前步骤使用的 query 值。
                query = select(BackgroundJob).where(ownership)
                if task_id:
                    # 变量说明：query 表示当前步骤使用的 query 值。
                    query = query.where(BackgroundJob.id == str(task_id))
                    # 变量说明：job 表示当前步骤使用的 job 值。
                    job = db.scalar(query)
                    if job is None:
                        return ToolResult("check_background", False, "background job not found", error_code="task_not_found")
                    # 变量说明：terminal 表示当前步骤使用的 terminal 值。
                    terminal = job.status in TERMINAL_BACKGROUND_STATUSES
                    if terminal and job.observed_at is None:
                        # 变量说明：observed_at 表示observed_at 对应的时间信息。
                        job.observed_at = _utcnow()
                        # 变量说明：observed_by_run_id 表示observed_by_run 对象的唯一标识。
                        job.observed_by_run_id = self.run_id
                        db.commit()
                    # 变量说明：payload 表示跨层传递的数据载荷。
                    payload: Any = background_job_payload(job, output_offset=output_offset)
                    # 变量说明：statuses 表示当前流程使用的 statuses 集合。
                    statuses = {job.status}
                else:
                    # 变量说明：jobs 表示当前流程使用的 jobs 集合。
                    jobs = list(db.scalars(query.order_by(BackgroundJob.created_at.asc())))
                    for job in jobs:
                        if job.status in TERMINAL_BACKGROUND_STATUSES and job.observed_at is None:
                            # 变量说明：observed_at 表示observed_at 对应的时间信息。
                            job.observed_at = _utcnow()
                            # 变量说明：observed_by_run_id 表示observed_by_run 对象的唯一标识。
                            job.observed_by_run_id = self.run_id
                    db.commit()
                    # 变量说明：payload 表示跨层传递的数据载荷。
                    payload = [background_job_payload(job) for job in jobs]
                    # 变量说明：statuses 表示当前流程使用的 statuses 集合。
                    statuses = {job.status for job in jobs}
                    # 变量说明：terminal 表示当前步骤使用的 terminal 值。
                    terminal = not statuses.intersection({"queued", "running"})
            if not wait or terminal:
                # 变量说明：ok 表示当前步骤使用的 ok 值。
                ok = not statuses.intersection({"failed", "cancelled"})
                return ToolResult(
                    "check_background",
                    ok,
                    json.dumps(payload, ensure_ascii=False),
                    error_code=None if ok else "background_job_failed",
                    metadata={
                        "background_job_active": not terminal,
                        "session_id": str(task_id or ""),
                        "next_output_offset": payload.get("next_output_offset", 0) if isinstance(payload, dict) else 0,
                        "background_wait_seconds": max(0.0, time.monotonic() - wait_started_at) if wait else 0.0,
                    },
                )
            if _cancel_event is not None and _cancel_event.wait(0.25):
                return ToolResult(
                    "check_background",
                    False,
                    "background wait cancelled",
                    error_code="cancelled",
                    metadata={"background_wait_seconds": max(0.0, time.monotonic() - wait_started_at)},
                )
            if time.monotonic() >= deadline:
                return ToolResult(
                    "check_background",
                    True,
                    json.dumps(payload, ensure_ascii=False),
                    metadata={
                        "background_job_active": True,
                        "background_wait_timed_out": True,
                        "session_id": str(task_id or ""),
                        "next_output_offset": payload.get("next_output_offset", 0) if isinstance(payload, dict) else 0,
                        "background_wait_seconds": max(0.0, time.monotonic() - wait_started_at),
                    },
                )
            time.sleep(0.25)

    # 函数职责：完成 write_stdin 对应的业务处理。
    # 参数关系：task_id 表示任务标识；input 表示当前步骤使用的 input 值；close 表示当前步骤使用的 close 值；wait_ms 表示当前流程使用的 wait_ms 集合；output_offset 表示当前步骤使用的 output_offset 值。
    # 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
    def write_stdin(
        self,
        *,
        task_id: str,
        input: str,
        close: bool = False,
        wait_ms: int = 250,
        output_offset: int = 0,
    ) -> ToolResult:
        # 变量说明：normalized_id 表示normalized 对象的唯一标识。
        normalized_id = str(task_id or "").strip()
        if not normalized_id:
            return ToolResult("write_stdin", False, "task_id is required", error_code="invalid_arguments")
        with database_module.SessionLocal() as db:
            # 变量说明：ownership 表示当前步骤使用的 ownership 值。
            ownership = self._ownership_clause()
            if self.session_id is not None:
                # 变量说明：ownership 表示当前步骤使用的 ownership 值。
                ownership = BackgroundJob.session_id == self.session_id
            # 变量说明：job 表示当前步骤使用的 job 值。
            job = db.scalar(select(BackgroundJob).where(
                ownership,
                BackgroundJob.id == normalized_id,
            ))
            if job is None:
                return ToolResult("write_stdin", False, "background job not found", error_code="task_not_found")
            if job.status != "running":
                return ToolResult(
                    "write_stdin",
                    False,
                    f"background job is {job.status}",
                    error_code="task_not_running",
                )
        # 变量说明：error 表示当前捕获或准备上报的错误。
        error = background_job_manager.send_input(normalized_id, input, close=bool(close))
        if error:
            return ToolResult("write_stdin", False, error, error_code="stdin_unavailable")
        # 变量说明：result 表示本步骤产生的结果。
        result = self.check(
            task_id=normalized_id,
            wait=True,
            wait_timeout=min(MAX_BACKGROUND_TIMEOUT_SECONDS, max(0, int(wait_ms))) / 1000,
            output_offset=output_offset,
        )
        # 变量说明：payload 表示跨层传递的数据载荷。
        payload = json.loads(result.content)
        payload["chars_sent"] = len(str(input))
        payload["stdin_closed"] = bool(close)
        # 变量说明：tool_name 表示当前步骤使用的 tool_name 值。
        result.tool_name = "write_stdin"
        # 变量说明：content 表示待处理或返回的正文内容。
        result.content = json.dumps(payload, ensure_ascii=False)
        # 变量说明：changed 表示当前步骤使用的 changed 值。
        result.changed = True
        return result

    # 函数职责：完成 unresolved_jobs 对应的业务处理。
    # 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
    def unresolved_jobs(self) -> list[dict[str, Any]]:
        with database_module.SessionLocal() as db:
            # 变量说明：ownership 表示当前步骤使用的 ownership 值。
            ownership = self._ownership_clause()
            # 变量说明：jobs 表示当前流程使用的 jobs 集合。
            jobs = list(db.scalars(
                select(BackgroundJob).where(
                    ownership,
                    (BackgroundJob.status.in_({"queued", "running"})) | (BackgroundJob.observed_at.is_(None)),
                )
            ))
            return [background_job_payload(job) for job in jobs]

    # 函数职责：完成 active_jobs 对应的业务处理。
    # 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
    def active_jobs(self) -> list[dict[str, Any]]:
        with database_module.SessionLocal() as db:
            # 变量说明：jobs 表示当前流程使用的 jobs 集合。
            jobs = list(db.scalars(select(BackgroundJob).where(
                self._ownership_clause(),
                BackgroundJob.status.in_({"queued", "running"}),
            )))
            return [background_job_payload(job) for job in jobs]

    # 函数职责：完成 register_waiter 对应的业务处理。
    # 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
    def register_waiter(self) -> list[str]:
        with database_module.SessionLocal() as db:
            if db.get_bind().dialect.name == "sqlite":
                db.execute(text("BEGIN IMMEDIATE"))
            # 变量说明：jobs 表示当前流程使用的 jobs 集合。
            jobs = list(db.scalars(select(BackgroundJob).where(
                self._ownership_clause(),
                BackgroundJob.status.in_({"queued", "running"}),
            )))
            for job in jobs:
                # 变量说明：waiting_run_id 表示waiting_run 对象的唯一标识。
                job.waiting_run_id = self.run_id
            db.commit()
            return [job.id for job in jobs]

    def completion_boundary(self) -> dict[str, Any]:
        """Atomically choose waiting, terminal observation, or clear completion.

        The write transaction closes the race where a job could finish after
        an active read but before waiter registration, leaving the Run asleep
        without a terminal notification.
        """

        with database_module.SessionLocal() as db:
            if db.get_bind().dialect.name == "sqlite":
                db.execute(text("BEGIN IMMEDIATE"))
            active = list(db.scalars(
                select(BackgroundJob)
                .where(
                    self._ownership_clause(),
                    BackgroundJob.status.in_({"queued", "running"}),
                )
                .order_by(BackgroundJob.created_at.asc(), BackgroundJob.id.asc())
                .with_for_update()
            ))
            if active:
                for job in active:
                    job.waiting_run_id = self.run_id
                payloads = [background_job_payload(job) for job in active]
                db.commit()
                return {"status": "waiting", "jobs": payloads}

            terminal_query = (
                select(BackgroundJob)
                .where(
                    self._ownership_clause(),
                    BackgroundJob.status.in_(TERMINAL_BACKGROUND_STATUSES),
                    BackgroundJob.observed_at.is_(None),
                )
                .order_by(BackgroundJob.created_at.asc(), BackgroundJob.id.asc())
                .with_for_update()
            )
            if self._delivered_terminal_ids:
                terminal_query = terminal_query.where(
                    BackgroundJob.id.not_in(self._delivered_terminal_ids)
                )
            terminal = list(db.scalars(terminal_query))
            payloads = [background_job_payload(job) for job in terminal]
            db.commit()
        if terminal:
            self.track_terminal_deliveries([job.id for job in terminal])
            return {"status": "observe", "results": payloads}
        return {"status": "clear"}

    # 函数职责：完成 track_terminal_deliveries 对应的业务处理。
    # 参数关系：job_ids 表示job 对象标识集合。
    # 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
    def track_terminal_deliveries(self, job_ids: list[str]) -> None:
        """Stage terminal events until the owning model outcome is committed."""

        self._delivered_terminal_ids.update(str(job_id) for job_id in job_ids if str(job_id))

    # 函数职责：完成 delivered_terminal_ids 对应的业务处理。
    # 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
    def delivered_terminal_ids(self) -> list[str]:
        return sorted(self._delivered_terminal_ids)

    # 函数职责：完成 observe_terminal_results 对应的业务处理。
    # 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
    def observe_terminal_results(self) -> list[dict[str, Any]]:
        with database_module.SessionLocal() as db:
            # 变量说明：query 表示当前步骤使用的 query 值。
            query = select(BackgroundJob).where(
                self._ownership_clause(),
                BackgroundJob.status.in_(TERMINAL_BACKGROUND_STATUSES),
                BackgroundJob.observed_at.is_(None),
            )
            if self._delivered_terminal_ids:
                # 变量说明：query 表示当前步骤使用的 query 值。
                query = query.where(BackgroundJob.id.not_in(self._delivered_terminal_ids))
            # 变量说明：jobs 表示当前流程使用的 jobs 集合。
            jobs = list(db.scalars(
                query.order_by(BackgroundJob.created_at.asc(), BackgroundJob.id.asc())
            ))
            self.track_terminal_deliveries([job.id for job in jobs])
            return [background_job_payload(job) for job in jobs]
