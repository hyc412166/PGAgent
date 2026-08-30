"""Durable long-running command jobs owned by Agent runs."""

from __future__ import annotations

import json
import logging
import os
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
from src.persistence.database import BackgroundJob, CollaborationEvent, PlanStep, Run
from src.tasks.graph import ready_steps, refresh_task_state
from src.tools import builtins
from src.tools.types import ToolResult


TERMINAL_BACKGROUND_STATUSES = frozenset({"completed", "failed", "cancelled"})
MAX_BACKGROUND_TIMEOUT_SECONDS = 7 * 24 * 60 * 60
MAX_BACKGROUND_OUTPUT_CHARS = 100_000
logger = logging.getLogger(__name__)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _terminate_process_tree(process: subprocess.Popen[Any]) -> None:
    if process.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(
            ["taskkill.exe", "/PID", str(process.pid), "/T", "/F"],
            capture_output=True,
            check=False,
            timeout=10,
        )
    else:
        os.killpg(os.getpgid(process.pid), signal.SIGKILL)
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def _read_log_tail(path: Path) -> str:
    if not path.exists():
        return ""
    with path.open("rb") as stream:
        size = stream.seek(0, os.SEEK_END)
        stream.seek(max(0, size - MAX_BACKGROUND_OUTPUT_CHARS * 4))
        return stream.read().decode("utf-8", errors="replace")[-MAX_BACKGROUND_OUTPUT_CHARS:]


def background_job_payload(job: BackgroundJob) -> dict[str, Any]:
    return {
        "id": job.id,
        "run_id": job.run_id,
        "status": job.status,
        "command": job.command,
        "shell": job.shell,
        "timeout_seconds": job.timeout_seconds,
        "pid": job.pid,
        "exit_code": job.exit_code,
        "log_path": job.log_path,
        "output": job.output_preview,
        "error": job.error,
        "created_at": job.created_at.isoformat() if job.created_at else None,
        "started_at": job.started_at.isoformat() if job.started_at else None,
        "finished_at": job.finished_at.isoformat() if job.finished_at else None,
        "observed": job.observed_at is not None,
        "observed_by_run_id": job.observed_by_run_id,
        "waiting_run_id": job.waiting_run_id,
    }


class BackgroundJobManager:
    """Runs bounded host commands while durable state remains in SQLite."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._threads: dict[str, threading.Thread] = {}
        self._cancel_events: dict[str, threading.Event] = {}
        self._processes: dict[str, subprocess.Popen[Any]] = {}
        self._shutting_down = False
        self._terminal_listener: Callable[[str], None] | None = None

    def set_terminal_listener(self, listener: Callable[[str], None] | None) -> None:
        self._terminal_listener = listener

    def launch(self, job_id: str) -> bool:
        with self._lock:
            current = self._threads.get(job_id)
            if current is not None and current.is_alive():
                return False
            if self._shutting_down:
                return False
            cancel_event = threading.Event()
            thread = threading.Thread(
                target=self._run,
                args=(job_id, cancel_event),
                name=f"pgagent-background-{job_id}",
                daemon=True,
            )
            self._threads[job_id] = thread
            self._cancel_events[job_id] = cancel_event
            thread.start()
            return True

    def _command_parts(self, job: BackgroundJob) -> list[str]:
        if job.shell == "powershell":
            executable = shutil.which("pwsh") or shutil.which("powershell")
            if not executable:
                raise RuntimeError("PowerShell is unavailable")
            return [executable, "-NoProfile", "-NonInteractive", "-Command", job.command]
        if job.shell != "command":
            raise ValueError("shell must be command or powershell")
        parts = builtins._split_command(job.command)
        if any(marker in parts[0] for marker in ("/", "\\", ":")):
            raise ValueError("command must use a bare allowlisted executable name")
        if parts[0].lower() not in {item.lower() for item in builtins.DEFAULT_COMMAND_ALLOWLIST}:
            raise ValueError(f"command is not allowlisted: {parts[0].lower()}")
        return parts

    def _run(self, job_id: str, cancel_event: threading.Event) -> None:
        process: subprocess.Popen[Any] | None = None
        try:
            with database_module.SessionLocal() as db:
                claimed = db.execute(
                    update(BackgroundJob)
                    .where(BackgroundJob.id == job_id, BackgroundJob.status == "queued")
                    .values(status="running", started_at=_utcnow(), error=None)
                )
                db.commit()
                if claimed.rowcount != 1:
                    return
                job = db.get(BackgroundJob, job_id)
                if job is None:
                    return
                parts = self._command_parts(job)
                workspace_root = Path(job.workspace_root)
                log_path = Path(job.log_path)
                timeout_seconds = job.timeout_seconds

            log_path.parent.mkdir(parents=True, exist_ok=True)
            popen_kwargs: dict[str, Any] = {}
            if os.name == "nt":
                popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
            else:
                popen_kwargs["start_new_session"] = True
            with log_path.open("ab") as output:
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
                    self._processes[job_id] = process
                with database_module.SessionLocal() as db:
                    db.execute(
                        update(BackgroundJob)
                        .where(BackgroundJob.id == job_id, BackgroundJob.status == "running")
                        .values(pid=process.pid)
                    )
                    db.commit()

                deadline = time.monotonic() + timeout_seconds
                while process.poll() is None and not cancel_event.wait(0.25):
                    if time.monotonic() >= deadline:
                        _terminate_process_tree(process)
                        self._finish(job_id, "failed", process.returncode, "background job timed out", log_path)
                        return
                if process.poll() is not None:
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
            self._finish(job_id, "failed", process.returncode if process else None, str(exc), None)
        finally:
            if process is not None and process.stdin is not None and not process.stdin.closed:
                process.stdin.close()
            with self._lock:
                self._processes.pop(job_id, None)
                self._cancel_events.pop(job_id, None)
                self._threads.pop(job_id, None)

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
        preview = _read_log_tail(log_path) if log_path is not None else ""
        changed = False
        with database_module.SessionLocal() as db:
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
            changed = settled.rowcount == 1
            if changed and terminal:
                job = db.get(BackgroundJob, job_id)
                target_run_id = (job.waiting_run_id or job.run_id) if job is not None else None
                owner_run = db.get(Run, target_run_id) if target_run_id else None
                if job is not None:
                    if job.plan_step_id:
                        step = db.get(PlanStep, job.plan_step_id)
                        if step is not None:
                            step.status = "completed" if status == "completed" else "failed"
                            step.result = preview
                            step.error = error
                            step.completed_at = _utcnow()
                            step.remaining_work = [] if status == "completed" else list(
                                step.remaining_work or [step.title]
                            )
                            if status == "completed":
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

    def cancel(self, job_id: str) -> bool:
        with self._lock:
            event = self._cancel_events.get(job_id)
            if event is not None:
                event.set()
                return True
        with database_module.SessionLocal() as db:
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

    def send_input(self, job_id: str, input_text: str, *, close: bool = False) -> str | None:
        with self._lock:
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

    def cancel_for_runs(self, run_ids: list[str]) -> list[str]:
        """Cancel active jobs owned or awaited by the stopped runs."""

        normalized = list(dict.fromkeys(str(run_id) for run_id in run_ids if str(run_id)))
        if not normalized:
            return []
        with database_module.SessionLocal() as db:
            job_ids = [str(job_id) for job_id in db.scalars(
                select(BackgroundJob.id).where(
                    BackgroundJob.status.in_({"queued", "running"}),
                    (BackgroundJob.run_id.in_(normalized))
                    | (BackgroundJob.waiting_run_id.in_(normalized)),
                )
            )]
        return [job_id for job_id in job_ids if self.cancel(job_id)]

    def recover(self) -> list[str]:
        """Relaunch safely paused jobs and settle unverifiable crash remnants."""

        with self._lock:
            self._shutting_down = False
        with database_module.SessionLocal() as db:
            stale_ids = list(db.scalars(select(BackgroundJob.id).where(BackgroundJob.status == "running")))
            queued = [str(item) for item in db.scalars(
                select(BackgroundJob.id).where(BackgroundJob.status == "queued")
            )]
            db.commit()
        for job_id in stale_ids:
            self._finish(
                str(job_id),
                "failed",
                None,
                "backend restarted while the background job was running",
                None,
            )
        for job_id in queued:
            self.launch(job_id)
        return queued

    def shutdown(self) -> None:
        with self._lock:
            self._shutting_down = True
            events = list(self._cancel_events.values())
            threads = list(self._threads.values())
        for event in events:
            event.set()
        for thread in threads:
            thread.join(timeout=15)


background_job_manager = BackgroundJobManager()


class BackgroundJobToolStore:
    """Run-scoped provider-tool facade for durable background jobs."""

    def __init__(
        self,
        *,
        run_id: str,
        session_id: str | None,
        workspace_id: str | None,
        workspace_root: str,
        include_session_jobs: bool = False,
    ) -> None:
        self.run_id = run_id
        self.session_id = session_id
        self.workspace_id = workspace_id
        self.workspace_root = str(Path(workspace_root).resolve())
        self.include_session_jobs = include_session_jobs
        self._delivered_terminal_ids: set[str] = set()

    def _ownership_clause(self):
        if self.include_session_jobs and self.session_id is not None:
            return BackgroundJob.session_id == self.session_id
        return BackgroundJob.run_id == self.run_id

    def start(
        self,
        *,
        command: str,
        timeout: int = 3600,
        shell: str = "command",
        plan_step_id: str = "",
    ) -> ToolResult:
        normalized_command = str(command or "").strip()
        normalized_shell = str(shell or "command").strip().lower()
        if not normalized_command:
            return ToolResult("background_run", False, "command is required", error_code="invalid_arguments")
        if normalized_shell not in {"command", "powershell"}:
            return ToolResult("background_run", False, "shell must be command or powershell", error_code="invalid_arguments")
        timeout_seconds = min(MAX_BACKGROUND_TIMEOUT_SECONDS, max(1, int(timeout)))
        with database_module.SessionLocal() as db:
            if db.get_bind().dialect.name == "sqlite":
                db.execute(text("BEGIN IMMEDIATE"))
            linked_step = None
            requested_step_id = str(plan_step_id or "").strip()
            if requested_step_id:
                owner_run = db.get(Run, self.run_id)
                if owner_run is None or not owner_run.task_id:
                    db.rollback()
                    return ToolResult(
                        "background_run", False, "run has no durable task graph", error_code="task_not_found"
                    )
                linked_step = db.get(PlanStep, requested_step_id)
                if linked_step is None or linked_step.task_id != owner_run.task_id:
                    linked_step = db.scalar(select(PlanStep).where(
                        PlanStep.task_id == owner_run.task_id,
                        PlanStep.external_id == requested_step_id,
                    ))
                ready_ids = {step.id for step in ready_steps(db, owner_run.task_id)}
                if linked_step is None or linked_step.id not in ready_ids:
                    db.rollback()
                    return ToolResult(
                        "background_run",
                        False,
                        "background plan step is not ready or is already claimed",
                        error_code="task_blocked",
                    )
                linked_step.status = "in_progress"
                linked_step.executor_kind = "background"
                linked_step.assigned_run_id = self.run_id
                linked_step.started_at = linked_step.started_at or _utcnow()
                linked_step.attempt = int(linked_step.attempt or 0) + 1
                linked_step.error = None
            job = BackgroundJob(
                run_id=self.run_id,
                session_id=self.session_id,
                workspace_id=self.workspace_id,
                workspace_root=self.workspace_root,
                command=normalized_command,
                shell=normalized_shell,
                status="queued",
                timeout_seconds=timeout_seconds,
                plan_step_id=linked_step.id if linked_step is not None else None,
            )
            db.add(job)
            db.flush()
            job.log_path = str((settings.data_dir / "background-jobs" / f"{job.id}.log").resolve())
            db.commit()
            db.refresh(job)
            payload = background_job_payload(job)
        background_job_manager.launch(job.id)
        return ToolResult(
            "background_run",
            True,
            json.dumps(payload, ensure_ascii=False),
            changed=True,
            metadata={
                "background_job_id": job.id,
                "background_job_active": True,
                "plan_step_id": job.plan_step_id or "",
            },
        )

    def check(
        self,
        *,
        task_id: str | None = None,
        wait: bool = False,
        wait_timeout: int = MAX_BACKGROUND_TIMEOUT_SECONDS,
        _cancel_event: threading.Event | None = None,
    ) -> ToolResult:
        wait_started_at = time.monotonic()
        deadline = time.monotonic() + min(MAX_BACKGROUND_TIMEOUT_SECONDS, max(1, int(wait_timeout)))
        while True:
            with database_module.SessionLocal() as db:
                ownership = self._ownership_clause()
                if task_id and self.session_id is not None:
                    # A recovery Run may explicitly adopt a Job created by an
                    # earlier Run in the same conversation.
                    ownership = BackgroundJob.session_id == self.session_id
                query = select(BackgroundJob).where(ownership)
                if task_id:
                    query = query.where(BackgroundJob.id == str(task_id))
                    job = db.scalar(query)
                    if job is None:
                        return ToolResult("check_background", False, "background job not found", error_code="task_not_found")
                    terminal = job.status in TERMINAL_BACKGROUND_STATUSES
                    if terminal and job.observed_at is None:
                        job.observed_at = _utcnow()
                        job.observed_by_run_id = self.run_id
                        db.commit()
                    payload: Any = background_job_payload(job)
                    statuses = {job.status}
                else:
                    jobs = list(db.scalars(query.order_by(BackgroundJob.created_at.asc())))
                    for job in jobs:
                        if job.status in TERMINAL_BACKGROUND_STATUSES and job.observed_at is None:
                            job.observed_at = _utcnow()
                            job.observed_by_run_id = self.run_id
                    db.commit()
                    payload = [background_job_payload(job) for job in jobs]
                    statuses = {job.status for job in jobs}
                    terminal = not statuses.intersection({"queued", "running"})
            if not wait or terminal:
                ok = not statuses.intersection({"failed", "cancelled"})
                return ToolResult(
                    "check_background",
                    ok,
                    json.dumps(payload, ensure_ascii=False),
                    error_code=None if ok else "background_job_failed",
                    metadata={
                        "background_job_active": not terminal,
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
                    False,
                    json.dumps(payload, ensure_ascii=False),
                    error_code="background_wait_timeout",
                    metadata={
                        "background_job_active": True,
                        "background_wait_seconds": max(0.0, time.monotonic() - wait_started_at),
                    },
                )
            time.sleep(0.25)

    def write_stdin(self, *, task_id: str, input: str, close: bool = False) -> ToolResult:
        normalized_id = str(task_id or "").strip()
        if not normalized_id:
            return ToolResult("write_stdin", False, "task_id is required", error_code="invalid_arguments")
        with database_module.SessionLocal() as db:
            job = db.scalar(select(BackgroundJob).where(
                self._ownership_clause(),
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
        error = background_job_manager.send_input(normalized_id, input, close=bool(close))
        if error:
            return ToolResult("write_stdin", False, error, error_code="stdin_unavailable")
        payload = {"task_id": normalized_id, "chars_sent": len(str(input)), "stdin_closed": bool(close)}
        return ToolResult("write_stdin", True, json.dumps(payload, ensure_ascii=False), changed=True)

    def unresolved_jobs(self) -> list[dict[str, Any]]:
        with database_module.SessionLocal() as db:
            ownership = self._ownership_clause()
            jobs = list(db.scalars(
                select(BackgroundJob).where(
                    ownership,
                    (BackgroundJob.status.in_({"queued", "running"})) | (BackgroundJob.observed_at.is_(None)),
                )
            ))
            return [background_job_payload(job) for job in jobs]

    def active_jobs(self) -> list[dict[str, Any]]:
        with database_module.SessionLocal() as db:
            jobs = list(db.scalars(select(BackgroundJob).where(
                self._ownership_clause(),
                BackgroundJob.status.in_({"queued", "running"}),
            )))
            return [background_job_payload(job) for job in jobs]

    def register_waiter(self) -> list[str]:
        with database_module.SessionLocal() as db:
            if db.get_bind().dialect.name == "sqlite":
                db.execute(text("BEGIN IMMEDIATE"))
            jobs = list(db.scalars(select(BackgroundJob).where(
                self._ownership_clause(),
                BackgroundJob.status.in_({"queued", "running"}),
            )))
            for job in jobs:
                job.waiting_run_id = self.run_id
            db.commit()
            return [job.id for job in jobs]

    def track_terminal_deliveries(self, job_ids: list[str]) -> None:
        """Stage terminal events until the owning model outcome is committed."""

        self._delivered_terminal_ids.update(str(job_id) for job_id in job_ids if str(job_id))

    def delivered_terminal_ids(self) -> list[str]:
        return sorted(self._delivered_terminal_ids)

    def observe_terminal_results(self) -> list[dict[str, Any]]:
        with database_module.SessionLocal() as db:
            query = select(BackgroundJob).where(
                self._ownership_clause(),
                BackgroundJob.status.in_(TERMINAL_BACKGROUND_STATUSES),
                BackgroundJob.observed_at.is_(None),
            )
            if self._delivered_terminal_ids:
                query = query.where(BackgroundJob.id.not_in(self._delivered_terminal_ids))
            jobs = list(db.scalars(
                query.order_by(BackgroundJob.created_at.asc(), BackgroundJob.id.asc())
            ))
            self.track_terminal_deliveries([job.id for job in jobs])
            return [background_job_payload(job) for job in jobs]
