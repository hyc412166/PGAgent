"""Run lifecycle coordinator and restart reconciliation."""

from __future__ import annotations

import asyncio
import hashlib
import json
import threading
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from sqlalchemy import case, func, or_, select, text, update
from sqlalchemy.exc import IntegrityError

from src.persistence import database as database_module
from src.config import settings
from src.persistence.database import (
    Agent,
    Approval,
    BackgroundJob,
    ChatMessage,
    CollaborationEvent,
    CollaborationMessage,
    CollaborationTeam,
    ConversationTurn,
    ConversationCompaction,
    Artifact,
    DelegatedTask,
    DurableTask,
    DEFAULT_AGENT_ID,
    DEFAULT_WORKSPACE_ID,
    ModelConnection,
    MemoryJob,
    MemoryRollout,
    PlanStep,
    Run,
    RunEvent,
    Session,
    Skill,
    TeammateWorker,
    UsageRecord,
    Workspace,
)
from src.agent import (
    AgentRuntime,
    CompletionDecision,
    RunOutcome,
    RuntimeConfig,
    decide_deterministic_completion,
    normalize_usage,
)
from src.context import ContextManager, FilesystemArtifactStore
from src.context.window import message_tokens
from src.context.assembly import COMPACTION_SCHEMA, CONTINUATION_PREFIX
from src.tools import create_default_registry
from src.tools.registry import TOOL_SCHEMAS
from src.tools.types import ToolResult
from src.mcp import attach_mcp_tools, mcp_runtime_pool

from src.tasks import background as background_job_service
from src.model.gateway import ModelConfigurationError, ProviderConfig
from src.artifacts.storage import ArtifactToolStore
from src.tasks.background import BackgroundJobToolStore
from src.memory.service import MemoryToolStore
from src.memory.repository import load_memory_index, record_memory_citations
from src.memory.preferences import memories_enabled
from src.context.instructions import load_instruction_chain, render_workspace_rules
from src.runs.stream import run_stream_broker
from src.tasks.state import (
    recovery_prompt,
    sync_todos_for_run,
    task_checkpoint_for_run,
    todo_state_for_run,
    transition_run_task,
)
from src.tasks.graph import TaskGraphToolStore, ready_steps, refresh_task_state, settle_step, upsert_delegated_graph
from src.agents.collaboration import TeamToolStore, teammate_context
from src.sessions.delivery import (
    classify_error_details,
    classify_exception,
    ensure_run_turn,
    is_terminal_delivery,
    persist_terminal_response,
    public_error_message,
    sync_turn_progress,
    terminal_error_code,
)
from src.context.compaction import (
    _append_runtime_transcript,
    _canonical_arguments,
    _compaction_from_db,
    _compaction_source_is_current,
    _configuration_digest,
    _json_safe,
    _persist_artifact_refs,
    _persist_conversation_compaction,
    _prepare_session_history,
    _utcnow,
    approval_matches_pending,
)
from .configuration import (
    _allowed_runtime_tool_names,
    _effective_connection,
    _explicit_setting,
    _read_selected_skill_instructions,
)
from .delegation_format import (
    _delegate_catalog_prompt,
    _delegate_result_content,
    _model_id_for_delegate,
    _single_line,
)
from .delegation import _SubagentTaskDelegate
from .dependencies import build_model_call


ACTIVE_STATUSES = {"received", "preparing_context", "planning", "acting", "observing", "verifying", "running"}
# ``received`` is included above because it is a schedulable run that may not
# have reached the runtime yet.  Approval pauses are also user-stoppable; a
# stop request must settle the pending approval instead of leaving a dead
# button in the UI.
STOPPABLE_STATUSES = ACTIVE_STATUSES | {"awaiting_approval"}
USER_INTERRUPT_REASON = "user_interrupted"
USER_INTERRUPT_ERROR = "run_interrupted"
_CHILD_FORBIDDEN_ORCHESTRATION_TOOLS = frozenset({
    "task",
    "Agent",
    "spawn_teammate",
    "TeamCreate",
    "TeamDelete",
    "WorkerCreate",
    "WorkerGet",
    "WorkerObserve",
    "WorkerResolveTrust",
    "WorkerAwaitReady",
    "WorkerSendPrompt",
    "WorkerRestart",
    "WorkerTerminate",
    "WorkerObserveCompletion",
    "task_create",
    "task_get",
    "task_update",
    "task_list",
    "TaskCreate",
    "RunTaskPacket",
    "TaskGet",
    "TaskList",
    "TaskStop",
    "TaskUpdate",
    "TaskOutput",
    "claim_task",
    "shutdown_request",
    "plan_approval",
    "integrate_teammate",
})
USER_INTERRUPT_REASONS = frozenset({USER_INTERRUPT_REASON, "parent_user_interrupted"})

# ``stream_sink`` is a low-latency UI channel. Provider payloads never pass
# through directly; reasoning is exposed only as the runtime's typed delta.
_PUBLIC_TRANSIENT_STREAM_EVENT_TYPES = frozenset({
    "assistant_delta",
    "thought_delta",
    "progress",
    "agent_progress",
    "thought_summary",
    "activity_update",
})

_DELEGATE_OUTPUT_LIMIT = 16_000
_DELEGATE_MESSAGE_LIMIT = 20_000
_DELEGATE_AGENT_CATALOG_LIMIT = 40
_DELEGATE_CHILD_SYSTEM_SUFFIX = (
    "\n\n你正在作为受限的子 Agent 执行一项已分配任务。"
    "只完成下方任务并返回可验证的结构化结论；不要再次委派任务，"
    "不要假称调用未启用的工具，也不要把问题直接抛给用户。"
)




class RunCoordinator:
    """Launch, persist and resume local runs without blocking HTTP requests."""

    # Token deltas are intentionally delivered through the in-memory broker;
    # writing one SQLite row per token would turn streaming into a database
    # bottleneck.  We retain a bounded per-run copy here so an explicit user
    # stop can persist the portion that was actually visible at that moment.
    # Completed runs clear the buffer.  The durable event log still contains
    # every thought/tool lifecycle event emitted by ``_event_sink``.
    _stream_buffer_lock = threading.RLock()
    _stream_buffers: dict[str, dict[str, str]] = {}
    _interrupt_requested: dict[str, float] = {}
    _INTERRUPT_MARK_RETENTION_SECONDS = 300.0
    _MAX_PARTIAL_CHARS = 100_000

    def __init__(self) -> None:
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._memory_tasks: dict[str, asyncio.Task[None]] = {}
        self._tool_cancellers: dict[str, Callable[[], None]] = {}
        self._queued_resumes: set[str] = set()
        self._shutting_down = False
        self._event_loop: asyncio.AbstractEventLoop | None = None

    def start(self) -> None:
        """Bind in-process scheduling to the active application event loop."""

        self._shutting_down = False
        self._event_loop = asyncio.get_running_loop()

    def register_tool_canceller(self, run_id: str, callback: Callable[[], None]) -> None:
        self._tool_cancellers[run_id] = callback

    def unregister_tool_canceller(self, run_id: str) -> None:
        self._tool_cancellers.pop(run_id, None)

    def close_mcp_session(self, session_id: str) -> None:
        """Schedule teardown after a durable conversation is deleted."""

        loop = self._event_loop
        if loop is not None and not loop.is_closed():
            loop.call_soon_threadsafe(
                lambda: asyncio.create_task(mcp_runtime_pool.close_session(session_id))
            )

    @classmethod
    def _remember_stream_delta(cls, run_id: str, event: Mapping[str, Any]) -> None:
        """Keep bounded visible deltas for a possible user interruption."""

        event_type = str(event.get("type") or "")
        if event_type == "assistant_delta":
            key = "output"
            delta = str(event.get("delta") or "")
        elif event_type == "thought_delta":
            key = "thought"
            delta = str(event.get("delta") or "")
        elif event_type in {"progress", "agent_progress", "thought_summary", "activity_update"}:
            key = "thought"
            delta = str(event.get("summary") or event.get("progress") or event.get("status_text") or event.get("activity") or "")
        else:
            return
        if not delta:
            return
        with cls._stream_buffer_lock:
            now = time.monotonic()
            stale = [
                item_id
                for item_id, marked_at in cls._interrupt_requested.items()
                if now - marked_at > cls._INTERRUPT_MARK_RETENTION_SECONDS
            ]
            for item_id in stale:
                cls._interrupt_requested.pop(item_id, None)
            if run_id in cls._interrupt_requested:
                # A synchronous provider may finish its worker thread after
                # the asyncio task has been cancelled.  Do not leak late
                # tokens into a run that is already visible as stopped.
                return
            buffer = cls._stream_buffers.setdefault(run_id, {"output": "", "thought": ""})
            current = buffer.get(key, "")
            buffer[key] = (current + delta)[-cls._MAX_PARTIAL_CHARS :]

    @classmethod
    def _partial_stream(cls, run_id: str) -> dict[str, str]:
        with cls._stream_buffer_lock:
            return dict(cls._stream_buffers.get(run_id, {"output": "", "thought": ""}))

    @classmethod
    def _mark_interrupt_and_snapshot(cls, run_id: str) -> dict[str, str]:
        """Atomically freeze the visible stream before publishing a stop."""

        with cls._stream_buffer_lock:
            snapshot = dict(cls._stream_buffers.get(run_id, {"output": "", "thought": ""}))
            # Once the database CAS has claimed the run, no worker-thread
            # token may be published after this point.  The caller commits
            # the durable interruption and clears the buffer afterwards.
            cls._interrupt_requested[run_id] = time.monotonic()
            return snapshot

    @classmethod
    def _clear_stream_buffer(cls, run_id: str, *, interrupted: bool = False) -> None:
        with cls._stream_buffer_lock:
            cls._stream_buffers.pop(run_id, None)
            if interrupted:
                cls._interrupt_requested[run_id] = time.monotonic()
            else:
                cls._interrupt_requested.pop(run_id, None)

    @classmethod
    def _user_interrupt_locked(cls, run: Run) -> bool:
        return run.status == "stopped" and run.stop_reason in USER_INTERRUPT_REASONS

    @staticmethod
    def _stop_requested(run_id: str) -> bool:
        """Check the durable stop marker before starting a newly queued task.

        A stop can race the short window between the HTTP launch commit and
        ``coordinator.launch``.  In that case there is no asyncio task to
        cancel yet; the task must still refuse to enter the model/tool loop.
        """

        with database_module.SessionLocal() as db:
            run = db.get(Run, run_id)
            return bool(run is not None and RunCoordinator._user_interrupt_locked(run))

    def _clear_task(self, completed: asyncio.Task[None], run_id: str) -> None:
        if self._tasks.get(run_id) is completed:
            self._tasks.pop(run_id, None)

    def _launch_queued_resume(self, run_id: str) -> None:
        if run_id not in self._queued_resumes:
            return
        self._queued_resumes.discard(run_id)
        if not self._shutting_down:
            self.launch(run_id, resume=True)

    def launch(self, run_id: str, *, resume: bool = False) -> bool:
        if self._shutting_down:
            return False
        current = self._tasks.get(run_id)
        if current and not current.done():
            if not resume:
                return False
            if run_id not in self._queued_resumes:
                self._queued_resumes.add(run_id)
                loop = asyncio.get_running_loop()
                current.add_done_callback(
                    lambda _completed, key=run_id: loop.call_soon(self._launch_queued_resume, key)
                )
            return True
        task = asyncio.create_task(self._resume(run_id) if resume else self._execute(run_id), name=f"pgagent-run-{run_id}")
        self._tasks[run_id] = task
        task.add_done_callback(lambda completed, key=run_id: self._clear_task(completed, key))
        return True

    def _schedule_background_continuation(self, run_id: str, job_id: str) -> None:
        def launch() -> None:
            run_stream_broker.publish(run_id, {
                "type": "background_continuation_started",
                "run_id": run_id,
                "background_job_id": job_id,
            })
            self.launch(run_id, resume=True)

        loop = self._event_loop
        if loop is not None and loop.is_running():
            loop.call_soon_threadsafe(launch)

    def notify_background_terminal(self, job_id: str) -> bool:
        """Wake a run only after every background job registered to it is terminal."""

        target_run_id: str | None = None
        with database_module.SessionLocal() as db:
            if db.get_bind().dialect.name == "sqlite":
                db.execute(text("BEGIN IMMEDIATE"))
            job = db.get(BackgroundJob, job_id)
            if job is None:
                return False
            target_run_id = str(job.waiting_run_id or job.run_id or "") or None
            if target_run_id is None:
                return False
            active_sibling = db.scalar(select(BackgroundJob.id).where(
                BackgroundJob.waiting_run_id == target_run_id,
                BackgroundJob.status.in_({"queued", "running"}),
            ).limit(1))
            if active_sibling is not None:
                return False
            run = db.get(Run, target_run_id)
            if run is None or run.status != "stopped" or run.stop_reason != "waiting_background":
                return False
            run.status = "received"
            run.stop_reason = None
            run.error_code = None
            run.error_message = None
            run.finished_at = None
            if run.task_id:
                task = db.get(DurableTask, run.task_id)
                if task is not None and task.status == "waiting":
                    task.status = "running"
                    task.resume_summary = "A background terminal event arrived; resume and consume its result."
            db.add(RunEvent(
                run_id=run.id,
                event_type="background_continuation_queued",
                payload={"background_job_id": job.id},
            ))
            db.commit()
        self._schedule_background_continuation(target_run_id, job_id)
        return True

    def reconcile_waiting_background_runs(self) -> list[str]:
        """Recover event/run commit races without invoking the model on a timer."""

        with database_module.SessionLocal() as db:
            waiting = list(db.scalars(select(Run.id).where(
                Run.status == "stopped",
                Run.stop_reason == "waiting_background",
            )))
            terminal_job_by_run = {
                str(run_id): str(job_id)
                for run_id, job_id in db.execute(
                    select(BackgroundJob.waiting_run_id, BackgroundJob.id)
                    .where(
                        BackgroundJob.waiting_run_id.in_(waiting),
                        BackgroundJob.status.in_({"completed", "failed", "cancelled"}),
                    )
                    .order_by(BackgroundJob.finished_at.desc(), BackgroundJob.id.desc())
                ).all()
                if run_id
            }
        resumed: list[str] = []
        for run_id, job_id in terminal_job_by_run.items():
            if self.notify_background_terminal(job_id):
                resumed.append(run_id)
        return resumed

    def launch_memory_job(self, job_id: str) -> bool:
        """Schedule durable auxiliary extraction without blocking reply delivery."""

        if self._shutting_down:
            return False
        with database_module.SessionLocal() as db:
            if not memories_enabled(db):
                return False
        current = self._memory_tasks.get(job_id)
        if current is not None and not current.done():
            return False
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return False
        task = loop.create_task(self._process_memory_job(job_id), name=f"pgagent-memory-{job_id}")
        self._memory_tasks[job_id] = task
        task.add_done_callback(
            lambda completed, key=job_id: self._memory_tasks.pop(key, None)
            if self._memory_tasks.get(key) is completed else None
        )
        return True

    @staticmethod
    def pending_memory_job_ids(*, recover_running: bool = True) -> list[str]:
        """Reset process-interrupted workers and return retryable durable jobs."""

        with database_module.SessionLocal() as db:
            if not memories_enabled(db):
                return []
            if recover_running:
                now = _utcnow()
                for job in db.scalars(select(MemoryJob).where(
                    MemoryJob.status.in_({"running", "committing"}),
                    or_(MemoryJob.lease_expires_at.is_(None), MemoryJob.lease_expires_at <= now),
                )):
                    job.status = "pending" if int(job.attempts or 0) < 3 else "failed"
                    job.error = "worker process restarted before completion"
                    job.lease_expires_at = None
                    if job.status == "failed":
                        db.execute(update(MemoryRollout).where(
                            MemoryRollout.consolidation_job_id == job.id,
                            MemoryRollout.selected_for_phase2_at.is_(None),
                        ).values(status="active", consolidation_job_id=None))
            db.commit()
            return list(db.scalars(
                select(MemoryJob.id)
                .where(MemoryJob.status == "pending", MemoryJob.attempts < 3)
                .order_by(MemoryJob.created_at.asc(), MemoryJob.id.asc())
            ))

    async def _process_memory_job(self, job_id: str) -> None:
        from src.memory.pipeline import memory_pipeline

        await memory_pipeline.process(job_id)
    def stop(
        self,
        run_id: str,
        *,
        db: Any | None = None,
        reason: str = USER_INTERRUPT_REASON,
    ) -> Run | None:
        """Stop one run atomically and cancel its in-process task if present.

        The database transition happens before ``asyncio.Task.cancel``.  This
        ordering closes the race where a provider returns just as the user
        presses stop: ``_persist_outcome`` sees the durable stop marker and
        discards that late completion.  Calling this method repeatedly is
        idempotent for a user-stopped run and never overwrites another
        terminal outcome.

        ``db`` is accepted so HTTP callers can reuse their request-scoped
        session.  Maintenance callers may omit it and receive a detached
        ``Run`` loaded through ``SessionLocal``.
        """

        owns_db = db is None
        session = db or database_module.SessionLocal()
        changed = False
        parent_bridge_events: list[dict[str, Any]] = []
        child_run_ids_to_cancel: list[str] = []
        delegated_parent_id: str | None = None
        partial = {"output": "", "thought": ""}
        stream_marked = False
        try:
            run = session.get(Run, run_id)
            if run is None:
                return None
            prior_status = str(run.status or "")
            prior_stop_reason = str(run.stop_reason or "")
            delegated_parent_id = session.scalar(
                select(DelegatedTask.parent_run_id)
                .where(DelegatedTask.child_run_id == run_id)
            )
            can_stop = prior_status in STOPPABLE_STATUSES or (
                prior_status == "stopped"
                and not is_terminal_delivery(prior_status, prior_stop_reason)
            )
            if can_stop:
                now = _utcnow()
                reason_guard = Run.stop_reason.is_(None) if not prior_stop_reason else Run.stop_reason == prior_stop_reason
                claimed = session.execute(
                    update(Run)
                    .where(
                        Run.id == run_id,
                        Run.status == prior_status,
                        reason_guard,
                    )
                    .values(
                        status="stopped",
                        stop_reason=reason,
                        error_code=USER_INTERRUPT_ERROR,
                        error_message="任务已按你的要求停止。",
                        finished_at=now,
                    )
                )
                if claimed.rowcount != 1:
                    # Another stop/outcome won the compare-and-swap.  Reload
                    # the authoritative terminal row and remain idempotent.
                    session.rollback()
                    run = session.get(Run, run_id)
                    return run
                else:
                    session.refresh(run)
                    run.status = "stopped"
                    run.stop_reason = reason
                    run.error_code = USER_INTERRUPT_ERROR
                    run.error_message = "任务已按你的要求停止。"
                    run.finished_at = now
                # Freeze the stream only after the lifecycle CAS succeeds.
                # Taking the snapshot under the same lock prevents a late
                # worker-thread delta from appearing in the UI but missing
                # from the persisted interruption evidence.
                partial = RunCoordinator._mark_interrupt_and_snapshot(run_id)
                stream_marked = True
                interrupted_payload = {
                    "reason": reason,
                    "previous_status": prior_status,
                    "partial_output": partial.get("output") or None,
                    "partial_thought": partial.get("thought") or None,
                    "partial_output_chars": len(partial.get("output") or ""),
                    "partial_thought_chars": len(partial.get("thought") or ""),
                }
                session.add(RunEvent(
                    run_id=run_id,
                    event_type="run_interrupted",
                    payload=interrupted_payload,
                ))
                session.add(RunEvent(
                    run_id=run_id,
                    event_type="run_stopped",
                    payload={"code": reason, **interrupted_payload},
                ))
                # A stop while waiting for a risky tool must invalidate the
                # approval record.  Otherwise a stale browser click could
                # later resume a run that the user explicitly cancelled.
                pending_approvals = list(session.scalars(
                    select(Approval)
                    .where(Approval.run_id == run_id, Approval.status == "pending")
                ))
                for approval in pending_approvals:
                    approval.status = "superseded"
                    approval.reason = "Approval cancelled because the run was interrupted."
                    approval.decided_at = now
                    session.add(RunEvent(
                        run_id=run_id,
                        event_type="approval_cancelled",
                        payload={"approval_id": approval.id, "reason": reason},
                    ))

                # If the parent is stopped, settle any child runs that are
                # still active.  This preserves the current DelegatedTask
                # side-panel history and prevents an orphan child from
                # reporting a completion after its parent was cancelled.
                child_tasks = list(session.scalars(
                    select(DelegatedTask).where(
                        DelegatedTask.parent_run_id == run_id,
                        DelegatedTask.status == "in_progress",
                    )
                ))
                for child_task in child_tasks:
                    child_run = session.get(Run, child_task.child_run_id) if child_task.child_run_id else None
                    if child_run is None or not (
                        child_run.status in STOPPABLE_STATUSES
                        or (
                            child_run.status == "stopped"
                            and child_run.stop_reason == "delegated_child_awaiting_approval"
                        )
                    ):
                        continue
                    child_run_ids_to_cancel.append(child_run.id)
                    child_run.status = "stopped"
                    child_run.stop_reason = "parent_user_interrupted"
                    child_run.error_code = USER_INTERRUPT_ERROR
                    child_run.error_message = "主任务已按用户要求停止。"
                    child_run.finished_at = now
                    child_approvals = list(session.scalars(
                        select(Approval)
                        .where(Approval.run_id == child_run.id, Approval.status == "pending")
                    ))
                    for approval in child_approvals:
                        approval.status = "superseded"
                        approval.reason = "Approval cancelled because the parent run was interrupted."
                        approval.decided_at = now
                        session.add(RunEvent(
                            run_id=child_run.id,
                            event_type="approval_cancelled",
                            payload={"approval_id": approval.id, "reason": "parent_user_interrupted"},
                        ))
                    session.add(RunEvent(
                        run_id=child_run.id,
                        event_type="run_interrupted",
                        payload={
                            "reason": "parent_user_interrupted",
                            "parent_run_id": run_id,
                        },
                    ))
                    bridge = self._sync_delegated_child_state(
                        session,
                        child_run,
                        status="stopped",
                        stop_reason="parent_user_interrupted",
                        error=child_run.error_message,
                        error_code=USER_INTERRUPT_ERROR,
                    )
                    if bridge is not None:
                        parent_bridge_events.append(bridge)
                    elif child_task.status == "in_progress":
                        # A freshly-created delegation may not have emitted
                        # its delegation_link event yet.  The durable
                        # DelegatedTask row is still enough to settle it and
                        # keep the parent side panel truthful.
                        child_task.status = "blocked"
                        child_result = dict(child_task.result or {})
                        child_result.update({
                            "task_id": child_task.id,
                            "delegation_id": child_task.id,
                            "child_run_id": child_run.id,
                            "parent_run_id": run_id,
                            "status": "stopped",
                            "stop_reason": "parent_user_interrupted",
                            "error_code": USER_INTERRUPT_ERROR,
                            "error": child_run.error_message,
                        })
                        child_task.result = child_result
                        bridge = {
                            "type": "delegated_child_stopped",
                            "parent_run_id": run_id,
                            "task_id": child_task.id,
                            "delegation_id": child_task.id,
                            "child_run_id": child_run.id,
                            "status": "stopped",
                            "stop_reason": "parent_user_interrupted",
                        }
                        session.add(RunEvent(
                            run_id=run_id,
                            event_type="delegated_child_stopped",
                            payload=bridge,
                        ))
                        parent_bridge_events.append(bridge)

                # A direct stop request may target a delegated child from
                # the side panel.  The child runs inside the parent
                # coordinator task, so settle its DelegatedTask before the
                # recursive parent stop below.  Otherwise the parent stop
                # sees a child Run that is already terminal and skips the
                # in-progress delegation row, leaving the side panel stuck.
                if delegated_parent_id and reason in USER_INTERRUPT_REASONS:
                    direct_child_task = session.scalar(
                        select(DelegatedTask)
                        .where(DelegatedTask.child_run_id == run_id)
                        .limit(1)
                    )
                    if direct_child_task is not None and direct_child_task.status == "in_progress":
                        bridge = self._sync_delegated_child_state(
                            session,
                            run,
                            status="stopped",
                            stop_reason=reason,
                            error=run.error_message,
                            error_code=USER_INTERRUPT_ERROR,
                        )
                        if bridge is not None:
                            parent_bridge_events.append(bridge)
                        elif direct_child_task.status == "in_progress":
                            # Keep the durable task truthful even if a
                            # partially-created child has not emitted its
                            # delegation link yet.
                            direct_child_task.status = "blocked"
                            direct_result = dict(direct_child_task.result or {})
                            direct_result.update({
                                "task_id": direct_child_task.id,
                                "delegation_id": direct_child_task.id,
                                "child_run_id": run_id,
                                "parent_run_id": delegated_parent_id,
                                "status": "stopped",
                                "stop_reason": reason,
                                "error_code": USER_INTERRUPT_ERROR,
                                "error": run.error_message,
                            })
                            direct_child_task.result = direct_result
                            direct_bridge = {
                                "type": "delegated_child_stopped",
                                "parent_run_id": delegated_parent_id,
                                "task_id": direct_child_task.id,
                                "delegation_id": direct_child_task.id,
                                "child_run_id": run_id,
                                "status": "stopped",
                                "stop_reason": reason,
                            }
                            session.add(RunEvent(
                                run_id=delegated_parent_id,
                                event_type="delegated_child_stopped",
                                payload=direct_bridge,
                            ))
                            parent_bridge_events.append(direct_bridge)

                # Store a compact snapshot alongside the interruption event so
                # history readers can render the partial response without
                # mistaking it for a completed ChatMessage transcript.
                session.add(RunEvent(
                    run_id=run_id,
                    event_type="runtime_snapshot",
                    payload={
                        "status": "stopped",
                        "output": partial.get("output") or None,
                        "stop_reason": reason,
                        "error": run.error_message,
                        "partial_output": partial.get("output") or None,
                        "partial_thought": partial.get("thought") or None,
                    },
                ))
                transition_run_task(session, run, status="stopped", stop_reason=reason)
                sync_turn_progress(session, run)
                persist_terminal_response(
                    session,
                    run,
                    error_code=reason,
                    error_message="任务已按你的要求停止。",
                )
                session.commit()
                changed = True
                session.refresh(run)
            else:
                # A terminal result is authoritative.  In particular, a
                # late click must not rewrite completed/failed/guard-stopped
                # history as a user interruption.
                session.commit()

            if changed:
                RunCoordinator._clear_stream_buffer(run_id, interrupted=True)
        finally:
            if stream_marked and not changed:
                # A failed DB commit must not leave an active run's stream
                # permanently muted.
                RunCoordinator._clear_stream_buffer(run_id)
            if owns_db:
                session.close()

        if changed:
            terminal_event = {
                "type": "run_stopped",
                "code": reason,
                "reason": "任务已按你的要求停止。",
                "partial_output": partial.get("output") or None,
                "partial_thought": partial.get("thought") or None,
            }
            run_stream_broker.publish(run_id, terminal_event)
            for bridge in parent_bridge_events:
                parent_run_id = str(bridge.get("parent_run_id") or "")
                if parent_run_id:
                    run_stream_broker.publish(parent_run_id, bridge)

        # A delegated child executes inside the parent coordinator task, so a
        # direct stop request for the child must also stop its parent.  This
        # prevents the parent from consuming a synthetic child result and
        # resuming after the user explicitly interrupted the conversation.
        if changed and delegated_parent_id and reason in USER_INTERRUPT_REASONS:
            self.stop(delegated_parent_id, reason=USER_INTERRUPT_REASON)

        # Cancel after the durable transition.  A synchronous provider running
        # in ``asyncio.to_thread`` cannot be force-killed, but its cancelled
        # awaiter will discard the eventual result and the stop marker remains
        # authoritative.
        target_run_ids = list(dict.fromkeys([run_id, *child_run_ids_to_cancel]))
        background_job_service.background_job_manager.cancel_for_runs(target_run_ids)
        try:
            current = asyncio.current_task()
        except RuntimeError:
            current = None
        for target_run_id in target_run_ids:
            canceller = self._tool_cancellers.get(target_run_id)
            if canceller is not None:
                try:
                    canceller()
                except Exception:
                    # Cancellation is best effort for a custom registry; the
                    # durable stop transition and task cancellation still apply.
                    pass
                self.unregister_tool_canceller(target_run_id)
            task = self._tasks.get(target_run_id)
            if task is not None and not task.done() and task is not current:
                task.cancel()

        return run

    def launch_delegated_child_continuation(self, run_id: str) -> bool:
        """Schedule the parent continuation created by a terminal child.

        The parent row has already moved from its explicit child-wait stop to
        ``received`` in the child outcome transaction.  This method only owns
        in-process scheduling and never changes persistence, so callers can
        safely retry it without creating a second continuation task.
        """

        if self._shutting_down:
            return False
        current = self._tasks.get(run_id)
        if current is not None and not current.done():
            return False
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # A synchronous maintenance caller can still leave durable state
            # for an explicit resume; do not create an orphan coroutine.
            return False
        task = loop.create_task(
            self._resume_delegated_child(run_id),
            name=f"pgagent-child-continuation-{run_id}",
        )
        self._tasks[run_id] = task
        task.add_done_callback(lambda completed, key=run_id: self._clear_task(completed, key))
        return True

    def launch_parent_continuation_if_queued(self, parent_event: dict[str, Any] | None) -> bool:
        """Start a durable queued parent continuation exactly once per event."""

        if not parent_event or not parent_event.get("continuation_queued"):
            return False
        parent_run_id = str(parent_event.get("parent_run_id") or "").strip()
        return bool(parent_run_id) and self.launch_delegated_child_continuation(parent_run_id)

    async def shutdown(self) -> None:
        self._shutting_down = True
        self._event_loop = None
        self._queued_resumes.clear()
        had_memory_tasks = any(not task.done() for task in self._memory_tasks.values())
        tasks = [
            task
            for task in [*self._tasks.values(), *self._memory_tasks.values()]
            if not task.done()
        ]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if had_memory_tasks:
            with database_module.SessionLocal() as db:
                for job in db.scalars(select(MemoryJob).where(
                    MemoryJob.status.in_({"running", "committing"})
                )):
                    job.status = "pending" if int(job.attempts or 0) < 3 else "failed"
                    job.error = "worker cancelled during graceful shutdown"
                    job.lease_expires_at = None
                    if job.status == "failed":
                        db.execute(update(MemoryRollout).where(
                            MemoryRollout.consolidation_job_id == job.id,
                            MemoryRollout.selected_for_phase2_at.is_(None),
                        ).values(status="active", consolidation_job_id=None))
                db.commit()
        self._tasks.clear()
        self._memory_tasks.clear()
        await mcp_runtime_pool.shutdown()

    @staticmethod
    def reconcile_interrupted_runs() -> list[str]:
        """Make crashes visible instead of leaving runs permanently active."""

        parent_bridge_events: list[dict[str, Any]] = []
        parent_continuation_run_ids: list[str] = []
        with database_module.SessionLocal() as db:
            runs = list(db.scalars(select(Run).where(Run.status.in_(ACTIVE_STATUSES))))
            for run in runs:
                run.status = "stopped"
                run.stop_reason = "interrupted_restart"
                run.error_code = "interrupted_restart"
                run.error_message = "PGAgent 在运行期间被关闭，可重新发送消息继续任务"
                run.finished_at = _utcnow()
                db.add(RunEvent(run_id=run.id, event_type="run_interrupted", payload={"reason": "process_restart"}))
                transition_run_task(db, run, status="stopped", stop_reason="interrupted_restart")
                parent_event = RunCoordinator._sync_delegated_child_state(
                    db,
                    run,
                    status="stopped",
                    stop_reason="interrupted_restart",
                    error=run.error_message,
                    error_code="interrupted_restart",
                )
                if parent_event is not None:
                    parent_bridge_events.append(parent_event)
                    if parent_event.get("continuation_queued"):
                        parent_run_id = str(parent_event.get("parent_run_id") or "").strip()
                        if parent_run_id:
                            parent_continuation_run_ids.append(parent_run_id)
                sync_turn_progress(db, run)
                persist_terminal_response(
                    db,
                    run,
                    error_code="interrupted_restart",
                    error_message=run.error_message,
                )
            db.commit()
        for parent_event in parent_bridge_events:
            parent_run_id = str(parent_event.get("parent_run_id") or "")
            if parent_run_id:
                run_stream_broker.publish(parent_run_id, parent_event)
        return list(dict.fromkeys(parent_continuation_run_ids))

    @staticmethod
    def reconcile_terminal_deliveries(*, include_legacy: bool = True) -> int:
        """Repair terminal root runs that do not yet have a durable reply."""

        repaired = 0
        with database_module.SessionLocal() as db:
            query = select(Run).where(Run.status.in_({"completed", "failed", "stopped"}))
            if not include_legacy:
                query = query.join(ConversationTurn, Run.turn_id == ConversationTurn.id).where(
                    ConversationTurn.reply_status != "delivered"
                )
            runs = list(db.scalars(query.order_by(Run.started_at.desc(), Run.id.desc())))
            for run in runs:
                if not is_terminal_delivery(str(run.status or ""), run.stop_reason):
                    continue
                turn = ensure_run_turn(db, run)
                if turn is None or turn.reply_status == "delivered":
                    continue
                snapshot = db.scalar(
                    select(RunEvent)
                    .where(RunEvent.run_id == run.id, RunEvent.event_type == "runtime_snapshot")
                    .order_by(RunEvent.created_at.desc(), RunEvent.id.desc())
                )
                snapshot_payload = snapshot.payload if snapshot is not None and isinstance(snapshot.payload, dict) else {}
                output = snapshot_payload.get("output")
                if run.status == "completed" and not str(output or "").strip():
                    run.status = "failed"
                    run.error_code = "empty_model_output"
                    run.error_message = public_error_message(run.error_code)
                    run.finished_at = run.finished_at or _utcnow()
                safe_output = str(output or "").strip() if (
                    run.status == "completed" or run.stop_reason == "needs_user_input"
                ) else ""
                message = persist_terminal_response(
                    db,
                    run,
                    output=safe_output or None,
                    error_code=run.error_code or run.stop_reason,
                    error_message=run.error_message,
                )
                if message is not None:
                    repaired += 1
            db.commit()
        return repaired

    def reconcile_orphaned_runs(self, *, grace_seconds: float = 30.0) -> list[str]:
        """Settle accepted root runs whose coordinator task disappeared."""

        now = _utcnow()
        settled: list[str] = []
        with database_module.SessionLocal() as db:
            runs = list(db.scalars(
                select(Run)
                .where(Run.status.in_(ACTIVE_STATUSES))
                .order_by(Run.started_at.asc(), Run.id.asc())
            ))
            for run in runs:
                if run.id in self._tasks and not self._tasks[run.id].done():
                    continue
                if db.scalar(select(DelegatedTask.id).where(DelegatedTask.child_run_id == run.id).limit(1)):
                    continue
                if run.started_at is None:
                    age = grace_seconds
                else:
                    comparison_now = now if run.started_at.tzinfo is not None else now.replace(tzinfo=None)
                    age = max(0.0, (comparison_now - run.started_at).total_seconds())
                if age < grace_seconds:
                    continue
                turn = ensure_run_turn(db, run)
                if turn is None:
                    continue
                run.status = "failed"
                run.stop_reason = None
                run.error_code = "coordinator_orphaned"
                run.error_message = public_error_message(run.error_code)
                run.finished_at = now
                db.add(RunEvent(
                    run_id=run.id,
                    event_type="integration_failed",
                    payload={"error_type": "coordinator_orphaned", "phase": "scheduling"},
                ))
                sync_turn_progress(db, run)
                persist_terminal_response(
                    db,
                    run,
                    error_code=run.error_code,
                    error_message=run.error_message,
                )
                settled.append(run.id)
            db.commit()
        for run_id in settled:
            run_stream_broker.publish(run_id, {
                "type": "integration_failed",
                "code": "coordinator_orphaned",
                "error": public_error_message("coordinator_orphaned"),
            })
        return settled

    @staticmethod
    def _event_sink(run_id: str):
        status_by_event = {
            "context_prepared": "preparing_context",
            "context_resumed": "acting",
            "model_step_started": "acting",
            "tool_finished": "observing",
            "completion_verification_started": "verifying",
            "completion_verification_rejected": "acting",
        }
        deferred_stream_events = {"approval_requested", "run_completed", "run_stopped", "model_failed"}

        def sink(event: dict[str, Any]) -> None:
            event_type = str(event.get("type") or "runtime_event")
            payload = {key: _json_safe(value) for key, value in event.items() if key != "type"}
            with database_module.SessionLocal() as db:
                # Claim the row with a conditional no-op update before
                # appending the event.  If a user stop already committed, a
                # late ``to_thread`` sink cannot mutate status or append a
                # post-stop lifecycle event.
                claim = db.execute(
                    update(Run)
                    .where(
                        Run.id == run_id,
                        or_(
                            Run.status != "stopped",
                            Run.stop_reason.is_(None),
                            Run.stop_reason.not_in(USER_INTERRUPT_REASONS),
                        ),
                    )
                    .values(current_step=Run.current_step)
                )
                if claim.rowcount != 1:
                    db.rollback()
                    return
                run = db.get(Run, run_id)
                if run is None:
                    db.rollback()
                    return
                db.add(RunEvent(run_id=run_id, event_type=event_type, step=payload.get("step"), payload=payload))
                if event_type in status_by_event:
                    run.status = status_by_event[event_type]
                if run.turn_id:
                    sync_turn_progress(db, run)
                db.commit()
            # Approval and terminal events are published only after the outcome
            # transaction makes their corresponding rows/messages visible.
            if event_type not in deferred_stream_events:
                run_stream_broker.publish(run_id, {"type": event_type, **payload})

        return sink

    @staticmethod
    def _stream_sink(run_id: str):
        def sink(event: dict[str, Any]) -> None:
            safe_event = _json_safe(event)
            if str(safe_event.get("type") or "") not in _PUBLIC_TRANSIENT_STREAM_EVENT_TYPES:
                return
            # Keep the low-latency stream transient, but retain a bounded copy
            # in memory for an explicit stop response.  This avoids one DB
            # transaction per token while still letting the stop endpoint
            # persist exactly what the user saw at cancellation time.
            RunCoordinator._remember_stream_delta(run_id, safe_event)
            with RunCoordinator._stream_buffer_lock:
                interrupted = run_id in RunCoordinator._interrupt_requested
            if interrupted:
                return
            run_stream_broker.publish(run_id, safe_event)

        return sink

    @staticmethod
    def _runtime_snapshot(outcome: RunOutcome) -> dict[str, Any]:
        messages = [] if outcome.stop_reason == "acceptance_failed" else outcome.messages
        acceptance_report = dict(outcome.acceptance_report)
        return _json_safe({
            "status": outcome.status,
            "output": outcome.output,
            "messages": messages,
            "events": outcome.events,
            "steps": outcome.steps,
            "tool_calls": outcome.tool_calls,
            "mode": outcome.mode,
            "stop_reason": outcome.stop_reason,
            "error": outcome.error,
            "pending_approval": outcome.pending_approval,
            "guard_snapshot": outcome.guard_snapshot,
            "usage": outcome.usage,
            "active_elapsed_seconds": outcome.active_elapsed_seconds,
            "runtime_binding": outcome.runtime_binding,
            "compaction_state": outcome.compaction_state,
            "artifact_refs": outcome.artifact_refs,
            "transcript_delta": outcome.transcript_delta,
            "verification_trace": outcome.verification_trace,
            "acceptance_report": acceptance_report,
            "completion_verification_attempts": outcome.completion_verification_attempts,
            "memory_citation": outcome.memory_citation,
        })

    @staticmethod
    def _outcome_from_snapshot(payload: dict[str, Any]) -> RunOutcome:
        return RunOutcome(
            status=str(payload.get("status") or "failed"),
            output=payload.get("output"),
            messages=list(payload.get("messages") or []),
            events=list(payload.get("events") or []),
            steps=int(payload.get("steps") or 0),
            tool_calls=int(payload.get("tool_calls") or 0),
            mode=str(payload.get("mode") or "auto"),
            stop_reason=payload.get("stop_reason"),
            error=payload.get("error"),
            pending_approval=payload.get("pending_approval"),
            guard_snapshot=dict(payload.get("guard_snapshot") or {}),
            usage=normalize_usage(payload.get("usage")),
            active_elapsed_seconds=max(0.0, float(payload.get("active_elapsed_seconds") or 0.0)),
            runtime_binding=dict(payload.get("runtime_binding") or {}),
            compaction_state=dict(payload.get("compaction_state") or {}),
            artifact_refs=[dict(item) for item in payload.get("artifact_refs") or [] if isinstance(item, dict)],
            transcript_delta=(
                [dict(item) for item in payload.get("transcript_delta") or [] if isinstance(item, dict)]
                if "transcript_delta" in payload
                else None
            ),
            verification_trace=[
                dict(item) for item in payload.get("verification_trace") or [] if isinstance(item, dict)
            ],
            acceptance_report=dict(payload.get("acceptance_report") or {}),
            completion_verification_attempts=max(0, int(payload.get("completion_verification_attempts") or 0)),
            memory_citation=dict(payload.get("memory_citation") or {}),
        )

    @staticmethod
    def _resolve_runtime(
        run_id: str,
        *,
        runtime_binding: dict[str, Any] | None = None,
        coordinator_instance: RunCoordinator | None = None,
    ) -> tuple[AgentRuntime, dict[str, Any]]:
        owner = coordinator_instance or coordinator
        with database_module.SessionLocal() as db:
            run = db.get(Run, run_id)
            if run is None:
                raise LookupError("运行不存在")
            session = db.get(Session, run.session_id) if run.session_id else None
            frozen_binding = dict(runtime_binding or {})
            delegated_child = bool(frozen_binding.get("delegation_version"))
            runtime_max_run_seconds: float | None = settings.max_run_seconds
            # Session runs always use the fixed PGAgent coordinator. Keep
            # standalone runs backwards compatible with their explicit Agent.
            # A delegated child is intentionally linked to its parent session
            # for approval visibility, but its frozen binding remains the
            # authority for agent identity on approval resume.
            agent_id = (
                str(frozen_binding.get("agent_id") or run.agent_id or DEFAULT_AGENT_ID)
                if delegated_child
                else (DEFAULT_AGENT_ID if session is not None else (run.agent_id or DEFAULT_AGENT_ID))
            )
            agent = db.get(Agent, agent_id) or db.get(Agent, DEFAULT_AGENT_ID)
            if agent is None:
                raise ModelConfigurationError("当前会话没有选择 Agent")
            if session is not None and not delegated_child:
                session.agent_id = DEFAULT_AGENT_ID
                run.agent_id = DEFAULT_AGENT_ID
            workspace_id = (
                run.workspace_id
                or (session.workspace_id if session else None)
                or agent.workspace_id
                or DEFAULT_WORKSPACE_ID
            )
            workspace = db.get(Workspace, workspace_id) or db.get(Workspace, DEFAULT_WORKSPACE_ID)
            if workspace is None:
                raise ModelConfigurationError("当前 Agent 没有可用工作区")

            messages = _prepare_session_history(db, session) if session else []
            compaction_state = _compaction_from_db(db, session) if session else {}
            if session:
                count = int(db.scalar(
                    select(func.count(ChatMessage.id)).where(ChatMessage.session_id == session.id)
                ) or 0)
                max_sequence = int(db.scalar(
                    select(func.max(ChatMessage.sequence)).where(ChatMessage.session_id == session.id)
                ) or 0)
                transcript_sequence = max_sequence or count
            else:
                transcript_sequence = 0
            agent_tool_ids = list(getattr(agent, "tool_ids", []) or [])
            agent_skill_ids = list(getattr(agent, "skill_ids", []) or [])
            session_skill_ids = list(getattr(session, "skill_ids", []) or []) if session else []
            configured_skill_ids = session_skill_ids or agent_skill_ids
            permission_mode = str(getattr(session, "permission_mode", "smart") or "smart")
            allowed_tool_names = _allowed_runtime_tool_names(agent_tool_ids)
            skill_instructions = _read_selected_skill_instructions(db, configured_skill_ids)
            # A new ordinary user turn starts with no previous task board. A
            # recovery run reads its canonical plan from DurableTask/PlanStep;
            # approval resume may use its frozen binding below only for legacy
            # runs that predate durable task state.
            todo_state = todo_state_for_run(db, run)
            if frozen_binding:
                global_memories_enabled = bool(frozen_binding.get("memories_enabled", True))
                use_memories = bool(frozen_binding.get("use_memories", True))
                memory_use_enabled = global_memories_enabled and use_memories
                memory_index = (
                    str(frozen_binding.get("memory_index") or "")
                    if memory_use_enabled else ""
                )
            else:
                global_memories_enabled = memories_enabled(db)
                use_memories = bool(session.use_memories) if session is not None else True
                memory_use_enabled = global_memories_enabled and use_memories
                memory_index = ""
            if not frozen_binding and memory_use_enabled:
                memory_index = load_memory_index(
                    workspace_id=workspace.id,
                    session_id=session.id if session else None,
                )
            effective_system_prompt = agent.system_prompt
            agents_instructions = ""
            agents_instruction_sources: list[str] = []
            if frozen_binding:
                frozen_agent_id = str(frozen_binding.get("agent_id") or "").strip()
                if frozen_agent_id and agent.id != frozen_agent_id:
                    raise RuntimeError("冻结的子 Agent 已不存在或已被替换，已拒绝续跑")
                required = (
                    "workspace_root",
                    "model_connection_id",
                    "provider",
                    "base_url",
                    "secret_ref",
                    "model_id",
                    "custom_headers_digest",
                )
                missing = [key for key in required if not str(frozen_binding.get(key) or "").strip()]
                if missing:
                    raise RuntimeError(f"运行快照缺少冻结配置：{', '.join(missing)}")
                connection = db.get(ModelConnection, str(frozen_binding["model_connection_id"]))
                if connection is None or not connection.enabled:
                    raise RuntimeError("冻结的模型连接已不存在或被禁用，已拒绝续跑")
                connection_changed = (
                    connection.provider != str(frozen_binding["provider"])
                    or connection.base_url != str(frozen_binding["base_url"])
                    or connection.secret_ref != str(frozen_binding["secret_ref"])
                    or _configuration_digest(connection.custom_headers or {})
                    != str(frozen_binding["custom_headers_digest"])
                )
                if connection_changed:
                    raise RuntimeError("模型连接配置在审批等待期间已改变，已拒绝续跑")
                workspace_root = str(frozen_binding["workspace_root"])
                agents_instructions = str(frozen_binding.get("agents_instructions") or "")
                if isinstance(frozen_binding.get("agents_instruction_sources"), list):
                    agents_instruction_sources = [
                        str(item) for item in frozen_binding["agents_instruction_sources"]
                    ]
                provider_config = ProviderConfig(
                    provider=str(frozen_binding["provider"]),
                    base_url=str(frozen_binding["base_url"]),
                    secret_ref=str(frozen_binding["secret_ref"]),
                    model_id=str(frozen_binding["model_id"]),
                    model_connection_id=str(frozen_binding["model_connection_id"]),
                    thinking_level=str(frozen_binding.get("thinking_level") or "auto"),
                    custom_headers=dict(connection.custom_headers or {}),
                )
                if isinstance(frozen_binding.get("tool_ids"), list):
                    agent_tool_ids = [str(item) for item in frozen_binding["tool_ids"]]
                if isinstance(frozen_binding.get("skill_ids"), list):
                    configured_skill_ids = [str(item) for item in frozen_binding["skill_ids"]]
                if str(frozen_binding.get("permission_mode") or "").strip():
                    permission_mode = str(frozen_binding["permission_mode"])
                if isinstance(frozen_binding.get("allowed_tool_names"), list):
                    allowed_tool_names = _allowed_runtime_tool_names(
                        [str(item) for item in frozen_binding["allowed_tool_names"]]
                    )
                else:
                    # Compatibility for snapshots created before executable
                    # capability bindings were persisted.
                    allowed_tool_names = _allowed_runtime_tool_names(agent_tool_ids)
                if isinstance(frozen_binding.get("skill_instructions"), list):
                    skill_instructions = [
                        dict(item) for item in frozen_binding["skill_instructions"] if isinstance(item, dict)
                    ]
                else:
                    skill_instructions = _read_selected_skill_instructions(db, configured_skill_ids)
                if isinstance(frozen_binding.get("todo_state"), list) and not run.task_id:
                    todo_state = list(frozen_binding["todo_state"])
                if "agent_system_prompt" in frozen_binding:
                    effective_system_prompt = str(frozen_binding["agent_system_prompt"] or "")
                # A persisted child snapshot is never allowed to regain task
                # from a hand-edited/legacy binding during approval resume.
                if frozen_binding.get("delegation_version"):
                    allowed_tool_names = [name for name in allowed_tool_names if name != "task"]
                    if "max_run_seconds" in frozen_binding:
                        frozen_limit = frozen_binding.get("max_run_seconds")
                        runtime_max_run_seconds = (
                            max(0.0, float(frozen_limit))
                            if frozen_limit is not None
                            else None
                        )
            else:
                connection = _effective_connection(db, session, agent)
                if connection is None:
                    raise ModelConfigurationError("当前 Agent 没有可用模型连接")
                session_model = None
                if session is not None and (
                    not session.model_connection_id or session.model_connection_id == connection.id
                ):
                    session_model = session.model_id
                agent_model = agent.model_id if (
                    not agent.model_connection_id or agent.model_connection_id == connection.id
                ) else None
                model_id = session_model or agent_model or connection.default_model
                if not model_id:
                    candidates = [*(connection.discovered_models or []), *(connection.manual_models or [])]
                    model_id = candidates[0] if candidates else None
                if not model_id:
                    raise ModelConfigurationError("模型连接中没有可用模型，请发现模型或填写手动模型 ID")
                thinking_level = _explicit_setting(
                    session.thinking_level if session is not None else None,
                    agent.thinking_level,
                    connection.thinking_level,
                ) or "auto"
                workspace_root = workspace.root_path
                agents_instructions, agents_instruction_sources = load_instruction_chain(workspace_root)
                if "read_artifact" not in allowed_tool_names:
                    allowed_tool_names.append("read_artifact")
                provider_config = ProviderConfig(
                    provider=connection.provider,
                    base_url=connection.base_url,
                    secret_ref=connection.secret_ref,
                    model_id=model_id,
                    model_connection_id=connection.id,
                    thinking_level=thinking_level,
                    custom_headers=dict(connection.custom_headers or {}),
                )
                frozen_binding = {
                    "workspace_root": workspace_root,
                    "agents_instructions": agents_instructions,
                    "agents_instruction_sources": agents_instruction_sources,
                    "model_connection_id": connection.id,
                    "provider": connection.provider,
                    "base_url": connection.base_url,
                    "secret_ref": connection.secret_ref,
                    "model_id": model_id,
                    "thinking_level": thinking_level,
                    "permission_mode": permission_mode,
                    "skill_ids": configured_skill_ids,
                    "tool_ids": agent_tool_ids,
                    "allowed_tool_names": allowed_tool_names,
                    "skill_instructions": skill_instructions,
                    "todo_state": todo_state,
                    "memories_enabled": global_memories_enabled,
                    "use_memories": use_memories,
                    "memory_index": memory_index,
                    # Header values may contain credentials. Persist only a
                    # digest and reject resume if the live values drift.
                    "custom_headers_digest": _configuration_digest(connection.custom_headers or {}),
                }

            delegate_catalog_prompt = "" if frozen_binding.get("delegation_version") else _delegate_catalog_prompt(db, run)
            terminal_background_job_ids: list[str] = []
            if not frozen_binding.get("delegation_version"):
                delegate_catalog_prompt += recovery_prompt(db, run)
            if {"background_run", "check_background"}.issubset(set(allowed_tool_names)):
                delegate_catalog_prompt += (
                    "\n后台作业规则：耗时下载、安装或构建可先调用 background_run，随后继续完成不依赖其结果的工作；"
                    "后台作业入队不代表任务完成。若最终候选结果产生时作业仍在运行，运行时会暂停并由终态事件自动恢复；"
                    "仅在需要主动查看中间日志时调用 check_background，不要反复轮询。\n"
                )
                background_ownership = (
                    BackgroundJob.run_id == run.id
                    if frozen_binding.get("delegation_version")
                    else BackgroundJob.session_id == (session.id if session else None)
                )
                active_background_jobs = list(db.scalars(
                    select(BackgroundJob).where(
                        background_ownership,
                        BackgroundJob.status.in_({"queued", "running"}),
                    ).order_by(BackgroundJob.created_at.asc())
                )) if session is not None else []
                if active_background_jobs:
                    delegate_catalog_prompt += "\n本会话仍在运行的后台作业：\n" + "\n".join(
                        f"- id={job.id} status={job.status} command={_single_line(job.command, limit=240)}"
                        for job in active_background_jobs
                    ) + "\n可继续其他独立工作；无需询问它们是否完成。\n"
                terminal_background_jobs = list(db.scalars(
                    select(BackgroundJob).where(
                        background_ownership,
                        BackgroundJob.status.in_({"completed", "failed", "cancelled"}),
                        BackgroundJob.observed_at.is_(None),
                    ).order_by(BackgroundJob.created_at.asc())
                )) if session is not None else []
                if terminal_background_jobs:
                    terminal_background_job_ids = [job.id for job in terminal_background_jobs]
                    terminal_payloads = []
                    for job in terminal_background_jobs:
                        terminal_payloads.append({
                            "id": job.id,
                            "status": job.status,
                            "exit_code": job.exit_code,
                            "output": job.output_preview,
                            "error": job.error,
                        })
                    delegate_catalog_prompt += (
                        "\n<background-task-events>\n"
                        "以下是运行时推送的后台终态事件；直接使用这些结果继续任务，不要再次轮询。\n"
                        + json.dumps(terminal_payloads, ensure_ascii=False)[:20_000]
                        + "\n</background-task-events>\n"
                    )

            context = {
                "system_prompt": effective_system_prompt,
                # Description remains UI metadata. This system-generated list
                # is capability discovery only: it gives the fixed coordinator
                # the exact enabled child IDs needed by the task tool without
                # exposing child prompts, credentials, or workspaces.
                "agent_instructions": delegate_catalog_prompt,
                "permission_policy": f"permission_mode={permission_mode}",
                "workspace_rules": render_workspace_rules(workspace_root, agents_instructions),
                "memory_index": memory_index,
                "recent_messages": messages,
                "mode": "auto",
                "workspace_root": workspace_root,
                "provider": provider_config,
                "model_connection_id": provider_config.model_connection_id,
                "runtime_binding": frozen_binding,
                # These selections are durable configuration metadata. The
                # active runtime only acts on capabilities it explicitly
                # implements; it must not infer executable permissions merely
                # from a catalog selection.
                "permission_mode": permission_mode,
                "skill_ids": configured_skill_ids,
                "tool_ids": agent_tool_ids,
                "allowed_tool_names": allowed_tool_names,
                "skill_instructions": skill_instructions,
                "todo_state": todo_state,
                "max_run_seconds": runtime_max_run_seconds,
                "session_id": session.id if session else None,
                "compaction_state": compaction_state,
                "context_sequence": transcript_sequence,
            }
            db.commit()

        task_delegate = None
        if not context["runtime_binding"].get("delegation_version"):
            task_delegate = _SubagentTaskDelegate(
                coordinator=owner,
                parent_run_id=run_id,
                parent_agent_id=agent.id,
                parent_binding=dict(context["runtime_binding"]),
                parent_allowed_tool_names=list(context["allowed_tool_names"]),
                permission_mode=str(context["permission_mode"]),
            )

        background_store = BackgroundJobToolStore(
            run_id=run_id,
            workspace_id=workspace.id,
            session_id=session.id if session else None,
            workspace_root=context["workspace_root"],
            include_session_jobs=not bool(context["runtime_binding"].get("delegation_version")),
        )
        background_store.track_terminal_deliveries(terminal_background_job_ids)
        delegated_teammate_id = str(context["runtime_binding"].get("teammate_id") or "")
        team_store = (
            TeamToolStore(
                run_id=str(context["runtime_binding"].get("parent_run_id") or run_id),
                session_id=session.id if session else None,
                workspace_root=context["workspace_root"],
                actor_worker_id=delegated_teammate_id or None,
            )
            if not context["runtime_binding"].get("delegation_version") or delegated_teammate_id
            else None
        )
        task_store = None if context["runtime_binding"].get("delegation_version") else TaskGraphToolStore(
            run_id=run_id
        )
        runtime_artifact_store = FilesystemArtifactStore(
            settings.data_dir / "artifacts" / str(session.id if session else run.id)
        )
        registry = create_default_registry(
            context["workspace_root"],
            allowed_tool_names=context["allowed_tool_names"],
            permission_mode=context["permission_mode"],
            skill_instructions=context["skill_instructions"],
            todo_state=context["todo_state"],
            todo_change_sink=(
                (lambda todos, key=run_id: sync_todos_for_run(key, todos))
                if not context["runtime_binding"].get("delegation_version") else None
            ),
            memory_store=(
                MemoryToolStore(
                    workspace_id=workspace.id,
                    session_id=session.id if session else None,
                )
                if memory_use_enabled else None
            ),
            artifact_store=ArtifactToolStore(runtime_artifact_store),
            background_store=background_store,
            team_store=team_store,
            task_store=task_store,
            task_delegate=task_delegate,
        )
        context["background_store"] = background_store
        owner.register_tool_canceller(run_id, registry.cancel_active)
        runtime = AgentRuntime(
            model_call=build_model_call(context["provider"]),
            tool_registry=registry,
            context_manager=ContextManager(max_tokens=settings.context_limit_tokens),
            artifact_store=runtime_artifact_store,
            event_sink=type(owner)._event_sink(run_id),
            stream_sink=type(owner)._stream_sink(run_id),
            config=RuntimeConfig(
                max_steps=settings.max_steps,
                max_tool_calls=settings.max_tool_calls,
                identical_call_limit=settings.max_identical_calls,
                no_progress_limit=settings.no_progress_limit,
                context_compaction_threshold_tokens=settings.compact_threshold_tokens,
                model_timeout_seconds=settings.model_timeout_seconds,
                max_run_seconds=context["max_run_seconds"],
            ),
            task_state_provider=(
                (lambda key=run_id: task_checkpoint_for_run(key))
                if not context["runtime_binding"].get("delegation_version") else None
            ),
        )
        return runtime, context

    @staticmethod
    def _delegation_link(
        db: Any,
        run: Run,
        runtime_binding: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """Find the durable parent/task link for a delegated child run."""

        binding = dict(runtime_binding or {})
        if binding.get("delegation_version") and binding.get("delegation_id"):
            return {
                "delegation_id": str(binding["delegation_id"]),
                "parent_run_id": str(binding.get("parent_run_id") or ""),
                "parent_agent_id": str(binding.get("parent_agent_id") or ""),
                "parent_session_id": binding.get("parent_session_id"),
            }
        link = db.scalar(
            select(RunEvent)
            .where(RunEvent.run_id == run.id, RunEvent.event_type == "delegation_link")
            .order_by(RunEvent.created_at.desc(), RunEvent.id.desc())
        )
        if link is None or not isinstance(link.payload, dict):
            return None
        payload = link.payload
        delegation_id = str(payload.get("delegation_id") or "").strip()
        if delegation_id:
            return {
                "delegation_id": delegation_id,
                "parent_run_id": str(payload.get("parent_run_id") or ""),
                "parent_agent_id": str(payload.get("parent_agent_id") or ""),
                "parent_session_id": payload.get("parent_session_id"),
            }
        return None

    @staticmethod
    def _sync_delegated_child_state(
        db: Any,
        run: Run,
        *,
        status: str,
        output: str | None = None,
        stop_reason: str | None = None,
        error: str | None = None,
        error_code: str | None = None,
        pending_approval: dict[str, Any] | None = None,
        steps: int = 0,
        tool_calls: int = 0,
        usage: dict[str, Any] | None = None,
        workspace_changed: bool = False,
        runtime_binding: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """Atomically mirror a child state into its delegation and parent audit trail."""

        link = RunCoordinator._delegation_link(db, run, runtime_binding)
        if link is None:
            return None
        delegation_id = str(link.get("delegation_id") or "")
        if not delegation_id:
            return None
        task = db.get(DelegatedTask, delegation_id)
        if task is None:
            return None
        existing_child_run_id = str(task.child_run_id or (task.result or {}).get("child_run_id") or "")
        if existing_child_run_id and existing_child_run_id != run.id:
            return None

        binding = dict(runtime_binding or {})
        if not binding:
            snapshot = db.scalar(
                select(RunEvent)
                .where(RunEvent.run_id == run.id, RunEvent.event_type == "runtime_snapshot")
                .order_by(RunEvent.created_at.desc(), RunEvent.id.desc())
            )
            if snapshot is not None and isinstance(snapshot.payload, dict):
                stored_binding = snapshot.payload.get("runtime_binding")
                if isinstance(stored_binding, dict):
                    binding = dict(stored_binding)
        if not binding:
            # Failures before a first runtime snapshot still have the public
            # frozen selection captured when the delegation was created.
            stored_binding = (task.result or {}).get("binding")
            if isinstance(stored_binding, dict):
                binding = dict(stored_binding)
        child = db.get(Agent, run.agent_id or task.child_agent_id)
        child_id = child.id if child is not None else (run.agent_id or task.child_agent_id or "")
        child_name = _single_line(child.name, limit=120) if child is not None else "Child Agent"
        public_binding = _SubagentTaskDelegate._public_binding(binding) if binding else {}
        safe_pending = _SubagentTaskDelegate._safe_pending_summary(pending_approval)
        linked_plan_step = db.get(PlanStep, task.plan_step_id) if task.plan_step_id else None
        waiting_background = status == "stopped" and stop_reason == "waiting_background"
        public_status = "waiting_background" if waiting_background else status
        payload = {
            "task_id": task.id,
            "delegation_id": task.id,
            "child_run_id": run.id,
            "parent_run_id": link["parent_run_id"],
            "parent_session_id": run.session_id or link.get("parent_session_id"),
            "plan_step_id": task.plan_step_id,
            "plan_step_external_id": linked_plan_step.external_id if linked_plan_step is not None else None,
            "teammate_id": task.teammate_id,
            "agent": {"id": child_id, "name": child_name},
            "status": public_status,
            "output": str(output or "")[:_DELEGATE_OUTPUT_LIMIT],
            "output_truncated": len(str(output or "")) > _DELEGATE_OUTPUT_LIMIT,
            "stop_reason": _single_line(stop_reason, limit=200),
            "error_code": _single_line(error_code, limit=120),
            "error": _single_line(error, limit=2_000),
            "pending_approval": safe_pending,
            "steps": max(0, int(steps or 0)),
            "tool_calls": max(0, int(tool_calls or 0)),
            "workspace_changed": bool(workspace_changed),
            "usage": normalize_usage(usage),
            "binding": public_binding,
        }
        task_status = "in_progress" if status == "awaiting_approval" or waiting_background else (
            "completed" if status == "completed" else "blocked"
        )
        changed = (
            task.status != task_status
            or _canonical_arguments(task.result) != _canonical_arguments(payload)
        )
        if changed:
            task.status = task_status
            task.result = payload
        worker = db.get(TeammateWorker, task.teammate_id) if task.teammate_id else None
        if worker is not None:
            if status == "awaiting_approval" or waiting_background:
                worker.status = "working"
            elif status == "completed":
                worker.status = "stopped" if worker.status == "stopping" else "idle"
                worker.current_plan_step_id = None
            else:
                # Assignment failure is durable history, not destruction of
                # the reusable teammate identity. A later graph step may
                # safely assign the same teammate again.
                worker.status = "stopped" if worker.status == "stopping" else "idle"
                worker.current_plan_step_id = None
            worker.last_run_id = run.id
        if task.plan_step_id:
            plan_step = linked_plan_step or db.get(PlanStep, task.plan_step_id)
            if plan_step is not None and status in {"completed", "failed", "stopped"} and not waiting_background:
                plan_step.status = "completed" if status == "completed" else "failed"
                plan_step.result = str(output or "")
                plan_step.error = None if status == "completed" else str(error or stop_reason or status)
                plan_step.remaining_work = [] if status == "completed" else list(
                    plan_step.remaining_work or [plan_step.title]
                )
                if status == "completed":
                    plan_step.completed_work = list(plan_step.completed_work or [plan_step.title])
                plan_step.completed_at = _utcnow()
                plan_step.assigned_run_id = run.id
                refresh_task_state(db, plan_step.task_id)

        event_type = {
            "awaiting_approval": "delegated_child_awaiting_approval",
            "completed": "delegated_child_completed",
            "stopped": "delegated_child_stopped",
            "failed": "delegated_child_failed",
        }.get(status, "delegated_child_incomplete")
        if waiting_background:
            event_type = "delegated_child_waiting_background"
        if changed:
            db.add(CollaborationEvent(
                task_id=linked_plan_step.task_id if linked_plan_step is not None else None,
                plan_step_id=task.plan_step_id,
                run_id=link["parent_run_id"] or None,
                source_kind="delegated_child",
                source_id=run.id,
                event_type=event_type,
                payload=payload,
            ))
            if worker is not None and status in {"completed", "failed", "stopped"} and not waiting_background:
                worker_message = CollaborationMessage(
                    team_id=worker.team_id,
                    sender_worker_id=worker.id,
                    recipient_worker_id=None,
                    message_type="task_result",
                    content=str(output or error or stop_reason or status)[:20_000],
                    payload={
                        "delegation_id": task.id,
                        "plan_step_id": task.plan_step_id,
                        "status": status,
                    },
                )
                db.add(worker_message)
        continuation_queued = False
        if status in {"completed", "failed", "stopped"} and not waiting_background and link["parent_run_id"]:
            # A parallel parent must resume only after every child that paused
            # for approval has reached a terminal state.  At the point the
            # parent is stopped for delegated approval, any remaining
            # in-progress delegation belongs to that waiting parallel batch.
            db.flush()
            waiting_sibling_id = db.scalar(
                select(DelegatedTask.id)
                .where(
                    DelegatedTask.parent_run_id == link["parent_run_id"],
                    DelegatedTask.status == "in_progress",
                )
                .limit(1)
            )
            parent = db.get(Run, link["parent_run_id"])
            # The parent was deliberately stopped rather than completed while
            # the child waited.  Only that exact state can be safely resumed;
            # a still-active parent will naturally receive the synchronous
            # delegate result, and unrelated stopped runs must remain stopped.
            if (
                parent is not None
                and parent.status == "stopped"
                and parent.stop_reason in {"delegated_child_awaiting_approval", "delegated_child_waiting_event"}
                and stop_reason not in USER_INTERRUPT_REASONS
                and waiting_sibling_id is None
            ):
                parent.status = "received"
                parent.stop_reason = None
                parent.error_message = None
                parent.finished_at = None
                if parent.task_id:
                    parent_task = db.get(DurableTask, parent.task_id)
                    if parent_task is not None and parent_task.status == "waiting":
                        parent_task.status = "running"
                        parent_task.resume_summary = "A delegated child terminal event arrived; resume coordination."
                continuation_queued = True
                db.add(RunEvent(
                    run_id=parent.id,
                    event_type="delegated_child_continuation_queued",
                    payload={
                        "child_run_id": run.id,
                        "task_id": task.id,
                        "delegation_id": task.id,
                        "child_status": status,
                    },
                ))

        parent_event = {
            "type": event_type,
            "parent_run_id": link["parent_run_id"],
            "task_id": task.id,
            "delegation_id": task.id,
            "child_run_id": run.id,
            "child_agent_id": child_id,
            "status": status,
            "pending_approval": safe_pending,
            "stop_reason": _single_line(stop_reason, limit=200),
            "error_code": _single_line(error_code, limit=120),
            "continuation_queued": continuation_queued,
        }
        if changed and link["parent_run_id"]:
            db.add(RunEvent(
                run_id=link["parent_run_id"],
                event_type=event_type,
                payload=parent_event,
            ))

        # A child response is deliberately *not* a normal chat message.
        # Its complete structured result remains on DelegatedTask and in the
        # child Run timeline, while the parent coordinator is solely
        # responsible for the user-facing final reply.  This prevents the
        # same answer appearing once as a child transcript and again as the
        # parent's synthesis.
        return parent_event if (changed or continuation_queued) and link["parent_run_id"] else None

    def reconcile_delegated_child_terminal(
        self,
        db: Any,
        run: Run,
        *,
        status: str,
        stop_reason: str | None = None,
        error: str | None = None,
        error_code: str | None = None,
    ) -> dict[str, Any] | None:
        """Expose a transaction-local terminal sync for approval rejection."""

        return self._sync_delegated_child_state(
            db,
            run,
            status=status,
            stop_reason=stop_reason,
            error=error,
            error_code=error_code,
        )

    @staticmethod
    def _persist_outcome(
        run_id: str,
        outcome: RunOutcome,
        consumed_background_event_ids: Iterable[str] = (),
    ) -> None:
        event_error_type = next(
            (
                str(event.get("error_type") or event.get("error_code") or event.get("code") or "")
                for event in reversed(outcome.events)
                if event.get("error_type") or event.get("error_code") or event.get("code")
            ),
            "",
        )
        effective_status = str(outcome.status or "failed")
        effective_stop_reason = outcome.stop_reason
        normalized_error_code: str | None = None
        safe_error_message: str | None = None
        if effective_status == "completed" and not str(outcome.output or "").strip():
            effective_status = "failed"
            effective_stop_reason = None
            normalized_error_code = "empty_model_output"
            safe_error_message = public_error_message(normalized_error_code)
        elif effective_status == "failed":
            normalized_error_code = classify_error_details(event_error_type, outcome.error)
            safe_error_message = public_error_message(normalized_error_code)
        elif effective_status == "stopped" and is_terminal_delivery(effective_status, effective_stop_reason):
            normalized_error_code = terminal_error_code(
                status=effective_status,
                stop_reason=effective_stop_reason,
                error_code=event_error_type or None,
            )
            safe_error_message = public_error_message(normalized_error_code, effective_status)
        elif effective_status == "stopped":
            # Waiting for a delegated child is a resumable pause, not the
            # terminal delivery boundary for the user's turn.
            safe_error_message = outcome.error
        snapshot = RunCoordinator._runtime_snapshot(outcome)
        snapshot["status"] = effective_status
        snapshot["delivery_error_code"] = normalized_error_code
        publish_type = {
            "awaiting_approval": "approval_requested",
            "completed": "run_completed",
            "stopped": "run_stopped",
            "failed": "model_failed",
        }.get(effective_status)
        publish_event = next(
            (
                _json_safe(event)
                for event in reversed(outcome.events)
                if event.get("type") == publish_type
            ),
            None,
        )
        if effective_status != outcome.status:
            publish_event = {
                "type": "model_failed",
                "code": normalized_error_code,
                "error": safe_error_message,
            }
        persisted = False
        parent_bridge_event: dict[str, Any] | None = None
        extraction_job_id: str | None = None
        with database_module.SessionLocal() as db:
            # Serialize this persistence path against ``stop()``.  Whichever
            # conditional row update claims the run first owns the lifecycle
            # transition; a provider result that arrives after a user stop
            # is discarded before it can append messages or context.
            claim = db.execute(
                update(Run)
                .where(
                    Run.id == run_id,
                    or_(
                        Run.status != "stopped",
                        Run.stop_reason.is_(None),
                        Run.stop_reason.not_in(USER_INTERRUPT_REASONS),
                    ),
                )
                .values(current_step=Run.current_step)
            )
            if claim.rowcount != 1:
                run = db.get(Run, run_id)
                if run is not None and RunCoordinator._user_interrupt_locked(run):
                    db.add(RunEvent(
                        run_id=run_id,
                        event_type="run_outcome_discarded",
                        payload={"reason": USER_INTERRUPT_REASON, "late_status": outcome.status},
                    ))
                    db.commit()
                else:
                    db.rollback()
                RunCoordinator._clear_stream_buffer(run_id, interrupted=True)
                coordinator.unregister_tool_canceller(run_id)
                return
            run = db.get(Run, run_id)
            if run is None:
                return
            # An explicit stop commits before cancelling the in-process task.
            # A provider may still return from a synchronous worker or a
            # cancellation race may let this method run; never let that late
            # outcome rewrite the user's durable stopped state.
            if RunCoordinator._user_interrupt_locked(run):
                db.add(RunEvent(
                    run_id=run_id,
                    event_type="run_outcome_discarded",
                    payload={
                        "reason": USER_INTERRUPT_REASON,
                        "late_status": outcome.status,
                    },
                ))
                db.commit()
                RunCoordinator._clear_stream_buffer(run_id, interrupted=True)
                coordinator.unregister_tool_canceller(run_id)
                return
            turn = ensure_run_turn(db, run)
            run.status = effective_status
            run.current_step = outcome.steps
            run.tool_calls = outcome.tool_calls
            run.stop_reason = effective_stop_reason
            run.error_code = normalized_error_code
            run.error_message = safe_error_message
            if effective_status in {"completed", "failed", "stopped"}:
                run.finished_at = _utcnow()
            else:
                run.finished_at = None
            transition_run_task(db, run, status=effective_status, stop_reason=effective_stop_reason)
            sync_turn_progress(db, run)
            db.add(RunEvent(run_id=run_id, event_type="runtime_snapshot", payload=snapshot))

            parent_bridge_event = RunCoordinator._sync_delegated_child_state(
                db,
                run,
                status=effective_status,
                output=str(outcome.output or "").strip() or None,
                stop_reason=effective_stop_reason,
                error=safe_error_message,
                error_code=normalized_error_code,
                pending_approval=outcome.pending_approval,
                steps=outcome.steps,
                tool_calls=outcome.tool_calls,
                usage=normalize_usage(outcome.usage),
                workspace_changed=_SubagentTaskDelegate._changed_workspace(outcome),
                runtime_binding=outcome.runtime_binding,
            )

            if run.session_id and not outcome.runtime_binding.get("delegation_version"):
                session_for_context = db.get(Session, run.session_id)
                source_sequence_valid = (
                    _compaction_source_is_current(db, session_for_context, outcome)
                    if session_for_context is not None
                    else None
                )
                _append_runtime_transcript(
                    db,
                    run_id,
                    run.session_id,
                    outcome,
                    terminal_managed=turn is not None,
                )
                db.flush()
                if session_for_context is not None:
                    _persist_conversation_compaction(
                        db,
                        session_for_context,
                        run_id,
                        outcome,
                        source_sequence_valid=source_sequence_valid,
                    )

            usage = normalize_usage(outcome.usage)
            if usage["request_count"]:
                connection_id = usage["model_connection_id"]
                record = db.scalar(select(UsageRecord).where(UsageRecord.run_id == run_id))
                if record is None:
                    record = UsageRecord(
                        run_id=run_id,
                        session_id=run.session_id,
                        agent_id=run.agent_id,
                        model_connection_id=connection_id,
                        model_id=usage["model_id"],
                        provider=usage["provider"],
                    )
                    db.add(record)
                record.model_connection_id = connection_id
                record.model_id = usage["model_id"]
                record.provider = usage["provider"]
                record.request_count = usage["request_count"]
                record.input_tokens = usage["input_tokens"]
                record.output_tokens = usage["output_tokens"]
                record.cache_creation_tokens = usage["cache_creation_tokens"]
                record.cache_read_tokens = usage["cache_read_tokens"]
                record.total_tokens = usage["total_tokens"]
                record.cost_usd = usage["cost_usd"]

            if effective_status == "awaiting_approval" and outcome.pending_approval:
                pending = outcome.pending_approval
                existing_pending = list(db.scalars(
                    select(Approval)
                    .where(Approval.run_id == run_id, Approval.status == "pending")
                    .order_by(Approval.created_at.asc(), Approval.id.asc())
                ))
                matching: Approval | None = None
                for existing in existing_pending:
                    if matching is None and approval_matches_pending(existing, pending):
                        matching = existing
                        continue
                    existing.status = "superseded"
                    existing.decided_at = _utcnow()
                    db.add(RunEvent(
                        run_id=run_id,
                        event_type="approval_superseded",
                        payload={"approval_id": existing.id, "reason": "pending_call_changed"},
                    ))
                if matching is None:
                    db.add(Approval(
                        run_id=run_id,
                        tool_name=str(pending.get("tool_name") or "unknown"),
                        arguments=dict(pending.get("arguments") or {}),
                        reason=str(pending.get("reason") or "该工具会修改本机状态，需要你的确认"),
                    ))
            terminal_reasoning = next(
                (
                    str(message.get("reasoning_content") or "")
                    for message in reversed(list(outcome.transcript_delta or outcome.messages))
                    if isinstance(message, dict)
                    and message.get("role") == "assistant"
                    and not message.get("tool_calls")
                    and str(message.get("content") or "").strip() == str(outcome.output or "").strip()
                ),
                "",
            )
            persist_terminal_response(
                db,
                run,
                output=str(outcome.output or "").strip() or None,
                error_code=normalized_error_code,
                error_message=safe_error_message,
                provider_payload=(
                    {"reasoning_content": terminal_reasoning}
                    if terminal_reasoning else None
                ),
            )
            cited = (
                []
                if outcome.runtime_binding.get("delegation_version")
                else record_memory_citations(db, run=run, citation=outcome.memory_citation)
            )
            if cited:
                db.add(RunEvent(
                    run_id=run.id,
                    event_type="memory_citations_recorded",
                    payload={
                        "targets": [
                            {"type": item.target_type, "id": item.target_id}
                            for item in cited
                        ]
                    },
                ))
            if (
                effective_status == "completed"
                and turn is not None
                and run.session_id
                and not outcome.runtime_binding.get("delegation_version")
                and bool(outcome.runtime_binding.get("memories_enabled", True))
                and str(outcome.output or "").strip()
            ):
                existing_job = db.scalar(select(MemoryJob).where(
                    MemoryJob.run_id == run.id,
                    MemoryJob.kind == "extract",
                ))
                if existing_job is None:
                    db.flush()
                    source_end_sequence = int(db.scalar(select(func.max(ChatMessage.sequence)).where(
                        ChatMessage.session_id == run.session_id
                    )) or 0)
                    candidate_job = MemoryJob(
                        run_id=run.id,
                        session_id=run.session_id,
                        workspace_id=run.workspace_id,
                        kind="extract",
                        status="deferred",
                        payload={
                            "turn_id": turn.id,
                            "source_end_sequence": source_end_sequence,
                            "runtime_binding": dict(outcome.runtime_binding or {}),
                        },
                    )
                    try:
                        with db.begin_nested():
                            db.add(candidate_job)
                            db.flush()
                        existing_job = candidate_job
                    except IntegrityError:
                        existing_job = db.scalar(select(MemoryJob).where(
                            MemoryJob.run_id == run.id,
                            MemoryJob.kind == "extract",
                        ))
                if existing_job is not None and existing_job.status == "pending":
                    extraction_job_id = existing_job.id
            if run.session_id:
                session = db.get(Session, run.session_id)
                if session is not None:
                    session.updated_at = _utcnow()
                    # Recompute after terminal delivery so the UI immediately
                    # includes the final assistant reply in context usage.
                    _prepare_session_history(db, session)
                    prepared = next(
                        (
                            event for event in reversed(outcome.events)
                            if event.get("type") == "context_prepared"
                            and isinstance(event.get("estimated_tokens"), (int, float))
                        ),
                        None,
                    )
                    if prepared is not None and not is_terminal_delivery(
                        effective_status, effective_stop_reason
                    ):
                        session.context_tokens = min(
                            max(0, int(prepared.get("estimated_tokens") or 0)),
                            settings.context_limit_tokens,
                        )
            background_event_ids = sorted({
                str(job_id) for job_id in consumed_background_event_ids if str(job_id)
            })
            if background_event_ids:
                consumed_at = _utcnow()
                db.execute(
                    update(BackgroundJob)
                    .where(
                        BackgroundJob.id.in_(background_event_ids),
                        BackgroundJob.observed_at.is_(None),
                    )
                    .values(observed_at=consumed_at, observed_by_run_id=run_id)
                )
                db.execute(
                    update(CollaborationEvent)
                    .where(
                        CollaborationEvent.source_kind == "background_job",
                        CollaborationEvent.source_id.in_(background_event_ids),
                        CollaborationEvent.consumed_at.is_(None),
                    )
                    .values(consumed_at=consumed_at, consumer_run_id=run_id)
                )
            db.commit()
            persisted = True
        if persisted and publish_event is not None:
            run_stream_broker.publish(run_id, publish_event)
        if persisted and parent_bridge_event is not None:
            parent_run_id = str(parent_bridge_event.get("parent_run_id") or "")
            if parent_run_id:
                run_stream_broker.publish(parent_run_id, parent_bridge_event)
        if persisted:
            coordinator.launch_parent_continuation_if_queued(parent_bridge_event)
            if effective_status == "stopped" and effective_stop_reason == "waiting_background":
                coordinator.reconcile_waiting_background_runs()
            if extraction_job_id:
                coordinator.launch_memory_job(extraction_job_id)
            RunCoordinator._clear_stream_buffer(run_id)
        coordinator.unregister_tool_canceller(run_id)

    async def _execute(self, run_id: str) -> None:
        try:
            if self._stop_requested(run_id):
                return
            runtime, context = self._resolve_runtime(run_id, coordinator_instance=self)
            await attach_mcp_tools(
                runtime.tool_registry,
                session_key=str(context.get("session_id") or run_id),
                workspace_root=str(context["workspace_root"]),
                frozen_tools=context["runtime_binding"].get("mcp_tools"),
                progress_sink=self._event_sink(run_id),
            )
            self._install_completion_verifier(runtime, context)
            outcome = await runtime.run(
                system_prompt=context["system_prompt"],
                agent_instructions=context["agent_instructions"],
                workspace_rules=context["workspace_rules"],
                memory_index=context.get("memory_index"),
                recent_messages=context["recent_messages"],
                mode=context["mode"],
                compaction_state=context.get("compaction_state"),
                permission_policy=context.get("permission_policy"),
                session_id=context.get("session_id"),
                context_sequence=int(context.get("context_sequence") or 0),
            )
            outcome.runtime_binding = {
                **dict(context["runtime_binding"]),
                **runtime.tool_registry.runtime_state(),
            }
            self._persist_outcome(
                run_id,
                outcome,
                context["background_store"].delivered_terminal_ids(),
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._persist_failure(run_id, exc)

    async def _resume(self, run_id: str) -> None:
        try:
            if self._stop_requested(run_id):
                return
            with database_module.SessionLocal() as db:
                snapshot = db.scalar(
                    select(RunEvent)
                    .where(RunEvent.run_id == run_id, RunEvent.event_type == "runtime_snapshot")
                    .order_by(RunEvent.created_at.desc(), RunEvent.id.desc())
                )
                if snapshot is None:
                    raise RuntimeError("缺少可恢复的运行快照")
                prior = self._outcome_from_snapshot(snapshot.payload)
            if not prior.runtime_binding:
                raise RuntimeError("运行快照缺少冻结配置，已拒绝在可变环境中续跑")
            runtime, context = self._resolve_runtime(
                run_id,
                runtime_binding=prior.runtime_binding,
                coordinator_instance=self,
            )
            await attach_mcp_tools(
                runtime.tool_registry,
                session_key=str(context.get("session_id") or run_id),
                workspace_root=str(context["workspace_root"]),
                frozen_tools=context["runtime_binding"].get("mcp_tools"),
                progress_sink=self._event_sink(run_id),
            )
            self._install_completion_verifier(runtime, context)
            if prior.stop_reason == "waiting_background":
                outcome = await runtime.run(
                    system_prompt=context["system_prompt"],
                    agent_instructions=context["agent_instructions"],
                    workspace_rules=context["workspace_rules"],
                    memory_index=context.get("memory_index"),
                    recent_messages=[],
                    mode=prior.mode,
                    prepared_messages=prior.messages,
                    prior_events=prior.events,
                    guard_snapshot=prior.guard_snapshot,
                    prior_usage=prior.usage,
                    prior_active_elapsed_seconds=prior.active_elapsed_seconds,
                    compaction_state=prior.compaction_state,
                    permission_policy=context.get("permission_policy"),
                    session_id=context.get("session_id"),
                    context_sequence=int(context.get("context_sequence") or 0),
                    artifact_refs=prior.artifact_refs,
                    transcript_delta=prior.transcript_delta or [],
                    prior_verification_trace=prior.verification_trace,
                    prior_completion_verification_attempts=prior.completion_verification_attempts,
                    prior_acceptance_report=prior.acceptance_report,
                )
            else:
                outcome = await runtime.resume_after_approval(
                    prior,
                    runtime_context=context,
                )
            outcome.runtime_binding = {
                **dict(context["runtime_binding"]),
                **runtime.tool_registry.runtime_state(),
            }
            self._persist_outcome(
                run_id,
                outcome,
                context["background_store"].delivered_terminal_ids(),
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._persist_failure(run_id, exc)

    async def _resume_delegated_child(self, run_id: str) -> None:
        """Let the original coordinator model consume a terminal child result."""

        try:
            if self._stop_requested(run_id):
                return
            with database_module.SessionLocal() as db:
                snapshot = db.scalar(
                    select(RunEvent)
                    .where(RunEvent.run_id == run_id, RunEvent.event_type == "runtime_snapshot")
                    .order_by(RunEvent.created_at.desc(), RunEvent.id.desc())
                )
                if snapshot is None:
                    raise RuntimeError("delegated child continuation is missing the parent snapshot")
                prior = self._outcome_from_snapshot(snapshot.payload)
            if not prior.runtime_binding:
                raise RuntimeError("delegated child continuation is missing the frozen parent binding")
            runtime, context = self._resolve_runtime(
                run_id,
                runtime_binding=prior.runtime_binding,
                coordinator_instance=self,
            )
            await attach_mcp_tools(
                runtime.tool_registry,
                session_key=str(context.get("session_id") or run_id),
                workspace_root=str(context["workspace_root"]),
                frozen_tools=context["runtime_binding"].get("mcp_tools"),
                progress_sink=self._event_sink(run_id),
            )
            self._install_completion_verifier(runtime, context)
            outcome = await runtime.resume_after_delegated_child(
                prior,
                runtime_context=context,
            )
            outcome.runtime_binding = {
                **dict(context["runtime_binding"]),
                **runtime.tool_registry.runtime_state(),
            }
            self._persist_outcome(
                run_id,
                outcome,
                context["background_store"].delivered_terminal_ids(),
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._persist_failure(run_id, exc)

    @staticmethod
    def _install_completion_verifier(runtime: AgentRuntime, context: Mapping[str, Any]) -> None:
        """Attach the main-agent-only completion gate to a resolved runtime."""

        binding = context.get("runtime_binding")
        background_store = context.get("background_store")
        runtime.config.max_completion_verification_attempts = max(
            1,
            int(settings.completion_verification_max_attempts or 1),
        )
        if isinstance(binding, Mapping) and binding.get("delegation_version"):
            # Child agents never produce the user-facing final answer. Their
            # settled task observation is part of the parent's gated trace.
            if background_store is None:
                runtime.completion_verifier = None
            else:
                def verify_child_background(_candidate: Mapping[str, Any]) -> CompletionDecision:
                    active = background_store.active_jobs()
                    if active:
                        registered = background_store.register_waiter()
                        if registered:
                            return CompletionDecision(
                                False,
                                "后台作业仍在运行；本次执行将暂停，并在终态事件到达后自动恢复。",
                                {"background_jobs": [
                                    {"id": item["id"], "status": item["status"]} for item in active
                                    if item["id"] in registered
                                ]},
                                defer_until_event=True,
                            )
                    terminal_results = background_store.observe_terminal_results()
                    if terminal_results:
                        return CompletionDecision(
                            False,
                            "后台作业已结束。请根据以下终态事件完成当前子任务：\n"
                            + json.dumps(terminal_results, ensure_ascii=False)[:8_000],
                            {"background_jobs": [
                                {"id": item["id"], "status": item["status"]} for item in terminal_results
                            ]},
                        )
                    return CompletionDecision(True, "子 Agent 后台作业均已结束并读取")

                runtime.completion_verifier = verify_child_background
            return
        if background_store is None:
            runtime.completion_verifier = decide_deterministic_completion
            return

        def verify(candidate: Mapping[str, Any]):
            decision = decide_deterministic_completion(candidate)
            active = background_store.active_jobs() if background_store is not None else []
            if decision.accepted and active:
                registered = background_store.register_waiter()
                if registered:
                    decision.accepted = False
                    decision.defer_until_event = True
                    decision.reason = "后台作业仍在运行；本次执行将暂停，并在终态事件到达后自动恢复。"
                    decision.report = {
                        **dict(decision.report or {}),
                        "background_jobs": [
                            {"id": item["id"], "status": item["status"]} for item in active
                            if item["id"] in registered
                        ],
                    }
                else:
                    active = []
            if decision.accepted and not active and background_store is not None:
                terminal_results = background_store.observe_terminal_results()
                if terminal_results:
                    decision.accepted = False
                    decision.reason = (
                        "后台作业已结束。请根据以下终态事件更新结论，不需要再次轮询：\n"
                        + json.dumps(terminal_results, ensure_ascii=False)[:8_000]
                    )
                    decision.report = {
                        **dict(decision.report or {}),
                        "background_jobs": [
                            {"id": item["id"], "status": item["status"]} for item in terminal_results
                        ],
                    }
            return decision

        runtime.completion_verifier = verify

    @staticmethod
    def _persist_failure(run_id: str, error: BaseException) -> None:
        parent_bridge_event: dict[str, Any] | None = None
        with database_module.SessionLocal() as db:
            claim = db.execute(
                update(Run)
                .where(
                    Run.id == run_id,
                    or_(
                        Run.status != "stopped",
                        Run.stop_reason.is_(None),
                        Run.stop_reason.not_in(USER_INTERRUPT_REASONS),
                    ),
                )
                .values(current_step=Run.current_step)
            )
            if claim.rowcount != 1:
                run = db.get(Run, run_id)
                if run is not None and RunCoordinator._user_interrupt_locked(run):
                    db.add(RunEvent(
                        run_id=run_id,
                        event_type="run_failure_discarded",
                        payload={"reason": USER_INTERRUPT_REASON, "error_type": type(error).__name__},
                    ))
                    db.commit()
                else:
                    db.rollback()
                RunCoordinator._clear_stream_buffer(run_id, interrupted=True)
                coordinator.unregister_tool_canceller(run_id)
                return
            run = db.get(Run, run_id)
            if run is None:
                return
            # Cancellation of a synchronous provider happens at the awaiter;
            # the worker may still raise into this failure path afterwards.
            # Keep the explicit user/parent stop authoritative.
            if run.status == "stopped" and run.stop_reason in {
                USER_INTERRUPT_REASON,
                "parent_user_interrupted",
            }:
                RunCoordinator._clear_stream_buffer(run_id, interrupted=True)
                coordinator.unregister_tool_canceller(run_id)
                return
            raw_error = str(error) or type(error).__name__
            normalized_error_code = classify_exception(error)
            safe_error_message = public_error_message(normalized_error_code)
            run.status = "failed"
            run.error_code = normalized_error_code
            run.error_message = safe_error_message
            run.finished_at = _utcnow()
            transition_run_task(db, run, status="failed")
            db.add(RunEvent(
                run_id=run_id,
                event_type="integration_failed",
                payload={
                    "error_type": type(error).__name__,
                    "error_code": normalized_error_code,
                    "phase": "integration",
                    # Kept in the private event row for diagnosis. The public
                    # event serializer deliberately does not expose this key.
                    "internal_error": raw_error[:20_000],
                },
            ))
            parent_bridge_event = RunCoordinator._sync_delegated_child_state(
                db,
                run,
                status="failed",
                error=safe_error_message,
                error_code=normalized_error_code,
            )
            sync_turn_progress(db, run)
            persist_terminal_response(
                db,
                run,
                error_code=normalized_error_code,
                error_message=safe_error_message,
            )
            db.commit()
            error_message = run.error_message
        run_stream_broker.publish(run_id, {"type": "integration_failed", "error": error_message})
        if parent_bridge_event is not None and parent_bridge_event.get("parent_run_id"):
            run_stream_broker.publish(str(parent_bridge_event["parent_run_id"]), parent_bridge_event)
        coordinator.launch_parent_continuation_if_queued(parent_bridge_event)
        RunCoordinator._clear_stream_buffer(run_id)
        coordinator.unregister_tool_canceller(run_id)


coordinator = RunCoordinator()
