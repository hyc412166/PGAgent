"""Run lifecycle coordinator and restart reconciliation."""
# 文件职责：协调运行从创建、排队、执行、审批暂停、恢复直至终态落库的完整生命周期，并在服务重启后修复遗留状态。
# 逻辑关系：API 路由把运行请求交给 RunCoordinator；协调器通过 runtime_preparer/runtime_factory 构造 AgentRuntime，把流事件写入数据库并供 runs.stream 与会话接口读取。

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import threading
import time
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Mapping

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
from src.persistence.run_events import append_run_event
from src.agent import (
    AgentRuntime,
    CompletionDecision,
    RunOutcome,
    decide_deterministic_completion,
    normalize_usage,
)
from src.context.window import message_tokens
from src.context.assembly import COMPACTION_SCHEMA, CONTINUATION_PREFIX
from src.attachments.contracts import ATTACHMENT_TOOL_NAMES
from src.tools.registry import TOOL_SCHEMAS
from src.tools.types import ToolResult
from src.mcp import mcp_runtime_pool

from src.tasks import background as background_job_service
from src.model.gateway import ModelConfigurationError, ProviderConfig
from src.memory.repository import load_memory_index, record_memory_citations
from src.memory.preferences import memories_enabled
from src.observability import bind_observability_context
from src.context.instructions import load_instruction_chain, render_workspace_rules
from src.runs.stream import run_stream_broker
from src.tasks.state import (
    recovery_prompt,
    todo_state_for_run,
    transition_run_task,
)
from src.tasks.graph import ready_steps, refresh_task_state, settle_step, upsert_delegated_graph
from src.agents.collaboration import teammate_context
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
from .runtime_factory import RunRuntimeFactory
from .continuation import RunContinuationCodec
from .runtime_preparer import RunRuntimePreparer


logger = logging.getLogger(__name__)


# 变量说明：ACTIVE_STATUSES 表示当前流程使用的 ACTIVE_STATUSES 集合。
ACTIVE_STATUSES = {"received", "preparing_context", "planning", "acting", "observing", "verifying", "running"}
# ``received`` is included above because it is a schedulable run that may not
# have reached the runtime yet.  Approval pauses are also user-stoppable; a
# stop request must settle the pending approval instead of leaving a dead
# button in the UI.
# 变量说明：STOPPABLE_STATUSES 表示当前流程使用的 STOPPABLE_STATUSES 集合。
STOPPABLE_STATUSES = ACTIVE_STATUSES | {"awaiting_approval"}
# 变量说明：USER_INTERRUPT_REASON 表示当前步骤使用的 USER_INTERRUPT_REASON 值。
USER_INTERRUPT_REASON = "user_interrupted"
# 变量说明：USER_INTERRUPT_ERROR 表示当前步骤使用的 USER_INTERRUPT_ERROR 值。
USER_INTERRUPT_ERROR = "run_interrupted"
# 变量说明：_CHILD_FORBIDDEN_ORCHESTRATION_TOOLS 表示当前流程使用的 _CHILD_FORBIDDEN_ORCHESTRATION_TOOLS 集合。
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
# 变量说明：USER_INTERRUPT_REASONS 表示当前流程使用的 USER_INTERRUPT_REASONS 集合。
USER_INTERRUPT_REASONS = frozenset({USER_INTERRUPT_REASON, "parent_user_interrupted"})

# ``stream_sink`` is a low-latency UI channel. Provider payloads never pass
# through directly; reasoning is exposed only as the runtime's typed delta.
# 变量说明：_PUBLIC_TRANSIENT_STREAM_EVENT_TYPES 表示当前流程使用的 _PUBLIC_TRANSIENT_STREAM_EVENT_TYPES 集合。
_PUBLIC_TRANSIENT_STREAM_EVENT_TYPES = frozenset({
    "assistant_delta",
    "thought_delta",
    "progress",
    "agent_progress",
    "thought_summary",
    "activity_update",
})

# 变量说明：_DELEGATE_OUTPUT_LIMIT 表示当前步骤使用的 _DELEGATE_OUTPUT_LIMIT 值。
_DELEGATE_OUTPUT_LIMIT = 16_000
# 变量说明：_DELEGATE_MESSAGE_LIMIT 表示当前步骤使用的 _DELEGATE_MESSAGE_LIMIT 值。
_DELEGATE_MESSAGE_LIMIT = 20_000
# 变量说明：_DELEGATE_AGENT_CATALOG_LIMIT 表示当前步骤使用的 _DELEGATE_AGENT_CATALOG_LIMIT 值。
_DELEGATE_AGENT_CATALOG_LIMIT = 40
# 变量说明：_DELEGATE_CHILD_SYSTEM_SUFFIX 表示当前步骤使用的 _DELEGATE_CHILD_SYSTEM_SUFFIX 值。
_DELEGATE_CHILD_SYSTEM_SUFFIX = (
    "\n\n你正在作为受限的子 Agent 执行一项已分配任务。"
    "只完成下方任务并返回可验证的结构化结论；不要再次委派任务，"
    "不要假称调用未启用的工具，也不要把问题直接抛给用户。"
)




# 类职责：定义 RunCoordinator 在本领域中的数据与行为。
class RunCoordinator:
    """Launch, persist and resume local runs without blocking HTTP requests."""

    # Token deltas are intentionally delivered through the in-memory broker;
    # writing one SQLite row per token would turn streaming into a database
    # bottleneck.  We retain a bounded per-run copy here so an explicit user
    # stop can persist the portion that was actually visible at that moment.
    # Completed runs clear the buffer.  The durable event log still contains
    # every thought/tool lifecycle event emitted by ``_event_sink``.
    # 变量说明：_stream_buffer_lock 表示当前步骤使用的 _stream_buffer_lock 值。
    _stream_buffer_lock = threading.RLock()
    # 变量说明：_stream_buffers 表示当前流程使用的 _stream_buffers 集合。
    _stream_buffers: dict[str, dict[str, str]] = {}
    # 变量说明：_interrupt_requested 表示当前步骤使用的 _interrupt_requested 值。
    _interrupt_requested: dict[str, float] = {}
    # 变量说明：_INTERRUPT_MARK_RETENTION_SECONDS 表示当前流程使用的 _INTERRUPT_MARK_RETENTION_SECONDS 集合。
    _INTERRUPT_MARK_RETENTION_SECONDS = 300.0
    # 变量说明：_MAX_PARTIAL_CHARS 表示当前流程使用的 _MAX_PARTIAL_CHARS 集合。
    _MAX_PARTIAL_CHARS = 100_000

    # 函数职责：初始化实例依赖与初始状态。
    # 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
    def __init__(self) -> None:
        # 变量说明：_tasks 表示当前流程使用的 _tasks 集合。
        self._tasks: dict[str, asyncio.Task[None]] = {}
        # 变量说明：_memory_tasks 表示当前流程使用的 _memory_tasks 集合。
        self._memory_tasks: dict[str, asyncio.Task[None]] = {}
        # 变量说明：_tool_cancellers 表示当前流程使用的 _tool_cancellers 集合。
        self._tool_cancellers: dict[str, Callable[[], None]] = {}
        # 变量说明：_queued_resumes 表示当前流程使用的 _queued_resumes 集合。
        self._queued_resumes: set[str] = set()
        # 变量说明：_shutting_down 表示当前步骤使用的 _shutting_down 值。
        self._shutting_down = False
        # 变量说明：_event_loop 表示当前步骤使用的 _event_loop 值。
        self._event_loop: asyncio.AbstractEventLoop | None = None

    @contextmanager
    def _observability_scope(self, run_id: str) -> Iterator[None]:
        """把根会话 trace 绑定到当前 Run，并让委派子 Run 沿用同一条诊断链。"""

        fields: dict[str, str] = {"trace_id": run_id, "run_id": run_id}
        with database_module.SessionLocal() as db:
            run = db.get(Run, run_id)
            if run is not None:
                turn_id = run.turn_id
                delegated = db.scalar(
                    select(DelegatedTask).where(DelegatedTask.child_run_id == run_id)
                )
                if delegated is not None:
                    fields["parent_run_id"] = delegated.parent_run_id
                    fields["child_run_id"] = run_id
                    parent = db.get(Run, delegated.parent_run_id)
                    if turn_id is None and parent is not None:
                        turn_id = parent.turn_id
                if turn_id:
                    fields["turn_id"] = turn_id
                    turn = db.get(ConversationTurn, turn_id)
                    if turn is not None:
                        fields["trace_id"] = turn.trace_id

        with bind_observability_context(**fields):
            yield

    # 函数职责：完成 start 对应的业务处理。
    # 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
    def start(self) -> None:
        """Bind in-process scheduling to the active application event loop."""

        # 变量说明：_shutting_down 表示当前步骤使用的 _shutting_down 值。
        self._shutting_down = False
        # 变量说明：_event_loop 表示当前步骤使用的 _event_loop 值。
        self._event_loop = asyncio.get_running_loop()

    # 函数职责：完成 register_tool_canceller 对应的业务处理。
    # 参数关系：run_id 表示当前运行标识；callback 表示当前步骤使用的 callback 值。
    # 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
    def register_tool_canceller(self, run_id: str, callback: Callable[[], None]) -> None:
        # 变量说明：映射 的索引项 表示该语句创建或更新的目标数据。
        self._tool_cancellers[run_id] = callback

    # 函数职责：完成 unregister_tool_canceller 对应的业务处理。
    # 参数关系：run_id 表示当前运行标识。
    # 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
    def unregister_tool_canceller(self, run_id: str) -> None:
        self._tool_cancellers.pop(run_id, None)

    # 函数职责：完成 close_mcp_session 对应的业务处理。
    # 参数关系：session_id 表示所属会话标识。
    # 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
    def close_mcp_session(self, session_id: str) -> None:
        """Schedule teardown after a durable conversation is deleted."""

        # 变量说明：loop 表示当前步骤使用的 loop 值。
        loop = self._event_loop
        if loop is not None and not loop.is_closed():
            loop.call_soon_threadsafe(
                lambda: asyncio.create_task(mcp_runtime_pool.close_session(session_id))
            )

    # 函数职责：完成 remember_stream_delta 对应的业务处理。
    # 参数关系：run_id 表示当前运行标识；event 表示当前运行事件。
    # 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
    @classmethod
    def _remember_stream_delta(cls, run_id: str, event: Mapping[str, Any]) -> None:
        """Keep bounded visible deltas for a possible user interruption."""

        # 变量说明：event_type 表示当前步骤使用的 event_type 值。
        event_type = str(event.get("type") or "")
        if event_type == "assistant_delta":
            # 变量说明：key 表示用于查找或映射的键。
            key = "output"
            # 变量说明：delta 表示当前步骤使用的 delta 值。
            delta = str(event.get("delta") or "")
        elif event_type == "thought_delta":
            # 变量说明：key 表示用于查找或映射的键。
            key = "thought"
            # 变量说明：delta 表示当前步骤使用的 delta 值。
            delta = str(event.get("delta") or "")
        elif event_type in {"progress", "agent_progress", "thought_summary", "activity_update"}:
            # 变量说明：key 表示用于查找或映射的键。
            key = "thought"
            # 变量说明：delta 表示当前步骤使用的 delta 值。
            delta = str(event.get("summary") or event.get("progress") or event.get("status_text") or event.get("activity") or "")
        else:
            return
        if not delta:
            return
        with cls._stream_buffer_lock:
            # 变量说明：now 表示当前时间。
            now = time.monotonic()
            # 变量说明：stale 表示当前步骤使用的 stale 值。
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
            # 变量说明：buffer 表示当前步骤使用的 buffer 值。
            buffer = cls._stream_buffers.setdefault(run_id, {"output": "", "thought": ""})
            # 变量说明：current 表示当前步骤使用的 current 值。
            current = buffer.get(key, "")
            buffer[key] = (current + delta)[-cls._MAX_PARTIAL_CHARS :]

    # 函数职责：完成 partial_stream 对应的业务处理。
    # 参数关系：run_id 表示当前运行标识。
    # 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
    @classmethod
    def _partial_stream(cls, run_id: str) -> dict[str, str]:
        with cls._stream_buffer_lock:
            return dict(cls._stream_buffers.get(run_id, {"output": "", "thought": ""}))

    # 函数职责：完成 mark_interrupt_and_snapshot 对应的业务处理。
    # 参数关系：run_id 表示当前运行标识。
    # 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
    @classmethod
    def _mark_interrupt_and_snapshot(cls, run_id: str) -> dict[str, str]:
        """Atomically freeze the visible stream before publishing a stop."""

        with cls._stream_buffer_lock:
            # 变量说明：snapshot 表示当前步骤使用的 snapshot 值。
            snapshot = dict(cls._stream_buffers.get(run_id, {"output": "", "thought": ""}))
            # Once the database CAS has claimed the run, no worker-thread
            # token may be published after this point.  The caller commits
            # the durable interruption and clears the buffer afterwards.
            # 变量说明：映射 的索引项 表示该语句创建或更新的目标数据。
            cls._interrupt_requested[run_id] = time.monotonic()
            return snapshot

    # 函数职责：完成 clear_stream_buffer 对应的业务处理。
    # 参数关系：run_id 表示当前运行标识；interrupted 表示当前步骤使用的 interrupted 值。
    # 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
    @classmethod
    def _clear_stream_buffer(cls, run_id: str, *, interrupted: bool = False) -> None:
        with cls._stream_buffer_lock:
            cls._stream_buffers.pop(run_id, None)
            if interrupted:
                # 变量说明：映射 的索引项 表示该语句创建或更新的目标数据。
                cls._interrupt_requested[run_id] = time.monotonic()
            else:
                cls._interrupt_requested.pop(run_id, None)

    # 函数职责：完成 user_interrupt_locked 对应的业务处理。
    # 参数关系：run 表示当前步骤使用的 run 值。
    # 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
    @classmethod
    def _user_interrupt_locked(cls, run: Run) -> bool:
        return run.status == "stopped" and run.stop_reason in USER_INTERRUPT_REASONS

    # 函数职责：完成 stop_requested 对应的业务处理。
    # 参数关系：run_id 表示当前运行标识。
    # 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
    @staticmethod
    def _stop_requested(run_id: str) -> bool:
        """Check the durable stop marker before starting a newly queued task.

        A stop can race the short window between the HTTP launch commit and
        ``coordinator.launch``.  In that case there is no asyncio task to
        cancel yet; the task must still refuse to enter the model/tool loop.
        """

        with database_module.SessionLocal() as db:
            # 变量说明：run 表示当前步骤使用的 run 值。
            run = db.get(Run, run_id)
            return bool(run is not None and RunCoordinator._user_interrupt_locked(run))

    # 函数职责：完成 clear_task 对应的业务处理。
    # 参数关系：completed 表示当前步骤使用的 completed 值；run_id 表示当前运行标识。
    # 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
    def _clear_task(self, completed: asyncio.Task[None], run_id: str) -> None:
        if self._tasks.get(run_id) is completed:
            self._tasks.pop(run_id, None)

    # 函数职责：完成 launch_queued_resume 对应的业务处理。
    # 参数关系：run_id 表示当前运行标识。
    # 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
    def _launch_queued_resume(self, run_id: str) -> None:
        if run_id not in self._queued_resumes:
            return
        self._queued_resumes.discard(run_id)
        if not self._shutting_down:
            self.launch(run_id, resume=True)

    # 函数职责：完成 launch 对应的业务处理。
    # 参数关系：run_id 表示当前运行标识；resume 表示当前步骤使用的 resume 值。
    # 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
    def launch(self, run_id: str, *, resume: bool = False) -> bool:
        if self._shutting_down:
            return False
        # 变量说明：current 表示当前步骤使用的 current 值。
        current = self._tasks.get(run_id)
        if current and not current.done():
            if not resume:
                return False
            if run_id not in self._queued_resumes:
                self._queued_resumes.add(run_id)
                # 变量说明：loop 表示当前步骤使用的 loop 值。
                loop = asyncio.get_running_loop()
                current.add_done_callback(
                    lambda _completed, key=run_id: loop.call_soon(self._launch_queued_resume, key)
                )
            return True
        # 变量说明：task 表示当前步骤使用的 task 值。
        task = asyncio.create_task(self._resume(run_id) if resume else self._execute(run_id), name=f"pgagent-run-{run_id}")
        self._tasks[run_id] = task
        task.add_done_callback(lambda completed, key=run_id: self._clear_task(completed, key))
        return True

    # 函数职责：异步完成 wait_for_run 对应的业务处理。
    # 参数关系：run_id 表示当前运行标识；timeout_seconds 表示当前流程使用的 timeout_seconds 集合。
    # 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
    async def wait_for_run(self, run_id: str, *, timeout_seconds: float) -> bool:
        """Wait for a coordinator-owned Run without transferring cancellation."""

        # 变量说明：task 表示当前步骤使用的 task 值。
        task = self._tasks.get(run_id)
        if task is None:
            with database_module.SessionLocal() as db:
                # 变量说明：status 表示当前对象或运行的状态。
                status = db.scalar(select(Run.status).where(Run.id == run_id))
            return status in {"completed", "failed", "stopped", "awaiting_approval"}
        try:
            await asyncio.wait_for(
                asyncio.shield(task),
                timeout=max(0.0, float(timeout_seconds)),
            )
            return True
        except asyncio.TimeoutError:
            return False

    # 函数职责：完成 schedule_background_continuation 对应的业务处理。
    # 参数关系：run_id 表示当前运行标识；job_id 表示job 对象的唯一标识。
    # 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
    def _schedule_background_continuation(self, run_id: str, job_id: str) -> None:
        # 函数职责：完成 launch 对应的业务处理。
        # 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
        def launch() -> None:
            run_stream_broker.publish(run_id, {
                "type": "background_continuation_started",
                "run_id": run_id,
                "background_job_id": job_id,
            })
            self.launch(run_id, resume=True)

        # 变量说明：loop 表示当前步骤使用的 loop 值。
        loop = self._event_loop
        if loop is not None and loop.is_running():
            loop.call_soon_threadsafe(launch)

    # 函数职责：完成 notify_background_terminal 对应的业务处理。
    # 参数关系：job_id 表示job 对象的唯一标识。
    # 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
    def notify_background_terminal(self, job_id: str) -> bool:
        """Wake a run only after every background job registered to it is terminal."""

        # 变量说明：target_run_id 表示target_run 对象的唯一标识。
        target_run_id: str | None = None
        with database_module.SessionLocal() as db:
            if db.get_bind().dialect.name == "sqlite":
                db.execute(text("BEGIN IMMEDIATE"))
            # 变量说明：job 表示当前步骤使用的 job 值。
            job = db.get(BackgroundJob, job_id)
            if job is None:
                return False
            # 变量说明：target_run_id 表示target_run 对象的唯一标识。
            target_run_id = str(job.waiting_run_id or job.run_id or "") or None
            if target_run_id is None:
                return False
            # 变量说明：active_sibling 表示当前步骤使用的 active_sibling 值。
            active_sibling = db.scalar(select(BackgroundJob.id).where(
                BackgroundJob.waiting_run_id == target_run_id,
                BackgroundJob.status.in_({"queued", "running"}),
            ).limit(1))
            if active_sibling is not None:
                return False
            # 变量说明：run 表示当前步骤使用的 run 值。
            run = db.get(Run, target_run_id)
            if run is None or run.status != "stopped" or run.stop_reason != "waiting_background":
                return False
            # 变量说明：status 表示当前对象或运行的状态。
            run.status = "received"
            # 变量说明：stop_reason 表示当前步骤使用的 stop_reason 值。
            run.stop_reason = None
            # 变量说明：error_code 表示当前步骤使用的 error_code 值。
            run.error_code = None
            # 变量说明：error_message 表示当前步骤使用的 error_message 值。
            run.error_message = None
            # 变量说明：finished_at 表示finished_at 对应的时间信息。
            run.finished_at = None
            if run.task_id:
                # 变量说明：task 表示当前步骤使用的 task 值。
                task = db.get(DurableTask, run.task_id)
                if task is not None and task.status == "waiting":
                    # 变量说明：status 表示当前对象或运行的状态。
                    task.status = "running"
                    # 变量说明：resume_summary 表示当前步骤使用的 resume_summary 值。
                    task.resume_summary = "A background terminal event arrived; resume and consume its result."
            append_run_event(
                db,
                run_id=run.id,
                event_type="background_continuation_queued",
                payload={"background_job_id": job.id},
            )
            db.commit()
        self._schedule_background_continuation(target_run_id, job_id)
        return True

    # 函数职责：完成 reconcile_waiting_background_runs 对应的业务处理。
    # 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
    def reconcile_waiting_background_runs(self) -> list[str]:
        """Recover event/run commit races without invoking the model on a timer."""

        with database_module.SessionLocal() as db:
            # 变量说明：waiting 表示当前步骤使用的 waiting 值。
            waiting = list(db.scalars(select(Run.id).where(
                Run.status == "stopped",
                Run.stop_reason == "waiting_background",
            )))
            # 变量说明：terminal_job_by_run 表示当前步骤使用的 terminal_job_by_run 值。
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
        # 变量说明：resumed 表示当前步骤使用的 resumed 值。
        resumed: list[str] = []
        for run_id, job_id in terminal_job_by_run.items():
            if self.notify_background_terminal(job_id):
                resumed.append(run_id)
        return resumed

    # 函数职责：完成 launch_memory_job 对应的业务处理。
    # 参数关系：job_id 表示job 对象的唯一标识。
    # 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
    def launch_memory_job(self, job_id: str) -> bool:
        """Schedule durable auxiliary extraction without blocking reply delivery."""

        if self._shutting_down:
            return False
        with database_module.SessionLocal() as db:
            if not memories_enabled(db):
                return False
        # 变量说明：current 表示当前步骤使用的 current 值。
        current = self._memory_tasks.get(job_id)
        if current is not None and not current.done():
            return False
        try:
            # 变量说明：loop 表示当前步骤使用的 loop 值。
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return False
        # 变量说明：task 表示当前步骤使用的 task 值。
        task = loop.create_task(self._process_memory_job(job_id), name=f"pgagent-memory-{job_id}")
        self._memory_tasks[job_id] = task
        task.add_done_callback(
            lambda completed, key=job_id: self._memory_tasks.pop(key, None)
            if self._memory_tasks.get(key) is completed else None
        )
        return True

    # 函数职责：完成 pending_memory_job_ids 对应的业务处理。
    # 参数关系：recover_running 表示当前步骤使用的 recover_running 值。
    # 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
    @staticmethod
    def pending_memory_job_ids(*, recover_running: bool = True) -> list[str]:
        """Reset process-interrupted workers and return retryable durable jobs."""

        with database_module.SessionLocal() as db:
            if not memories_enabled(db):
                return []
            if recover_running:
                # 变量说明：now 表示当前时间。
                now = _utcnow()
                for job in db.scalars(select(MemoryJob).where(
                    MemoryJob.status.in_({"running", "committing"}),
                    or_(MemoryJob.lease_expires_at.is_(None), MemoryJob.lease_expires_at <= now),
                )):
                    # 变量说明：status 表示当前对象或运行的状态。
                    job.status = "pending" if int(job.attempts or 0) < 3 else "failed"
                    # 变量说明：error 表示当前捕获或准备上报的错误。
                    job.error = "worker process restarted before completion"
                    # 变量说明：lease_expires_at 表示lease_expires_at 对应的时间信息。
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

    # 函数职责：异步完成 process_memory_job 对应的业务处理。
    # 参数关系：job_id 表示job 对象的唯一标识。
    # 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
    async def _process_memory_job(self, job_id: str) -> None:
        from src.memory.pipeline import memory_pipeline

        await memory_pipeline.process(job_id)
    # 函数职责：完成 stop 对应的业务处理。
    # 参数关系：run_id 表示当前运行标识；db 表示当前数据库会话；reason 表示当前步骤使用的 reason 值。
    # 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
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

        # 变量说明：owns_db 表示当前步骤使用的 owns_db 值。
        owns_db = db is None
        # 变量说明：session 表示当前步骤使用的 session 值。
        session = db or database_module.SessionLocal()
        # 变量说明：changed 表示当前步骤使用的 changed 值。
        changed = False
        # 变量说明：parent_bridge_events 表示当前流程使用的 parent_bridge_events 集合。
        parent_bridge_events: list[dict[str, Any]] = []
        # 变量说明：child_run_ids_to_cancel 表示当前步骤使用的 child_run_ids_to_cancel 值。
        child_run_ids_to_cancel: list[str] = []
        # 变量说明：delegated_parent_id 表示delegated_parent 对象的唯一标识。
        delegated_parent_id: str | None = None
        # 变量说明：partial 表示当前步骤使用的 partial 值。
        partial = {"output": "", "thought": ""}
        # 变量说明：stream_marked 表示当前步骤使用的 stream_marked 值。
        stream_marked = False
        try:
            # 变量说明：run 表示当前步骤使用的 run 值。
            run = session.get(Run, run_id)
            if run is None:
                return None
            # 变量说明：prior_status 表示当前流程使用的 prior_status 集合。
            prior_status = str(run.status or "")
            # 变量说明：prior_stop_reason 表示当前步骤使用的 prior_stop_reason 值。
            prior_stop_reason = str(run.stop_reason or "")
            # 变量说明：delegated_parent_id 表示delegated_parent 对象的唯一标识。
            delegated_parent_id = session.scalar(
                select(DelegatedTask.parent_run_id)
                .where(DelegatedTask.child_run_id == run_id)
            )
            # 变量说明：can_stop 表示表示是否满足 _stop 条件的布尔标记。
            can_stop = prior_status in STOPPABLE_STATUSES or (
                prior_status == "stopped"
                and not is_terminal_delivery(prior_status, prior_stop_reason)
            )
            if can_stop:
                # 变量说明：now 表示当前时间。
                now = _utcnow()
                # 变量说明：reason_guard 表示当前步骤使用的 reason_guard 值。
                reason_guard = Run.stop_reason.is_(None) if not prior_stop_reason else Run.stop_reason == prior_stop_reason
                # 变量说明：claimed 表示当前步骤使用的 claimed 值。
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
                    # 变量说明：run 表示当前步骤使用的 run 值。
                    run = session.get(Run, run_id)
                    return run
                else:
                    session.refresh(run)
                    # 变量说明：status 表示当前对象或运行的状态。
                    run.status = "stopped"
                    # 变量说明：stop_reason 表示当前步骤使用的 stop_reason 值。
                    run.stop_reason = reason
                    # 变量说明：error_code 表示当前步骤使用的 error_code 值。
                    run.error_code = USER_INTERRUPT_ERROR
                    # 变量说明：error_message 表示当前步骤使用的 error_message 值。
                    run.error_message = "任务已按你的要求停止。"
                    # 变量说明：finished_at 表示finished_at 对应的时间信息。
                    run.finished_at = now
                # Freeze the stream only after the lifecycle CAS succeeds.
                # Taking the snapshot under the same lock prevents a late
                # worker-thread delta from appearing in the UI but missing
                # from the persisted interruption evidence.
                # 变量说明：partial 表示当前步骤使用的 partial 值。
                partial = RunCoordinator._mark_interrupt_and_snapshot(run_id)
                # 变量说明：stream_marked 表示当前步骤使用的 stream_marked 值。
                stream_marked = True
                # 变量说明：interrupted_payload 表示当前步骤使用的 interrupted_payload 值。
                interrupted_payload = {
                    "reason": reason,
                    "previous_status": prior_status,
                    "partial_output": partial.get("output") or None,
                    "partial_thought": partial.get("thought") or None,
                    "partial_output_chars": len(partial.get("output") or ""),
                    "partial_thought_chars": len(partial.get("thought") or ""),
                }
                append_run_event(
                    session,
                    run_id=run_id,
                    event_type="run_interrupted",
                    payload=interrupted_payload,
                )
                append_run_event(
                    session,
                    run_id=run_id,
                    event_type="run_stopped",
                    payload={"code": reason, **interrupted_payload},
                )
                # A stop while waiting for a risky tool must invalidate the
                # approval record.  Otherwise a stale browser click could
                # later resume a run that the user explicitly cancelled.
                # 变量说明：pending_approvals 表示当前流程使用的 pending_approvals 集合。
                pending_approvals = list(session.scalars(
                    select(Approval)
                    .where(Approval.run_id == run_id, Approval.status == "pending")
                ))
                for approval in pending_approvals:
                    # 变量说明：status 表示当前对象或运行的状态。
                    approval.status = "superseded"
                    # 变量说明：reason 表示当前步骤使用的 reason 值。
                    approval.reason = "Approval cancelled because the run was interrupted."
                    # 变量说明：decided_at 表示decided_at 对应的时间信息。
                    approval.decided_at = now
                    append_run_event(
                        session,
                        run_id=run_id,
                        event_type="approval_cancelled",
                        payload={"approval_id": approval.id, "reason": reason},
                    )

                # If the parent is stopped, settle any child runs that are
                # still active.  This preserves the current DelegatedTask
                # side-panel history and prevents an orphan child from
                # reporting a completion after its parent was cancelled.
                # 变量说明：child_tasks 表示当前流程使用的 child_tasks 集合。
                child_tasks = list(session.scalars(
                    select(DelegatedTask).where(
                        DelegatedTask.parent_run_id == run_id,
                        DelegatedTask.status == "in_progress",
                    )
                ))
                for child_task in child_tasks:
                    # 变量说明：child_run 表示当前步骤使用的 child_run 值。
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
                    # 变量说明：status 表示当前对象或运行的状态。
                    child_run.status = "stopped"
                    # 变量说明：stop_reason 表示当前步骤使用的 stop_reason 值。
                    child_run.stop_reason = "parent_user_interrupted"
                    # 变量说明：error_code 表示当前步骤使用的 error_code 值。
                    child_run.error_code = USER_INTERRUPT_ERROR
                    # 变量说明：error_message 表示当前步骤使用的 error_message 值。
                    child_run.error_message = "主任务已按用户要求停止。"
                    # 变量说明：finished_at 表示finished_at 对应的时间信息。
                    child_run.finished_at = now
                    # 变量说明：child_approvals 表示当前流程使用的 child_approvals 集合。
                    child_approvals = list(session.scalars(
                        select(Approval)
                        .where(Approval.run_id == child_run.id, Approval.status == "pending")
                    ))
                    for approval in child_approvals:
                        # 变量说明：status 表示当前对象或运行的状态。
                        approval.status = "superseded"
                        # 变量说明：reason 表示当前步骤使用的 reason 值。
                        approval.reason = "Approval cancelled because the parent run was interrupted."
                        # 变量说明：decided_at 表示decided_at 对应的时间信息。
                        approval.decided_at = now
                        append_run_event(
                            session,
                            run_id=child_run.id,
                            event_type="approval_cancelled",
                            payload={"approval_id": approval.id, "reason": "parent_user_interrupted"},
                        )
                    append_run_event(
                        session,
                        run_id=child_run.id,
                        event_type="run_interrupted",
                        payload={
                            "reason": "parent_user_interrupted",
                            "parent_run_id": run_id,
                        },
                    )
                    # 变量说明：bridge 表示当前步骤使用的 bridge 值。
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
                        # 变量说明：status 表示当前对象或运行的状态。
                        child_task.status = "blocked"
                        # 变量说明：child_result 表示当前步骤使用的 child_result 值。
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
                        # 变量说明：result 表示本步骤产生的结果。
                        child_task.result = child_result
                        # 变量说明：bridge 表示当前步骤使用的 bridge 值。
                        bridge = {
                            "type": "delegated_child_stopped",
                            "parent_run_id": run_id,
                            "task_id": child_task.id,
                            "delegation_id": child_task.id,
                            "child_run_id": child_run.id,
                            "status": "stopped",
                            "stop_reason": "parent_user_interrupted",
                        }
                        append_run_event(
                            session,
                            run_id=run_id,
                            event_type="delegated_child_stopped",
                            payload=bridge,
                        )
                        parent_bridge_events.append(bridge)

                # A direct stop request may target a delegated child from
                # the side panel.  The child runs inside the parent
                # coordinator task, so settle its DelegatedTask before the
                # recursive parent stop below.  Otherwise the parent stop
                # sees a child Run that is already terminal and skips the
                # in-progress delegation row, leaving the side panel stuck.
                if delegated_parent_id and reason in USER_INTERRUPT_REASONS:
                    # 变量说明：direct_child_task 表示当前步骤使用的 direct_child_task 值。
                    direct_child_task = session.scalar(
                        select(DelegatedTask)
                        .where(DelegatedTask.child_run_id == run_id)
                        .limit(1)
                    )
                    if direct_child_task is not None and direct_child_task.status == "in_progress":
                        # 变量说明：bridge 表示当前步骤使用的 bridge 值。
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
                            # 变量说明：status 表示当前对象或运行的状态。
                            direct_child_task.status = "blocked"
                            # 变量说明：direct_result 表示当前步骤使用的 direct_result 值。
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
                            # 变量说明：result 表示本步骤产生的结果。
                            direct_child_task.result = direct_result
                            # 变量说明：direct_bridge 表示当前步骤使用的 direct_bridge 值。
                            direct_bridge = {
                                "type": "delegated_child_stopped",
                                "parent_run_id": delegated_parent_id,
                                "task_id": direct_child_task.id,
                                "delegation_id": direct_child_task.id,
                                "child_run_id": run_id,
                                "status": "stopped",
                                "stop_reason": reason,
                            }
                            append_run_event(
                                session,
                                run_id=delegated_parent_id,
                                event_type="delegated_child_stopped",
                                payload=direct_bridge,
                            )
                            parent_bridge_events.append(direct_bridge)

                # Store a compact snapshot alongside the interruption event so
                # history readers can render the partial response without
                # mistaking it for a completed ChatMessage transcript.
                append_run_event(
                    session,
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
                )
                transition_run_task(session, run, status="stopped", stop_reason=reason)
                sync_turn_progress(session, run)
                persist_terminal_response(
                    session,
                    run,
                    error_code=reason,
                    error_message="任务已按你的要求停止。",
                )
                session.commit()
                # 变量说明：changed 表示当前步骤使用的 changed 值。
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
            # 变量说明：terminal_event 表示当前步骤使用的 terminal_event 值。
            terminal_event = {
                "type": "run_stopped",
                "code": reason,
                "reason": "任务已按你的要求停止。",
                "partial_output": partial.get("output") or None,
                "partial_thought": partial.get("thought") or None,
            }
            run_stream_broker.publish(run_id, terminal_event)
            for bridge in parent_bridge_events:
                # 变量说明：parent_run_id 表示parent_run 对象的唯一标识。
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
        # 变量说明：target_run_ids 表示target_run 对象标识集合。
        target_run_ids = list(dict.fromkeys([run_id, *child_run_ids_to_cancel]))
        background_job_service.background_job_manager.cancel_for_runs(target_run_ids)
        try:
            # 变量说明：current 表示当前步骤使用的 current 值。
            current = asyncio.current_task()
        except RuntimeError:
            # 变量说明：current 表示当前步骤使用的 current 值。
            current = None
        for target_run_id in target_run_ids:
            # 变量说明：canceller 表示当前步骤使用的 canceller 值。
            canceller = self._tool_cancellers.get(target_run_id)
            if canceller is not None:
                try:
                    canceller()
                except Exception:
                    # Cancellation is best effort for a custom registry; the
                    # durable stop transition and task cancellation still apply.
                    pass
                self.unregister_tool_canceller(target_run_id)
            # 变量说明：task 表示当前步骤使用的 task 值。
            task = self._tasks.get(target_run_id)
            if task is not None and not task.done() and task is not current:
                task.cancel()

        return run

    # 函数职责：完成 launch_delegated_child_continuation 对应的业务处理。
    # 参数关系：run_id 表示当前运行标识。
    # 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
    def launch_delegated_child_continuation(self, run_id: str) -> bool:
        """Schedule the parent continuation created by a terminal child.

        The parent row has already moved from its explicit child-wait stop to
        ``received`` in the child outcome transaction.  This method only owns
        in-process scheduling and never changes persistence, so callers can
        safely retry it without creating a second continuation task.
        """

        if self._shutting_down:
            return False
        # 变量说明：current 表示当前步骤使用的 current 值。
        current = self._tasks.get(run_id)
        if current is not None and not current.done():
            return False
        try:
            # 变量说明：loop 表示当前步骤使用的 loop 值。
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # A synchronous maintenance caller can still leave durable state
            # for an explicit resume; do not create an orphan coroutine.
            return False
        # 变量说明：task 表示当前步骤使用的 task 值。
        task = loop.create_task(
            self._resume_delegated_child(run_id),
            name=f"pgagent-child-continuation-{run_id}",
        )
        # 变量说明：映射 的索引项 表示该语句创建或更新的目标数据。
        self._tasks[run_id] = task
        task.add_done_callback(lambda completed, key=run_id: self._clear_task(completed, key))
        return True

    # 函数职责：完成 launch_parent_continuation_if_queued 对应的业务处理。
    # 参数关系：parent_event 表示当前步骤使用的 parent_event 值。
    # 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
    def launch_parent_continuation_if_queued(self, parent_event: dict[str, Any] | None) -> bool:
        """Start a durable queued parent continuation exactly once per event."""

        if not parent_event or not parent_event.get("continuation_queued"):
            return False
        # 变量说明：parent_run_id 表示parent_run 对象的唯一标识。
        parent_run_id = str(parent_event.get("parent_run_id") or "").strip()
        return bool(parent_run_id) and self.launch_delegated_child_continuation(parent_run_id)

    # 函数职责：异步完成 shutdown 对应的业务处理。
    # 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
    async def shutdown(self) -> None:
        # 变量说明：_shutting_down 表示当前步骤使用的 _shutting_down 值。
        self._shutting_down = True
        # 变量说明：_event_loop 表示当前步骤使用的 _event_loop 值。
        self._event_loop = None
        self._queued_resumes.clear()
        # 变量说明：had_memory_tasks 表示当前流程使用的 had_memory_tasks 集合。
        had_memory_tasks = any(not task.done() for task in self._memory_tasks.values())
        # 变量说明：tasks 表示当前流程使用的 tasks 集合。
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
                    # 变量说明：status 表示当前对象或运行的状态。
                    job.status = "pending" if int(job.attempts or 0) < 3 else "failed"
                    # 变量说明：error 表示当前捕获或准备上报的错误。
                    job.error = "worker cancelled during graceful shutdown"
                    # 变量说明：lease_expires_at 表示lease_expires_at 对应的时间信息。
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

    # 函数职责：完成 reconcile_interrupted_runs 对应的业务处理。
    # 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
    @staticmethod
    def reconcile_interrupted_runs() -> list[str]:
        """Make crashes visible instead of leaving runs permanently active."""

        # 变量说明：parent_bridge_events 表示当前流程使用的 parent_bridge_events 集合。
        parent_bridge_events: list[dict[str, Any]] = []
        # 变量说明：parent_continuation_run_ids 表示parent_continuation_run 对象标识集合。
        parent_continuation_run_ids: list[str] = []
        with database_module.SessionLocal() as db:
            # 变量说明：runs 表示当前流程使用的 runs 集合。
            runs = list(db.scalars(select(Run).where(Run.status.in_(ACTIVE_STATUSES))))
            for run in runs:
                # 变量说明：status 表示当前对象或运行的状态。
                run.status = "stopped"
                # 变量说明：stop_reason 表示当前步骤使用的 stop_reason 值。
                run.stop_reason = "interrupted_restart"
                # 变量说明：error_code 表示当前步骤使用的 error_code 值。
                run.error_code = "interrupted_restart"
                # 变量说明：error_message 表示当前步骤使用的 error_message 值。
                run.error_message = "PGAgent 在运行期间被关闭，可重新发送消息继续任务"
                # 变量说明：finished_at 表示finished_at 对应的时间信息。
                run.finished_at = _utcnow()
                append_run_event(db, run_id=run.id, event_type="run_interrupted", payload={"reason": "process_restart"})
                transition_run_task(db, run, status="stopped", stop_reason="interrupted_restart")
                # 变量说明：parent_event 表示当前步骤使用的 parent_event 值。
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
                        # 变量说明：parent_run_id 表示parent_run 对象的唯一标识。
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
            # 变量说明：parent_run_id 表示parent_run 对象的唯一标识。
            parent_run_id = str(parent_event.get("parent_run_id") or "")
            if parent_run_id:
                run_stream_broker.publish(parent_run_id, parent_event)
        return list(dict.fromkeys(parent_continuation_run_ids))

    # 函数职责：完成 reconcile_terminal_deliveries 对应的业务处理。
    # 参数关系：include_legacy 表示当前步骤使用的 include_legacy 值。
    # 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
    @staticmethod
    def reconcile_terminal_deliveries(*, include_legacy: bool = True) -> int:
        """Repair terminal root runs that do not yet have a durable reply."""

        # 变量说明：repaired 表示当前步骤使用的 repaired 值。
        repaired = 0
        with database_module.SessionLocal() as db:
            # 变量说明：query 表示当前步骤使用的 query 值。
            query = select(Run).where(Run.status.in_({"completed", "failed", "stopped"}))
            if not include_legacy:
                # 变量说明：query 表示当前步骤使用的 query 值。
                query = query.join(ConversationTurn, Run.turn_id == ConversationTurn.id).where(
                    ConversationTurn.reply_status != "delivered"
                )
            # 变量说明：runs 表示当前流程使用的 runs 集合。
            runs = list(db.scalars(query.order_by(Run.started_at.desc(), Run.id.desc())))
            for run in runs:
                if not is_terminal_delivery(str(run.status or ""), run.stop_reason):
                    continue
                # 变量说明：turn 表示当前步骤使用的 turn 值。
                turn = ensure_run_turn(db, run)
                if turn is None or turn.reply_status == "delivered":
                    continue
                # 变量说明：snapshot 表示当前步骤使用的 snapshot 值。
                snapshot = db.scalar(
                    select(RunEvent)
                    .where(RunEvent.run_id == run.id, RunEvent.event_type == "runtime_snapshot")
                    .order_by(RunEvent.sequence.desc(), RunEvent.id.desc())
                )
                # 变量说明：snapshot_payload 表示当前步骤使用的 snapshot_payload 值。
                snapshot_payload = snapshot.payload if snapshot is not None and isinstance(snapshot.payload, dict) else {}
                # 变量说明：output 表示当前步骤使用的 output 值。
                output = snapshot_payload.get("output")
                if run.status == "completed" and not str(output or "").strip():
                    # 变量说明：status 表示当前对象或运行的状态。
                    run.status = "failed"
                    # 变量说明：error_code 表示当前步骤使用的 error_code 值。
                    run.error_code = "empty_model_output"
                    # 变量说明：error_message 表示当前步骤使用的 error_message 值。
                    run.error_message = public_error_message(run.error_code)
                    # 变量说明：finished_at 表示finished_at 对应的时间信息。
                    run.finished_at = run.finished_at or _utcnow()
                # 变量说明：safe_output 表示当前步骤使用的 safe_output 值。
                safe_output = str(output or "").strip() if (
                    run.status == "completed" or run.stop_reason == "needs_user_input"
                ) else ""
                # 变量说明：message 表示当前消息。
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

    # 函数职责：完成 reconcile_orphaned_runs 对应的业务处理。
    # 参数关系：grace_seconds 表示当前流程使用的 grace_seconds 集合。
    # 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
    def reconcile_orphaned_runs(self, *, grace_seconds: float = 30.0) -> list[str]:
        """Settle accepted root runs whose coordinator task disappeared."""

        # 变量说明：now 表示当前时间。
        now = _utcnow()
        # 变量说明：settled 表示当前步骤使用的 settled 值。
        settled: list[str] = []
        with database_module.SessionLocal() as db:
            # 变量说明：runs 表示当前流程使用的 runs 集合。
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
                    # 变量说明：age 表示当前步骤使用的 age 值。
                    age = grace_seconds
                else:
                    # 变量说明：comparison_now 表示当前步骤使用的 comparison_now 值。
                    comparison_now = now if run.started_at.tzinfo is not None else now.replace(tzinfo=None)
                    # 变量说明：age 表示当前步骤使用的 age 值。
                    age = max(0.0, (comparison_now - run.started_at).total_seconds())
                if age < grace_seconds:
                    continue
                # 变量说明：turn 表示当前步骤使用的 turn 值。
                turn = ensure_run_turn(db, run)
                if turn is None:
                    continue
                # 变量说明：status 表示当前对象或运行的状态。
                run.status = "failed"
                # 变量说明：stop_reason 表示当前步骤使用的 stop_reason 值。
                run.stop_reason = None
                # 变量说明：error_code 表示当前步骤使用的 error_code 值。
                run.error_code = "coordinator_orphaned"
                # 变量说明：error_message 表示当前步骤使用的 error_message 值。
                run.error_message = public_error_message(run.error_code)
                # 变量说明：finished_at 表示finished_at 对应的时间信息。
                run.finished_at = now
                append_run_event(
                    db,
                    run_id=run.id,
                    event_type="integration_failed",
                    payload={"error_type": "coordinator_orphaned", "phase": "scheduling"},
                )
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

    # 函数职责：完成 event_sink 对应的业务处理。
    # 参数关系：run_id 表示当前运行标识。
    # 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
    @staticmethod
    def _event_sink(run_id: str):
        # 变量说明：status_by_event 表示当前步骤使用的 status_by_event 值。
        status_by_event = {
            "context_prepared": "preparing_context",
            "context_resumed": "acting",
            "model_step_started": "acting",
            "tool_finished": "observing",
            "completion_verification_started": "verifying",
            "completion_verification_rejected": "acting",
        }
        # 变量说明：deferred_stream_events 表示当前流程使用的 deferred_stream_events 集合。
        deferred_stream_events = {"approval_requested", "run_completed", "run_stopped", "model_failed"}

        # 函数职责：完成 sink 对应的业务处理。
        # 参数关系：event 表示当前运行事件。
        # 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
        def sink(event: dict[str, Any]) -> None:
            # 变量说明：event_type 表示当前步骤使用的 event_type 值。
            event_type = str(event.get("type") or "runtime_event")
            # 变量说明：payload 表示跨层传递的数据载荷。
            payload = {key: _json_safe(value) for key, value in event.items() if key != "type"}
            with database_module.SessionLocal() as db:
                # Claim the row with a conditional no-op update before
                # appending the event.  If a user stop already committed, a
                # late ``to_thread`` sink cannot mutate status or append a
                # post-stop lifecycle event.
                # 变量说明：claim 表示当前步骤使用的 claim 值。
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
                # 变量说明：run 表示当前步骤使用的 run 值。
                run = db.get(Run, run_id)
                if run is None:
                    db.rollback()
                    return
                append_run_event(db, run_id=run_id, event_type=event_type, step=payload.get("step"), payload=payload)
                if event_type in status_by_event:
                    # 变量说明：status 表示当前对象或运行的状态。
                    run.status = status_by_event[event_type]
                if run.turn_id:
                    sync_turn_progress(db, run)
                db.commit()
            # Approval and terminal events are published only after the outcome
            # transaction makes their corresponding rows/messages visible.
            if event_type not in deferred_stream_events:
                run_stream_broker.publish(run_id, {"type": event_type, **payload})

        return sink

    # 函数职责：流式传输 sink 对应的数据或流程。
    # 参数关系：run_id 表示当前运行标识。
    # 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
    @staticmethod
    def _stream_sink(run_id: str):
        # 函数职责：完成 sink 对应的业务处理。
        # 参数关系：event 表示当前运行事件。
        # 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
        def sink(event: dict[str, Any]) -> None:
            # 变量说明：safe_event 表示当前步骤使用的 safe_event 值。
            safe_event = _json_safe(event)
            if str(safe_event.get("type") or "") not in _PUBLIC_TRANSIENT_STREAM_EVENT_TYPES:
                return
            # Keep the low-latency stream transient, but retain a bounded copy
            # in memory for an explicit stop response.  This avoids one DB
            # transaction per token while still letting the stop endpoint
            # persist exactly what the user saw at cancellation time.
            RunCoordinator._remember_stream_delta(run_id, safe_event)
            with RunCoordinator._stream_buffer_lock:
                # 变量说明：interrupted 表示当前步骤使用的 interrupted 值。
                interrupted = run_id in RunCoordinator._interrupt_requested
            if interrupted:
                return
            run_stream_broker.publish(run_id, safe_event)

        return sink

    # 函数职责：完成 runtime_snapshot 对应的业务处理。
    # 参数关系：outcome 表示当前步骤使用的 outcome 值。
    # 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
    @staticmethod
    def _runtime_snapshot(outcome: RunOutcome) -> dict[str, Any]:
        return RunContinuationCodec.snapshot(outcome)

    # 函数职责：完成 outcome_from_snapshot 对应的业务处理。
    # 参数关系：payload 表示跨层传递的数据载荷。
    # 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
    @staticmethod
    def _outcome_from_snapshot(payload: dict[str, Any]) -> RunOutcome:
        return RunContinuationCodec.restore(payload)

    # 函数职责：解析 runtime 对应的数据或流程。
    # 参数关系：run_id 表示当前运行标识；runtime_binding 表示当前步骤使用的 runtime_binding 值；coordinator_instance 表示当前步骤使用的 coordinator_instance 值。
    # 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
    @staticmethod
    def _resolve_runtime(
        run_id: str,
        *,
        runtime_binding: dict[str, Any] | None = None,
        coordinator_instance: RunCoordinator | None = None,
    ) -> tuple[AgentRuntime, dict[str, Any]]:
        # 变量说明：owner 表示当前步骤使用的 owner 值。
        owner = coordinator_instance or coordinator
        with database_module.SessionLocal() as db:
            # 变量说明：run 表示当前步骤使用的 run 值。
            run = db.get(Run, run_id)
            if run is None:
                raise LookupError("运行不存在")
            # 变量说明：session 表示当前步骤使用的 session 值。
            session = db.get(Session, run.session_id) if run.session_id else None
            # 变量说明：frozen_binding 表示当前步骤使用的 frozen_binding 值。
            frozen_binding = dict(runtime_binding or {})
            # 变量说明：delegated_child 表示当前步骤使用的 delegated_child 值。
            delegated_child = bool(frozen_binding.get("delegation_version"))
            # 变量说明：runtime_max_run_seconds 表示当前流程使用的 runtime_max_run_seconds 集合。
            runtime_max_run_seconds: float | None = settings.max_run_seconds
            # Session runs always use the fixed PGAgent coordinator. Keep
            # standalone runs backwards compatible with their explicit Agent.
            # A delegated child is intentionally linked to its parent session
            # for approval visibility, but its frozen binding remains the
            # authority for agent identity on approval resume.
            # 变量说明：agent_id 表示智能体标识。
            agent_id = (
                str(frozen_binding.get("agent_id") or run.agent_id or DEFAULT_AGENT_ID)
                if delegated_child
                else (DEFAULT_AGENT_ID if session is not None else (run.agent_id or DEFAULT_AGENT_ID))
            )
            # 变量说明：agent 表示当前步骤使用的 agent 值。
            agent = db.get(Agent, agent_id) or db.get(Agent, DEFAULT_AGENT_ID)
            if agent is None:
                raise ModelConfigurationError("当前会话没有选择 Agent")
            if session is not None and not delegated_child:
                # 变量说明：agent_id 表示智能体标识。
                session.agent_id = DEFAULT_AGENT_ID
                # 变量说明：agent_id 表示智能体标识。
                run.agent_id = DEFAULT_AGENT_ID
            # 变量说明：workspace_id 表示工作区标识。
            workspace_id = (
                run.workspace_id
                or (session.workspace_id if session else None)
                or agent.workspace_id
                or DEFAULT_WORKSPACE_ID
            )
            # 变量说明：workspace 表示当前步骤使用的 workspace 值。
            workspace = db.get(Workspace, workspace_id) or db.get(Workspace, DEFAULT_WORKSPACE_ID)
            if workspace is None:
                raise ModelConfigurationError("当前 Agent 没有可用工作区")

            # 变量说明：messages 表示发送给模型或客户端的消息序列。
            messages = _prepare_session_history(db, session) if session else []
            # 变量说明：compaction_state 表示当前步骤使用的 compaction_state 值。
            compaction_state = _compaction_from_db(db, session) if session else {}
            if session:
                # 变量说明：count 表示当前步骤使用的 count 值。
                count = int(db.scalar(
                    select(func.count(ChatMessage.id)).where(ChatMessage.session_id == session.id)
                ) or 0)
                # 变量说明：max_sequence 表示当前步骤使用的 max_sequence 值。
                max_sequence = int(db.scalar(
                    select(func.max(ChatMessage.sequence)).where(ChatMessage.session_id == session.id)
                ) or 0)
                # 变量说明：transcript_sequence 表示当前步骤使用的 transcript_sequence 值。
                transcript_sequence = max_sequence or count
            else:
                # 变量说明：transcript_sequence 表示当前步骤使用的 transcript_sequence 值。
                transcript_sequence = 0
            # 变量说明：agent_tool_ids 表示agent_tool 对象标识集合。
            agent_tool_ids = list(getattr(agent, "tool_ids", []) or [])
            # 变量说明：workflow_profile_id 表示workflow_profile 对象的唯一标识。
            workflow_profile_id = str(
                getattr(agent, "workflow_profile_id", "auto") or "auto"
            )
            # 变量说明：agent_skill_ids 表示agent_skill 对象标识集合。
            agent_skill_ids = list(getattr(agent, "skill_ids", []) or [])
            # 变量说明：session_skill_ids 表示session_skill 对象标识集合。
            session_skill_ids = list(getattr(session, "skill_ids", []) or []) if session else []
            # 变量说明：configured_skill_ids 表示configured_skill 对象标识集合。
            configured_skill_ids = session_skill_ids or agent_skill_ids
            # 变量说明：configured_mcp_server_names 表示当前流程使用的 configured_mcp_server_names 集合。
            configured_mcp_server_names = (
                list(getattr(session, "mcp_server_names", []) or []) if session else []
            )
            # 变量说明：permission_mode 表示当前步骤使用的 permission_mode 值。
            permission_mode = str(getattr(session, "permission_mode", "smart") or "smart")
            # 变量说明：allowed_tool_names 表示当前流程使用的 allowed_tool_names 集合。
            allowed_tool_names = _allowed_runtime_tool_names(agent_tool_ids)
            # 变量说明：skill_instructions 表示当前流程使用的 skill_instructions 集合。
            skill_instructions = _read_selected_skill_instructions(db, configured_skill_ids)
            # A new ordinary user turn starts with no previous task board. A
            # recovery run reads its canonical plan from DurableTask/PlanStep;
            # approval resume may use its frozen binding below only for legacy
            # runs that predate durable task state.
            # 变量说明：todo_state 表示当前步骤使用的 todo_state 值。
            todo_state = todo_state_for_run(db, run)
            if frozen_binding:
                # 变量说明：global_memories_enabled 表示当前步骤使用的 global_memories_enabled 值。
                global_memories_enabled = bool(frozen_binding.get("memories_enabled", True))
                # 变量说明：use_memories 表示当前流程使用的 use_memories 集合。
                use_memories = bool(frozen_binding.get("use_memories", True))
                # 变量说明：memory_use_enabled 表示当前步骤使用的 memory_use_enabled 值。
                memory_use_enabled = global_memories_enabled and use_memories
                # 变量说明：memory_index 表示当前步骤使用的 memory_index 值。
                memory_index = (
                    str(frozen_binding.get("memory_index") or "")
                    if memory_use_enabled else ""
                )
            else:
                # 变量说明：global_memories_enabled 表示当前步骤使用的 global_memories_enabled 值。
                global_memories_enabled = memories_enabled(db)
                # 变量说明：use_memories 表示当前流程使用的 use_memories 集合。
                use_memories = bool(session.use_memories) if session is not None else True
                # 变量说明：memory_use_enabled 表示当前步骤使用的 memory_use_enabled 值。
                memory_use_enabled = global_memories_enabled and use_memories
                # 变量说明：memory_index 表示当前步骤使用的 memory_index 值。
                memory_index = ""
            if not frozen_binding and memory_use_enabled:
                # 变量说明：memory_index 表示当前步骤使用的 memory_index 值。
                memory_index = load_memory_index(
                    workspace_id=workspace.id,
                    session_id=session.id if session else None,
                )
            # 变量说明：effective_system_prompt 表示当前步骤使用的 effective_system_prompt 值。
            effective_system_prompt = agent.system_prompt
            # 变量说明：agents_instructions 表示当前流程使用的 agents_instructions 集合。
            agents_instructions = ""
            # 变量说明：agents_instruction_sources 表示当前流程使用的 agents_instruction_sources 集合。
            agents_instruction_sources: list[str] = []
            if frozen_binding:
                # 变量说明：frozen_agent_id 表示frozen_agent 对象的唯一标识。
                frozen_agent_id = str(frozen_binding.get("agent_id") or "").strip()
                if frozen_agent_id and agent.id != frozen_agent_id:
                    raise RuntimeError("冻结的子 Agent 已不存在或已被替换，已拒绝续跑")
                # 变量说明：required 表示当前步骤使用的 required 值。
                required = (
                    "workspace_root",
                    "model_connection_id",
                    "provider",
                    "base_url",
                    "secret_ref",
                    "model_id",
                    "custom_headers_digest",
                )
                # 变量说明：missing 表示当前步骤使用的 missing 值。
                missing = [key for key in required if not str(frozen_binding.get(key) or "").strip()]
                if missing:
                    raise RuntimeError(f"运行快照缺少冻结配置：{', '.join(missing)}")
                # 变量说明：connection 表示当前步骤使用的 connection 值。
                connection = db.get(ModelConnection, str(frozen_binding["model_connection_id"]))
                if connection is None or not connection.enabled:
                    raise RuntimeError("冻结的模型连接已不存在或被禁用，已拒绝续跑")
                # 变量说明：connection_changed 表示当前步骤使用的 connection_changed 值。
                connection_changed = (
                    connection.provider != str(frozen_binding["provider"])
                    or connection.api_protocol != str(frozen_binding.get("api_protocol") or "chat_completions")
                    or connection.base_url != str(frozen_binding["base_url"])
                    or connection.secret_ref != str(frozen_binding["secret_ref"])
                    or _configuration_digest(connection.custom_headers or {})
                    != str(frozen_binding["custom_headers_digest"])
                )
                if connection_changed:
                    raise RuntimeError("模型连接配置在审批等待期间已改变，已拒绝续跑")
                # 变量说明：workspace_root 表示当前步骤使用的 workspace_root 值。
                workspace_root = str(frozen_binding["workspace_root"])
                # 变量说明：agents_instructions 表示当前流程使用的 agents_instructions 集合。
                agents_instructions = str(frozen_binding.get("agents_instructions") or "")
                if isinstance(frozen_binding.get("agents_instruction_sources"), list):
                    # 变量说明：agents_instruction_sources 表示当前流程使用的 agents_instruction_sources 集合。
                    agents_instruction_sources = [
                        str(item) for item in frozen_binding["agents_instruction_sources"]
                    ]
                # 变量说明：provider_config 表示当前步骤使用的 provider_config 值。
                provider_config = ProviderConfig(
                    provider=str(frozen_binding["provider"]),
                    base_url=str(frozen_binding["base_url"]),
                    secret_ref=str(frozen_binding["secret_ref"]),
                    model_id=str(frozen_binding["model_id"]),
                    model_connection_id=str(frozen_binding["model_connection_id"]),
                    thinking_level=str(frozen_binding.get("thinking_level") or "auto"),
                    custom_headers=dict(connection.custom_headers or {}),
                    api_protocol=str(frozen_binding.get("api_protocol") or "chat_completions"),
                )
                if isinstance(frozen_binding.get("tool_ids"), list):
                    # 变量说明：agent_tool_ids 表示agent_tool 对象标识集合。
                    agent_tool_ids = [str(item) for item in frozen_binding["tool_ids"]]
                # 变量说明：workflow_profile_id 表示workflow_profile 对象的唯一标识。
                workflow_profile_id = str(
                    frozen_binding.get("workflow_profile_id") or "auto"
                )
                if isinstance(frozen_binding.get("skill_ids"), list):
                    # 变量说明：configured_skill_ids 表示configured_skill 对象标识集合。
                    configured_skill_ids = [str(item) for item in frozen_binding["skill_ids"]]
                if isinstance(frozen_binding.get("mcp_server_names"), list):
                    # 变量说明：configured_mcp_server_names 表示当前流程使用的 configured_mcp_server_names 集合。
                    configured_mcp_server_names = [
                        str(item) for item in frozen_binding["mcp_server_names"]
                    ]
                if str(frozen_binding.get("permission_mode") or "").strip():
                    # 变量说明：permission_mode 表示当前步骤使用的 permission_mode 值。
                    permission_mode = str(frozen_binding["permission_mode"])
                if isinstance(frozen_binding.get("allowed_tool_names"), list):
                    # 变量说明：allowed_tool_names 表示当前流程使用的 allowed_tool_names 集合。
                    allowed_tool_names = _allowed_runtime_tool_names(
                        [str(item) for item in frozen_binding["allowed_tool_names"]]
                    )
                else:
                    # Compatibility for snapshots created before executable
                    # capability bindings were persisted.
                    # 变量说明：allowed_tool_names 表示当前流程使用的 allowed_tool_names 集合。
                    allowed_tool_names = _allowed_runtime_tool_names(agent_tool_ids)
                if isinstance(frozen_binding.get("skill_instructions"), list):
                    # 变量说明：skill_instructions 表示当前流程使用的 skill_instructions 集合。
                    skill_instructions = [
                        dict(item) for item in frozen_binding["skill_instructions"] if isinstance(item, dict)
                    ]
                else:
                    # 变量说明：skill_instructions 表示当前流程使用的 skill_instructions 集合。
                    skill_instructions = _read_selected_skill_instructions(db, configured_skill_ids)
                if isinstance(frozen_binding.get("todo_state"), list) and not run.task_id:
                    # 变量说明：todo_state 表示当前步骤使用的 todo_state 值。
                    todo_state = list(frozen_binding["todo_state"])
                if "agent_system_prompt" in frozen_binding:
                    # 变量说明：effective_system_prompt 表示当前步骤使用的 effective_system_prompt 值。
                    effective_system_prompt = str(frozen_binding["agent_system_prompt"] or "")
                # A persisted child snapshot is never allowed to regain task
                # from a hand-edited/legacy binding during approval resume.
                if frozen_binding.get("delegation_version"):
                    # 变量说明：allowed_tool_names 表示当前流程使用的 allowed_tool_names 集合。
                    allowed_tool_names = [name for name in allowed_tool_names if name != "task"]
                    # 变量说明：messages 表示发送给模型或客户端的消息序列。
                    messages = [{
                        "role": "user",
                        "content": str(frozen_binding.get("rendered_task") or ""),
                    }]
                    # 变量说明：compaction_state 表示当前步骤使用的 compaction_state 值。
                    compaction_state = {}
                    # 变量说明：transcript_sequence 表示当前步骤使用的 transcript_sequence 值。
                    transcript_sequence = 0
                    if "max_run_seconds" in frozen_binding:
                        # 变量说明：frozen_limit 表示当前步骤使用的 frozen_limit 值。
                        frozen_limit = frozen_binding.get("max_run_seconds")
                        # 变量说明：runtime_max_run_seconds 表示当前流程使用的 runtime_max_run_seconds 集合。
                        runtime_max_run_seconds = (
                            max(0.0, float(frozen_limit))
                            if frozen_limit is not None
                            else None
                        )
            else:
                # 变量说明：connection 表示当前步骤使用的 connection 值。
                connection = _effective_connection(db, session, agent)
                if connection is None:
                    raise ModelConfigurationError("当前 Agent 没有可用模型连接")
                # 变量说明：session_model 表示当前步骤使用的 session_model 值。
                session_model = None
                if session is not None and (
                    not session.model_connection_id or session.model_connection_id == connection.id
                ):
                    # 变量说明：session_model 表示当前步骤使用的 session_model 值。
                    session_model = session.model_id
                # 变量说明：agent_model 表示当前步骤使用的 agent_model 值。
                agent_model = agent.model_id if (
                    not agent.model_connection_id or agent.model_connection_id == connection.id
                ) else None
                # 变量说明：model_id 表示model 对象的唯一标识。
                model_id = session_model or agent_model or connection.default_model
                if not model_id:
                    # 变量说明：candidates 表示当前流程使用的 candidates 集合。
                    candidates = [*(connection.discovered_models or []), *(connection.manual_models or [])]
                    # 变量说明：model_id 表示model 对象的唯一标识。
                    model_id = candidates[0] if candidates else None
                if not model_id:
                    raise ModelConfigurationError("模型连接中没有可用模型，请发现模型或填写手动模型 ID")
                # 变量说明：thinking_level 表示当前步骤使用的 thinking_level 值。
                thinking_level = _explicit_setting(
                    session.thinking_level if session is not None else None,
                    agent.thinking_level,
                    connection.thinking_level,
                ) or "auto"
                # 变量说明：workspace_root 表示当前步骤使用的 workspace_root 值。
                workspace_root = workspace.root_path
                # 变量说明：agents_instructions 表示当前流程使用的 agents_instructions 集合；agents_instruction_sources 表示当前流程使用的 agents_instruction_sources 集合。
                agents_instructions, agents_instruction_sources = load_instruction_chain(workspace_root)
                if "read_artifact" not in allowed_tool_names:
                    allowed_tool_names.append("read_artifact")
                # 变量说明：has_attachments 表示表示是否满足 _attachments 条件的布尔标记。
                has_attachments = bool(session and db.scalar(
                    select(Artifact.id).where(
                        Artifact.session_id == session.id,
                        Artifact.kind == "user_attachment",
                        Artifact.status == "available",
                    ).limit(1)
                ))
                if has_attachments:
                    for tool_name in ATTACHMENT_TOOL_NAMES:
                        if tool_name not in allowed_tool_names:
                            allowed_tool_names.append(tool_name)
                # 变量说明：provider_config 表示当前步骤使用的 provider_config 值。
                provider_config = ProviderConfig(
                    provider=connection.provider,
                    base_url=connection.base_url,
                    secret_ref=connection.secret_ref,
                    model_id=model_id,
                    model_connection_id=connection.id,
                    thinking_level=thinking_level,
                    custom_headers=dict(connection.custom_headers or {}),
                    api_protocol=connection.api_protocol,
                )
                # 变量说明：frozen_binding 表示当前步骤使用的 frozen_binding 值。
                frozen_binding = {
                    "workspace_root": workspace_root,
                    "agents_instructions": agents_instructions,
                    "agents_instruction_sources": agents_instruction_sources,
                    "model_connection_id": connection.id,
                    "provider": connection.provider,
                    "api_protocol": connection.api_protocol,
                    "base_url": connection.base_url,
                    "secret_ref": connection.secret_ref,
                    "model_id": model_id,
                    "thinking_level": thinking_level,
                    "workflow_profile_id": workflow_profile_id,
                    "permission_mode": permission_mode,
                    "skill_ids": configured_skill_ids,
                    "mcp_server_names": configured_mcp_server_names,
                    "tool_ids": agent_tool_ids,
                    "allowed_tool_names": allowed_tool_names,
                    "skill_instructions": skill_instructions,
                    "todo_state": todo_state,
                    "validation_runtime": dict(workspace.validation_runtime or {}),
                    "memories_enabled": global_memories_enabled,
                    "use_memories": use_memories,
                    "memory_index": memory_index,
                    # Header values may contain credentials. Persist only a
                    # digest and reject resume if the live values drift.
                    "custom_headers_digest": _configuration_digest(connection.custom_headers or {}),
                }

            if memory_use_enabled:
                for tool_name in ("MemorySearch", "MemoryRead", "MemoryList"):
                    if tool_name not in allowed_tool_names:
                        allowed_tool_names.append(tool_name)
                # 变量说明：frozen_binding 的索引项 表示该语句创建或更新的目标数据。
                frozen_binding["allowed_tool_names"] = allowed_tool_names

            # 变量说明：delegate_catalog_prompt 表示当前步骤使用的 delegate_catalog_prompt 值。
            delegate_catalog_prompt = "" if frozen_binding.get("delegation_version") else _delegate_catalog_prompt(db, run)
            # 变量说明：terminal_background_job_ids 表示terminal_background_job 对象标识集合。
            terminal_background_job_ids: list[str] = []
            if not frozen_binding.get("delegation_version"):
                delegate_catalog_prompt += recovery_prompt(db, run)
            if {"background_run", "check_background"}.issubset(set(allowed_tool_names)):
                delegate_catalog_prompt += (
                    "\n后台作业规则：耗时下载、安装或构建可先调用 background_run，随后继续完成不依赖其结果的工作；"
                    "后台作业入队不代表任务完成。若最终候选结果产生时作业仍在运行，运行时会暂停并由终态事件自动恢复；"
                    "仅在需要主动查看中间日志时调用 check_background，不要反复轮询。\n"
                )
                # 变量说明：background_ownership 表示当前步骤使用的 background_ownership 值。
                background_ownership = (
                    BackgroundJob.run_id == run.id
                    if frozen_binding.get("delegation_version")
                    else BackgroundJob.session_id == (session.id if session else None)
                )
                # 变量说明：active_background_jobs 表示当前流程使用的 active_background_jobs 集合。
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
                # 变量说明：terminal_background_jobs 表示当前流程使用的 terminal_background_jobs 集合。
                terminal_background_jobs = list(db.scalars(
                    select(BackgroundJob).where(
                        background_ownership,
                        BackgroundJob.status.in_({"completed", "failed", "cancelled"}),
                        BackgroundJob.observed_at.is_(None),
                    ).order_by(BackgroundJob.created_at.asc())
                )) if session is not None else []
                if terminal_background_jobs:
                    # 变量说明：terminal_background_job_ids 表示terminal_background_job 对象标识集合。
                    terminal_background_job_ids = [job.id for job in terminal_background_jobs]
                    # 变量说明：terminal_payloads 表示当前流程使用的 terminal_payloads 集合。
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

            # 变量说明：context 表示当前步骤使用的 context 值。
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
                "workflow_profile_id": workflow_profile_id,
                "skill_ids": configured_skill_ids,
                "mcp_server_names": configured_mcp_server_names,
                "tool_ids": agent_tool_ids,
                "allowed_tool_names": allowed_tool_names,
                "skill_instructions": skill_instructions,
                "todo_state": todo_state,
                "max_run_seconds": runtime_max_run_seconds,
                "session_id": session.id if session else None,
                "agent_id": agent.id,
                "workspace_id": workspace.id,
                "memory_use_enabled": memory_use_enabled,
                "terminal_background_job_ids": list(terminal_background_job_ids),
                "expose_legacy_tools": bool(runtime_binding),
                "compaction_state": compaction_state,
                "context_sequence": transcript_sequence,
            }
            db.commit()

        # 变量说明：assembly 表示当前步骤使用的 assembly 值。
        assembly = RunRuntimeFactory().create(
            run_id=run_id,
            context=context,
            coordinator=owner,
        )
        # 变量说明：context 的索引项 表示该语句创建或更新的目标数据。
        context["background_store"] = assembly.background_store
        return assembly.runtime, context

    # 函数职责：完成 delegation_link 对应的业务处理。
    # 参数关系：db 表示当前数据库会话；run 表示当前步骤使用的 run 值；runtime_binding 表示当前步骤使用的 runtime_binding 值。
    # 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
    @staticmethod
    def _delegation_link(
        db: Any,
        run: Run,
        runtime_binding: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """Find the durable parent/task link for a delegated child run."""

        # 变量说明：binding 表示当前步骤使用的 binding 值。
        binding = dict(runtime_binding or {})
        if binding.get("delegation_version") and binding.get("delegation_id"):
            return {
                "delegation_id": str(binding["delegation_id"]),
                "parent_run_id": str(binding.get("parent_run_id") or ""),
                "parent_agent_id": str(binding.get("parent_agent_id") or ""),
                "parent_session_id": binding.get("parent_session_id"),
            }
        # 变量说明：link 表示当前步骤使用的 link 值。
        link = db.scalar(
            select(RunEvent)
            .where(RunEvent.run_id == run.id, RunEvent.event_type == "delegation_link")
            .order_by(RunEvent.created_at.desc(), RunEvent.id.desc())
        )
        if link is None or not isinstance(link.payload, dict):
            return None
        # 变量说明：payload 表示跨层传递的数据载荷。
        payload = link.payload
        # 变量说明：delegation_id 表示delegation 对象的唯一标识。
        delegation_id = str(payload.get("delegation_id") or "").strip()
        if delegation_id:
            return {
                "delegation_id": delegation_id,
                "parent_run_id": str(payload.get("parent_run_id") or ""),
                "parent_agent_id": str(payload.get("parent_agent_id") or ""),
                "parent_session_id": payload.get("parent_session_id"),
            }
        return None

    # 函数职责：完成 sync_delegated_child_state 对应的业务处理。
    # 参数关系：db 表示当前数据库会话；run 表示当前步骤使用的 run 值；status 表示当前对象或运行的状态；output 表示当前步骤使用的 output 值；stop_reason 表示当前步骤使用的 stop_reason 值；error 表示当前捕获或准备上报的错误；error_code 表示当前步骤使用的 error_code 值；pending_approval 表示当前步骤使用的 pending_approval 值；其余参数沿用调用方提供的扩展选项。
    # 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
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

        # 变量说明：link 表示当前步骤使用的 link 值。
        link = RunCoordinator._delegation_link(db, run, runtime_binding)
        if link is None:
            return None
        # 变量说明：delegation_id 表示delegation 对象的唯一标识。
        delegation_id = str(link.get("delegation_id") or "")
        if not delegation_id:
            return None
        # 变量说明：task 表示当前步骤使用的 task 值。
        task = db.get(DelegatedTask, delegation_id)
        if task is None:
            return None
        # 变量说明：existing_child_run_id 表示existing_child_run 对象的唯一标识。
        existing_child_run_id = str(task.child_run_id or (task.result or {}).get("child_run_id") or "")
        if existing_child_run_id and existing_child_run_id != run.id:
            return None

        # 变量说明：binding 表示当前步骤使用的 binding 值。
        binding = dict(runtime_binding or {})
        if not binding:
            # 变量说明：snapshot 表示当前步骤使用的 snapshot 值。
            snapshot = db.scalar(
                select(RunEvent)
                .where(RunEvent.run_id == run.id, RunEvent.event_type == "runtime_snapshot")
                .order_by(RunEvent.sequence.desc(), RunEvent.id.desc())
            )
            if snapshot is not None and isinstance(snapshot.payload, dict):
                # 变量说明：stored_binding 表示当前步骤使用的 stored_binding 值。
                stored_binding = snapshot.payload.get("runtime_binding")
                if isinstance(stored_binding, dict):
                    # 变量说明：binding 表示当前步骤使用的 binding 值。
                    binding = dict(stored_binding)
        if not binding:
            # Failures before a first runtime snapshot still have the public
            # frozen selection captured when the delegation was created.
            # 变量说明：stored_binding 表示当前步骤使用的 stored_binding 值。
            stored_binding = (task.result or {}).get("binding")
            if isinstance(stored_binding, dict):
                # 变量说明：binding 表示当前步骤使用的 binding 值。
                binding = dict(stored_binding)
        # 变量说明：child 表示当前步骤使用的 child 值。
        child = db.get(Agent, run.agent_id or task.child_agent_id)
        # 变量说明：child_id 表示child 对象的唯一标识。
        child_id = child.id if child is not None else (run.agent_id or task.child_agent_id or "")
        # 变量说明：child_name 表示当前步骤使用的 child_name 值。
        child_name = _single_line(child.name, limit=120) if child is not None else "Child Agent"
        # 变量说明：public_binding 表示当前步骤使用的 public_binding 值。
        public_binding = _SubagentTaskDelegate._public_binding(binding) if binding else {}
        # 变量说明：safe_pending 表示当前步骤使用的 safe_pending 值。
        safe_pending = _SubagentTaskDelegate._safe_pending_summary(pending_approval)
        # 变量说明：linked_plan_step 表示当前步骤使用的 linked_plan_step 值。
        linked_plan_step = db.get(PlanStep, task.plan_step_id) if task.plan_step_id else None
        # 变量说明：waiting_background 表示当前步骤使用的 waiting_background 值。
        waiting_background = status == "stopped" and stop_reason == "waiting_background"
        # 变量说明：public_status 表示当前流程使用的 public_status 集合。
        public_status = "waiting_background" if waiting_background else status
        # 变量说明：payload 表示跨层传递的数据载荷。
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
        # 变量说明：task_status 表示当前流程使用的 task_status 集合。
        task_status = "in_progress" if status == "awaiting_approval" or waiting_background else (
            "completed" if status == "completed" else "blocked"
        )
        # 变量说明：changed 表示当前步骤使用的 changed 值。
        changed = (
            task.status != task_status
            or _canonical_arguments(task.result) != _canonical_arguments(payload)
        )
        if changed:
            # 变量说明：status 表示当前对象或运行的状态。
            task.status = task_status
            # 变量说明：result 表示本步骤产生的结果。
            task.result = payload
        # 变量说明：worker 表示当前步骤使用的 worker 值。
        worker = db.get(TeammateWorker, task.teammate_id) if task.teammate_id else None
        if worker is not None:
            if status == "awaiting_approval" or waiting_background:
                # 变量说明：status 表示当前对象或运行的状态。
                worker.status = "working"
            elif status == "completed":
                # 变量说明：status 表示当前对象或运行的状态。
                worker.status = "stopped" if worker.status == "stopping" else "idle"
                # 变量说明：current_plan_step_id 表示current_plan_step 对象的唯一标识。
                worker.current_plan_step_id = None
            else:
                # Assignment failure is durable history, not destruction of
                # the reusable teammate identity. A later graph step may
                # safely assign the same teammate again.
                # 变量说明：status 表示当前对象或运行的状态。
                worker.status = "stopped" if worker.status == "stopping" else "idle"
                # 变量说明：current_plan_step_id 表示current_plan_step 对象的唯一标识。
                worker.current_plan_step_id = None
            # 变量说明：last_run_id 表示last_run 对象的唯一标识。
            worker.last_run_id = run.id
        if task.plan_step_id:
            # 变量说明：plan_step 表示当前步骤使用的 plan_step 值。
            plan_step = linked_plan_step or db.get(PlanStep, task.plan_step_id)
            if plan_step is not None and status in {"completed", "failed", "stopped"} and not waiting_background:
                # 变量说明：status 表示当前对象或运行的状态。
                plan_step.status = "completed" if status == "completed" else "failed"
                # 变量说明：result 表示本步骤产生的结果。
                plan_step.result = str(output or "")
                # 变量说明：error 表示当前捕获或准备上报的错误。
                plan_step.error = None if status == "completed" else str(error or stop_reason or status)
                # 变量说明：remaining_work 表示当前步骤使用的 remaining_work 值。
                plan_step.remaining_work = [] if status == "completed" else list(
                    plan_step.remaining_work or [plan_step.title]
                )
                if status == "completed":
                    # 变量说明：completed_work 表示当前步骤使用的 completed_work 值。
                    plan_step.completed_work = list(plan_step.completed_work or [plan_step.title])
                # 变量说明：completed_at 表示completed_at 对应的时间信息。
                plan_step.completed_at = _utcnow()
                # 变量说明：assigned_run_id 表示assigned_run 对象的唯一标识。
                plan_step.assigned_run_id = run.id
                refresh_task_state(db, plan_step.task_id)

        # 变量说明：event_type 表示当前步骤使用的 event_type 值。
        event_type = {
            "awaiting_approval": "delegated_child_awaiting_approval",
            "completed": "delegated_child_completed",
            "stopped": "delegated_child_stopped",
            "failed": "delegated_child_failed",
        }.get(status, "delegated_child_incomplete")
        if waiting_background:
            # 变量说明：event_type 表示当前步骤使用的 event_type 值。
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
                # 变量说明：worker_message 表示当前步骤使用的 worker_message 值。
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
        # 变量说明：continuation_queued 表示当前步骤使用的 continuation_queued 值。
        continuation_queued = False
        if status in {"completed", "failed", "stopped"} and not waiting_background and link["parent_run_id"]:
            # A parallel parent must resume only after every child that paused
            # for approval has reached a terminal state.  At the point the
            # parent is stopped for delegated approval, any remaining
            # in-progress delegation belongs to that waiting parallel batch.
            db.flush()
            # 变量说明：waiting_sibling_id 表示waiting_sibling 对象的唯一标识。
            waiting_sibling_id = db.scalar(
                select(DelegatedTask.id)
                .where(
                    DelegatedTask.parent_run_id == link["parent_run_id"],
                    DelegatedTask.status == "in_progress",
                )
                .limit(1)
            )
            # 变量说明：parent 表示当前步骤使用的 parent 值。
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
                # 变量说明：status 表示当前对象或运行的状态。
                parent.status = "received"
                # 变量说明：stop_reason 表示当前步骤使用的 stop_reason 值。
                parent.stop_reason = None
                # 变量说明：error_message 表示当前步骤使用的 error_message 值。
                parent.error_message = None
                # 变量说明：finished_at 表示finished_at 对应的时间信息。
                parent.finished_at = None
                if parent.task_id:
                    # 变量说明：parent_task 表示当前步骤使用的 parent_task 值。
                    parent_task = db.get(DurableTask, parent.task_id)
                    if parent_task is not None and parent_task.status == "waiting":
                        # 变量说明：status 表示当前对象或运行的状态。
                        parent_task.status = "running"
                        # 变量说明：resume_summary 表示当前步骤使用的 resume_summary 值。
                        parent_task.resume_summary = "A delegated child terminal event arrived; resume coordination."
                # 变量说明：continuation_queued 表示当前步骤使用的 continuation_queued 值。
                continuation_queued = True
                append_run_event(
                    db,
                    run_id=parent.id,
                    event_type="delegated_child_continuation_queued",
                    payload={
                        "child_run_id": run.id,
                        "task_id": task.id,
                        "delegation_id": task.id,
                        "child_status": status,
                    },
                )

        # 变量说明：parent_event 表示当前步骤使用的 parent_event 值。
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
            append_run_event(
                db,
                run_id=link["parent_run_id"],
                event_type=event_type,
                payload=parent_event,
            )

        # A child response is deliberately *not* a normal chat message.
        # Its complete structured result remains on DelegatedTask and in the
        # child Run timeline, while the parent coordinator is solely
        # responsible for the user-facing final reply.  This prevents the
        # same answer appearing once as a child transcript and again as the
        # parent's synthesis.
        return parent_event if (changed or continuation_queued) and link["parent_run_id"] else None

    # 函数职责：完成 reconcile_delegated_child_terminal 对应的业务处理。
    # 参数关系：db 表示当前数据库会话；run 表示当前步骤使用的 run 值；status 表示当前对象或运行的状态；stop_reason 表示当前步骤使用的 stop_reason 值；error 表示当前捕获或准备上报的错误；error_code 表示当前步骤使用的 error_code 值；runtime_binding 表示当前步骤使用的 runtime_binding 值。
    # 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
    def reconcile_delegated_child_terminal(
        self,
        db: Any,
        run: Run,
        *,
        status: str,
        stop_reason: str | None = None,
        error: str | None = None,
        error_code: str | None = None,
        runtime_binding: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """Expose a transaction-local terminal sync for approval rejection."""

        return self._sync_delegated_child_state(
            db,
            run,
            status=status,
            stop_reason=stop_reason,
            error=error,
            error_code=error_code,
            runtime_binding=runtime_binding,
        )

    # 函数职责：完成 persist_outcome 对应的业务处理。
    # 参数关系：run_id 表示当前运行标识；outcome 表示当前步骤使用的 outcome 值；consumed_background_event_ids 表示consumed_background_event 对象标识集合。
    # 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
    @staticmethod
    def _persist_outcome(
        run_id: str,
        outcome: RunOutcome,
        consumed_background_event_ids: Iterable[str] = (),
    ) -> None:
        # 变量说明：error_event 表示当前步骤使用的 error_event 值。
        error_event = next(
            (
                event
                for event in reversed(outcome.events)
                if event.get("error_type") or event.get("error_code") or event.get("code")
            ),
            {},
        )
        # 变量说明：event_error_type 表示当前步骤使用的 event_error_type 值。
        event_error_type = str(
            error_event.get("error_type")
            or error_event.get("error_code")
            or error_event.get("code")
            or ""
        )
        # 变量说明：effective_status 表示当前流程使用的 effective_status 集合。
        effective_status = str(outcome.status or "failed")
        # 变量说明：effective_stop_reason 表示当前步骤使用的 effective_stop_reason 值。
        effective_stop_reason = outcome.stop_reason
        # 变量说明：normalized_error_code 表示当前步骤使用的 normalized_error_code 值。
        normalized_error_code: str | None = None
        # 变量说明：safe_error_message 表示当前步骤使用的 safe_error_message 值。
        safe_error_message: str | None = None
        if effective_status == "completed" and not str(outcome.output or "").strip():
            # 变量说明：effective_status 表示当前流程使用的 effective_status 集合。
            effective_status = "failed"
            # 变量说明：effective_stop_reason 表示当前步骤使用的 effective_stop_reason 值。
            effective_stop_reason = None
            # 变量说明：normalized_error_code 表示当前步骤使用的 normalized_error_code 值。
            normalized_error_code = "empty_model_output"
            # 变量说明：safe_error_message 表示当前步骤使用的 safe_error_message 值。
            safe_error_message = public_error_message(normalized_error_code)
        elif effective_status == "failed":
            # 变量说明：normalized_error_code 表示当前步骤使用的 normalized_error_code 值。
            normalized_error_code = classify_error_details(
                event_error_type,
                outcome.error,
                status_code=error_event.get("status_code"),
                error_kind=error_event.get("error_kind"),
                provider_error_code=error_event.get("provider_error_code"),
            )
            # 变量说明：safe_error_message 表示当前步骤使用的 safe_error_message 值。
            safe_error_message = public_error_message(normalized_error_code)
        elif effective_status == "stopped" and is_terminal_delivery(effective_status, effective_stop_reason):
            # 变量说明：normalized_error_code 表示当前步骤使用的 normalized_error_code 值。
            normalized_error_code = terminal_error_code(
                status=effective_status,
                stop_reason=effective_stop_reason,
                error_code=event_error_type or None,
            )
            # 变量说明：safe_error_message 表示当前步骤使用的 safe_error_message 值。
            safe_error_message = public_error_message(normalized_error_code, effective_status)
        elif effective_status == "stopped":
            # Waiting for a delegated child is a resumable pause, not the
            # terminal delivery boundary for the user's turn.
            # 变量说明：safe_error_message 表示当前步骤使用的 safe_error_message 值。
            safe_error_message = outcome.error
        # 变量说明：snapshot 表示当前步骤使用的 snapshot 值。
        snapshot = RunCoordinator._runtime_snapshot(outcome)
        snapshot["status"] = effective_status
        snapshot["delivery_error_code"] = normalized_error_code
        # 变量说明：publish_type 表示当前步骤使用的 publish_type 值。
        publish_type = {
            "awaiting_approval": "approval_requested",
            "completed": "run_completed",
            "stopped": "run_stopped",
            "failed": "model_failed",
        }.get(effective_status)
        # 变量说明：publish_event 表示当前步骤使用的 publish_event 值。
        publish_event = next(
            (
                _json_safe(event)
                for event in reversed(outcome.events)
                if event.get("type") == publish_type
            ),
            None,
        )
        if effective_status != outcome.status:
            # 变量说明：publish_event 表示当前步骤使用的 publish_event 值。
            publish_event = {
                "type": "model_failed",
                "code": normalized_error_code,
                "error": safe_error_message,
            }
        # 变量说明：persisted 表示当前步骤使用的 persisted 值。
        persisted = False
        # 变量说明：parent_bridge_event 表示当前步骤使用的 parent_bridge_event 值。
        parent_bridge_event: dict[str, Any] | None = None
        # 变量说明：extraction_job_id 表示extraction_job 对象的唯一标识。
        extraction_job_id: str | None = None
        with database_module.SessionLocal() as db:
            # Serialize this persistence path against ``stop()``.  Whichever
            # conditional row update claims the run first owns the lifecycle
            # transition; a provider result that arrives after a user stop
            # is discarded before it can append messages or context.
            # 变量说明：claim 表示当前步骤使用的 claim 值。
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
                # 变量说明：run 表示当前步骤使用的 run 值。
                run = db.get(Run, run_id)
                if run is not None and RunCoordinator._user_interrupt_locked(run):
                    append_run_event(
                        db,
                        run_id=run_id,
                        event_type="run_outcome_discarded",
                        payload={"reason": USER_INTERRUPT_REASON, "late_status": outcome.status},
                    )
                    db.commit()
                else:
                    db.rollback()
                RunCoordinator._clear_stream_buffer(run_id, interrupted=True)
                coordinator.unregister_tool_canceller(run_id)
                return
            # 变量说明：run 表示当前步骤使用的 run 值。
            run = db.get(Run, run_id)
            if run is None:
                return
            # An explicit stop commits before cancelling the in-process task.
            # A provider may still return from a synchronous worker or a
            # cancellation race may let this method run; never let that late
            # outcome rewrite the user's durable stopped state.
            if RunCoordinator._user_interrupt_locked(run):
                append_run_event(db,
                    run_id=run_id,
                    event_type="run_outcome_discarded",
                    payload={
                        "reason": USER_INTERRUPT_REASON,
                        "late_status": outcome.status,
                    },
                )
                db.commit()
                RunCoordinator._clear_stream_buffer(run_id, interrupted=True)
                coordinator.unregister_tool_canceller(run_id)
                return
            # 变量说明：turn 表示当前步骤使用的 turn 值。
            turn = ensure_run_turn(db, run)
            # 变量说明：status 表示当前对象或运行的状态。
            run.status = effective_status
            # 变量说明：current_step 表示当前步骤使用的 current_step 值。
            run.current_step = outcome.steps
            # 变量说明：tool_calls 表示当前流程使用的 tool_calls 集合。
            run.tool_calls = outcome.tool_calls
            # 变量说明：stop_reason 表示当前步骤使用的 stop_reason 值。
            run.stop_reason = effective_stop_reason
            # 变量说明：error_code 表示当前步骤使用的 error_code 值。
            run.error_code = normalized_error_code
            # 变量说明：error_message 表示当前步骤使用的 error_message 值。
            run.error_message = safe_error_message
            if effective_status in {"completed", "failed", "stopped"}:
                # 变量说明：finished_at 表示finished_at 对应的时间信息。
                run.finished_at = _utcnow()
            else:
                # 变量说明：finished_at 表示finished_at 对应的时间信息。
                run.finished_at = None
            transition_run_task(db, run, status=effective_status, stop_reason=effective_stop_reason)
            sync_turn_progress(db, run)
            append_run_event(db, run_id=run_id, event_type="runtime_snapshot", payload=snapshot)

            # 变量说明：parent_bridge_event 表示当前步骤使用的 parent_bridge_event 值。
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
                # 变量说明：session_for_context 表示当前步骤使用的 session_for_context 值。
                session_for_context = db.get(Session, run.session_id)
                # 变量说明：source_sequence_valid 表示当前步骤使用的 source_sequence_valid 值。
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

            # 变量说明：usage 表示当前步骤使用的 usage 值。
            usage = normalize_usage(outcome.usage)
            if usage["request_count"]:
                # 变量说明：connection_id 表示connection 对象的唯一标识。
                connection_id = usage["model_connection_id"]
                # 变量说明：record 表示当前步骤使用的 record 值。
                record = db.scalar(select(UsageRecord).where(UsageRecord.run_id == run_id))
                if record is None:
                    # 变量说明：record 表示当前步骤使用的 record 值。
                    record = UsageRecord(
                        run_id=run_id,
                        session_id=run.session_id,
                        agent_id=run.agent_id,
                        model_connection_id=connection_id,
                        model_id=usage["model_id"],
                        provider=usage["provider"],
                    )
                    db.add(record)
                # 变量说明：model_connection_id 表示model_connection 对象的唯一标识。
                record.model_connection_id = connection_id
                # 变量说明：model_id 表示model 对象的唯一标识。
                record.model_id = usage["model_id"]
                # 变量说明：provider 表示模型供应商。
                record.provider = usage["provider"]
                # 变量说明：request_count 表示request 的数量。
                record.request_count = usage["request_count"]
                # 变量说明：input_tokens 表示当前流程使用的 input_tokens 集合。
                record.input_tokens = usage["input_tokens"]
                # 变量说明：output_tokens 表示当前流程使用的 output_tokens 集合。
                record.output_tokens = usage["output_tokens"]
                # 变量说明：cache_creation_tokens 表示当前流程使用的 cache_creation_tokens 集合。
                record.cache_creation_tokens = usage["cache_creation_tokens"]
                # 变量说明：cache_read_tokens 表示当前流程使用的 cache_read_tokens 集合。
                record.cache_read_tokens = usage["cache_read_tokens"]
                # 变量说明：total_tokens 表示当前流程使用的 total_tokens 集合。
                record.total_tokens = usage["total_tokens"]
                # 变量说明：cost_usd 表示当前步骤使用的 cost_usd 值。
                record.cost_usd = usage["cost_usd"]

            if effective_status == "awaiting_approval" and outcome.pending_approval:
                # 变量说明：pending 表示当前步骤使用的 pending 值。
                pending = outcome.pending_approval
                # 变量说明：existing_pending 表示当前步骤使用的 existing_pending 值。
                existing_pending = list(db.scalars(
                    select(Approval)
                    .where(Approval.run_id == run_id, Approval.status == "pending")
                    .order_by(Approval.created_at.asc(), Approval.id.asc())
                ))
                # 变量说明：matching 表示当前步骤使用的 matching 值。
                matching: Approval | None = None
                for existing in existing_pending:
                    if matching is None and approval_matches_pending(existing, pending):
                        # 变量说明：matching 表示当前步骤使用的 matching 值。
                        matching = existing
                        continue
                    # 变量说明：status 表示当前对象或运行的状态。
                    existing.status = "superseded"
                    # 变量说明：decided_at 表示decided_at 对应的时间信息。
                    existing.decided_at = _utcnow()
                    append_run_event(
                        db,
                        run_id=run_id,
                        event_type="approval_superseded",
                        payload={"approval_id": existing.id, "reason": "pending_call_changed"},
                    )
                if matching is None:
                    db.add(Approval(
                        run_id=run_id,
                        tool_name=str(pending.get("tool_name") or "unknown"),
                        arguments=dict(pending.get("arguments") or {}),
                        reason=str(pending.get("reason") or "该工具会修改本机状态，需要你的确认"),
                    ))
            # 变量说明：terminal_provider_message 表示当前步骤使用的 terminal_provider_message 值。
            terminal_provider_message = next(
                (
                    message
                    for message in reversed(list(outcome.transcript_delta or outcome.messages))
                    if isinstance(message, dict)
                    and message.get("role") == "assistant"
                    and not message.get("tool_calls")
                    and str(message.get("content") or "").strip() == str(outcome.output or "").strip()
                ),
                {},
            )
            # 变量说明：terminal_reasoning 表示当前步骤使用的 terminal_reasoning 值。
            terminal_reasoning = str(terminal_provider_message.get("reasoning_content") or "")
            # 变量说明：terminal_native 表示当前步骤使用的 terminal_native 值。
            terminal_native = terminal_provider_message.get("_pgagent_provider")
            persist_terminal_response(
                db,
                run,
                output=str(outcome.output or "").strip() or None,
                error_code=normalized_error_code,
                error_message=safe_error_message,
                provider_payload=(
                    {
                        **({"reasoning_content": terminal_reasoning} if terminal_reasoning else {}),
                        **({"native": terminal_native} if isinstance(terminal_native, dict) else {}),
                    } or None
                ),
            )
            # 变量说明：cited 表示当前步骤使用的 cited 值。
            cited = (
                []
                if outcome.runtime_binding.get("delegation_version")
                else record_memory_citations(db, run=run, citation=outcome.memory_citation)
            )
            if cited:
                append_run_event(
                    db,
                    run_id=run.id,
                    event_type="memory_citations_recorded",
                    payload={
                        "targets": [
                            {"type": item.target_type, "id": item.target_id}
                            for item in cited
                        ]
                    },
                )
            if (
                effective_status == "completed"
                and turn is not None
                and run.session_id
                and not outcome.runtime_binding.get("delegation_version")
                and bool(outcome.runtime_binding.get("memories_enabled", True))
                and str(outcome.output or "").strip()
            ):
                # 变量说明：existing_job 表示当前步骤使用的 existing_job 值。
                existing_job = db.scalar(select(MemoryJob).where(
                    MemoryJob.run_id == run.id,
                    MemoryJob.kind == "extract",
                ))
                if existing_job is None:
                    db.flush()
                    # 变量说明：source_end_sequence 表示当前步骤使用的 source_end_sequence 值。
                    source_end_sequence = int(db.scalar(select(func.max(ChatMessage.sequence)).where(
                        ChatMessage.session_id == run.session_id
                    )) or 0)
                    # 变量说明：candidate_job 表示当前步骤使用的 candidate_job 值。
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
                        # 变量说明：existing_job 表示当前步骤使用的 existing_job 值。
                        existing_job = candidate_job
                    except IntegrityError:
                        # 变量说明：existing_job 表示当前步骤使用的 existing_job 值。
                        existing_job = db.scalar(select(MemoryJob).where(
                            MemoryJob.run_id == run.id,
                            MemoryJob.kind == "extract",
                        ))
                if existing_job is not None and existing_job.status == "pending":
                    # 变量说明：extraction_job_id 表示extraction_job 对象的唯一标识。
                    extraction_job_id = existing_job.id
            if run.session_id:
                # 变量说明：session 表示当前步骤使用的 session 值。
                session = db.get(Session, run.session_id)
                if session is not None:
                    # 变量说明：updated_at 表示最近更新时间。
                    session.updated_at = _utcnow()
                    # Recompute after terminal delivery so the UI immediately
                    # includes the final assistant reply in context usage.
                    _prepare_session_history(db, session)
                    # 变量说明：prepared 表示当前步骤使用的 prepared 值。
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
                        # 变量说明：context_tokens 表示当前流程使用的 context_tokens 集合。
                        session.context_tokens = min(
                            max(0, int(prepared.get("estimated_tokens") or 0)),
                            settings.context_limit_tokens,
                        )
            # 变量说明：background_event_ids 表示background_event 对象标识集合。
            background_event_ids = sorted({
                str(job_id) for job_id in consumed_background_event_ids if str(job_id)
            })
            if background_event_ids:
                # 变量说明：consumed_at 表示consumed_at 对应的时间信息。
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
            # 变量说明：persisted 表示当前步骤使用的 persisted 值。
            persisted = True
        if persisted and publish_event is not None:
            run_stream_broker.publish(run_id, publish_event)
        if persisted and parent_bridge_event is not None:
            # 变量说明：parent_run_id 表示parent_run 对象的唯一标识。
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

    # 函数职责：异步完成 execute 对应的业务处理。
    # 参数关系：run_id 表示当前运行标识。
    # 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
    async def _execute(self, run_id: str) -> None:
        with self._observability_scope(run_id):
            await self._execute_bound(run_id)

    async def _execute_bound(self, run_id: str) -> None:
        try:
            if self._stop_requested(run_id):
                return
            # 变量说明：delegated_binding 表示当前步骤使用的 delegated_binding 值。
            delegated_binding: dict[str, Any] | None = None
            with database_module.SessionLocal() as db:
                # 变量说明：snapshot 表示当前步骤使用的 snapshot 值。
                snapshot = db.scalar(
                    select(RunEvent)
                    .where(RunEvent.run_id == run_id, RunEvent.event_type == "runtime_snapshot")
                    .order_by(RunEvent.sequence.desc(), RunEvent.id.desc())
                )
                # 变量说明：candidate 表示当前步骤使用的 candidate 值。
                candidate = dict(snapshot.payload or {}).get("runtime_binding") if snapshot is not None else None
                if isinstance(candidate, dict) and candidate.get("delegation_version"):
                    # 变量说明：delegated_binding 表示当前步骤使用的 delegated_binding 值。
                    delegated_binding = dict(candidate)
            # 变量说明：runtime 表示当前步骤使用的 runtime 值；context 表示当前步骤使用的 context 值。
            runtime, context = self._resolve_runtime(
                run_id,
                runtime_binding=delegated_binding,
                coordinator_instance=self,
            )
            await RunRuntimePreparer().prepare(
                run_id=run_id,
                runtime=runtime,
                context=context,
                progress_sink=self._event_sink(run_id),
            )
            self._install_completion_verifier(runtime, context)
            # 变量说明：outcome 表示当前步骤使用的 outcome 值。
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
            # 变量说明：runtime_binding 表示当前步骤使用的 runtime_binding 值。
            outcome.runtime_binding = RunContinuationCodec.capture_runtime_binding(
                context["runtime_binding"], runtime
            )
            self._persist_outcome(
                run_id,
                outcome,
                context["background_store"].delivered_terminal_ids(),
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("Run execution failed")
            self._persist_failure(run_id, exc)

    # 函数职责：异步完成 resume 对应的业务处理。
    # 参数关系：run_id 表示当前运行标识。
    # 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
    async def _resume(self, run_id: str) -> None:
        with self._observability_scope(run_id):
            await self._resume_bound(run_id)

    async def _resume_bound(self, run_id: str) -> None:
        try:
            if self._stop_requested(run_id):
                return
            with database_module.SessionLocal() as db:
                # 变量说明：snapshot 表示当前步骤使用的 snapshot 值。
                snapshot = db.scalar(
                    select(RunEvent)
                    .where(RunEvent.run_id == run_id, RunEvent.event_type == "runtime_snapshot")
                    .order_by(RunEvent.sequence.desc(), RunEvent.id.desc())
                )
                if snapshot is None:
                    raise RuntimeError("缺少可恢复的运行快照")
                # 变量说明：prior 表示当前步骤使用的 prior 值。
                prior = self._outcome_from_snapshot(snapshot.payload)
            if not prior.runtime_binding:
                raise RuntimeError("运行快照缺少冻结配置，已拒绝在可变环境中续跑")
            # 变量说明：runtime 表示当前步骤使用的 runtime 值；context 表示当前步骤使用的 context 值。
            runtime, context = self._resolve_runtime(
                run_id,
                runtime_binding=prior.runtime_binding,
                coordinator_instance=self,
            )
            await RunRuntimePreparer().prepare(
                run_id=run_id,
                runtime=runtime,
                context=context,
                progress_sink=self._event_sink(run_id),
            )
            self._install_completion_verifier(runtime, context)
            if prior.stop_reason == "waiting_background":
                # 变量说明：outcome 表示当前步骤使用的 outcome 值。
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
                # 变量说明：outcome 表示当前步骤使用的 outcome 值。
                outcome = await runtime.resume_after_approval(
                    prior,
                    runtime_context=context,
                )
            # 变量说明：runtime_binding 表示当前步骤使用的 runtime_binding 值。
            outcome.runtime_binding = RunContinuationCodec.capture_runtime_binding(
                context["runtime_binding"], runtime
            )
            self._persist_outcome(
                run_id,
                outcome,
                context["background_store"].delivered_terminal_ids(),
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("Run resume failed")
            self._persist_failure(run_id, exc)

    # 函数职责：异步完成 resume_delegated_child 对应的业务处理。
    # 参数关系：run_id 表示当前运行标识。
    # 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
    async def _resume_delegated_child(self, run_id: str) -> None:
        with self._observability_scope(run_id):
            await self._resume_delegated_child_bound(run_id)

    async def _resume_delegated_child_bound(self, run_id: str) -> None:
        """Let the original coordinator model consume a terminal child result."""

        try:
            if self._stop_requested(run_id):
                return
            with database_module.SessionLocal() as db:
                # 变量说明：snapshot 表示当前步骤使用的 snapshot 值。
                snapshot = db.scalar(
                    select(RunEvent)
                    .where(RunEvent.run_id == run_id, RunEvent.event_type == "runtime_snapshot")
                    .order_by(RunEvent.sequence.desc(), RunEvent.id.desc())
                )
                if snapshot is None:
                    raise RuntimeError("delegated child continuation is missing the parent snapshot")
                # 变量说明：prior 表示当前步骤使用的 prior 值。
                prior = self._outcome_from_snapshot(snapshot.payload)
            if not prior.runtime_binding:
                raise RuntimeError("delegated child continuation is missing the frozen parent binding")
            # 变量说明：runtime 表示当前步骤使用的 runtime 值；context 表示当前步骤使用的 context 值。
            runtime, context = self._resolve_runtime(
                run_id,
                runtime_binding=prior.runtime_binding,
                coordinator_instance=self,
            )
            await RunRuntimePreparer().prepare(
                run_id=run_id,
                runtime=runtime,
                context=context,
                progress_sink=self._event_sink(run_id),
            )
            self._install_completion_verifier(runtime, context)
            # 变量说明：outcome 表示当前步骤使用的 outcome 值。
            outcome = await runtime.resume_after_delegated_child(
                prior,
                runtime_context=context,
            )
            # 变量说明：runtime_binding 表示当前步骤使用的 runtime_binding 值。
            outcome.runtime_binding = RunContinuationCodec.capture_runtime_binding(
                context["runtime_binding"], runtime
            )
            self._persist_outcome(
                run_id,
                outcome,
                context["background_store"].delivered_terminal_ids(),
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("Delegated child resume failed")
            self._persist_failure(run_id, exc)

    # 函数职责：完成 install_completion_verifier 对应的业务处理。
    # 参数关系：runtime 表示当前步骤使用的 runtime 值；context 表示当前步骤使用的 context 值。
    # 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
    @staticmethod
    def _install_completion_verifier(runtime: AgentRuntime, context: Mapping[str, Any]) -> None:
        """Attach the main-agent-only completion gate to a resolved runtime."""

        # 变量说明：binding 表示当前步骤使用的 binding 值。
        binding = context.get("runtime_binding")
        # 变量说明：background_store 表示当前步骤使用的 background_store 值。
        background_store = context.get("background_store")
        # 变量说明：max_completion_verification_attempts 表示当前流程使用的 max_completion_verification_attempts 集合。
        runtime.config.max_completion_verification_attempts = max(
            1,
            int(settings.completion_verification_max_attempts or 1),
        )
        if isinstance(binding, Mapping) and binding.get("delegation_version"):
            # Child agents never produce the user-facing final answer. Their
            # settled task observation is part of the parent's gated trace.
            if background_store is None:
                # 变量说明：completion_verifier 表示当前步骤使用的 completion_verifier 值。
                runtime.completion_verifier = None
            else:
                # 函数职责：完成 verify_child_background 对应的业务处理。
                # 参数关系：_candidate 表示当前步骤使用的 _candidate 值。
                # 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
                def verify_child_background(_candidate: Mapping[str, Any]) -> CompletionDecision:
                    # 变量说明：active 表示当前步骤使用的 active 值。
                    active = background_store.active_jobs()
                    if active:
                        # 变量说明：registered 表示当前步骤使用的 registered 值。
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
                    # 变量说明：terminal_results 表示当前流程使用的 terminal_results 集合。
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

                # 变量说明：completion_verifier 表示当前步骤使用的 completion_verifier 值。
                runtime.completion_verifier = verify_child_background
            return
        if background_store is None:
            # 变量说明：completion_verifier 表示当前步骤使用的 completion_verifier 值。
            runtime.completion_verifier = decide_deterministic_completion
            return

        # 函数职责：完成 verify 对应的业务处理。
        # 参数关系：candidate 表示当前步骤使用的 candidate 值。
        # 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
        def verify(candidate: Mapping[str, Any]):
            # 变量说明：decision 表示当前步骤使用的 decision 值。
            decision = decide_deterministic_completion(candidate)
            # 变量说明：active 表示当前步骤使用的 active 值。
            active = background_store.active_jobs() if background_store is not None else []
            if decision.accepted and active:
                # 变量说明：registered 表示当前步骤使用的 registered 值。
                registered = background_store.register_waiter()
                if registered:
                    # 变量说明：accepted 表示当前步骤使用的 accepted 值。
                    decision.accepted = False
                    # 变量说明：defer_until_event 表示当前步骤使用的 defer_until_event 值。
                    decision.defer_until_event = True
                    # 变量说明：reason 表示当前步骤使用的 reason 值。
                    decision.reason = "后台作业仍在运行；本次执行将暂停，并在终态事件到达后自动恢复。"
                    # 变量说明：report 表示当前步骤使用的 report 值。
                    decision.report = {
                        **dict(decision.report or {}),
                        "background_jobs": [
                            {"id": item["id"], "status": item["status"]} for item in active
                            if item["id"] in registered
                        ],
                    }
                else:
                    # 变量说明：active 表示当前步骤使用的 active 值。
                    active = []
            if decision.accepted and not active and background_store is not None:
                # 变量说明：terminal_results 表示当前流程使用的 terminal_results 集合。
                terminal_results = background_store.observe_terminal_results()
                if terminal_results:
                    # 变量说明：accepted 表示当前步骤使用的 accepted 值。
                    decision.accepted = False
                    # 变量说明：reason 表示当前步骤使用的 reason 值。
                    decision.reason = (
                        "后台作业已结束。请根据以下终态事件更新结论，不需要再次轮询：\n"
                        + json.dumps(terminal_results, ensure_ascii=False)[:8_000]
                    )
                    # 变量说明：report 表示当前步骤使用的 report 值。
                    decision.report = {
                        **dict(decision.report or {}),
                        "background_jobs": [
                            {"id": item["id"], "status": item["status"]} for item in terminal_results
                        ],
                    }
            return decision

        # 变量说明：completion_verifier 表示当前步骤使用的 completion_verifier 值。
        runtime.completion_verifier = verify

    # 函数职责：完成 persist_failure 对应的业务处理。
    # 参数关系：run_id 表示当前运行标识；error 表示当前捕获或准备上报的错误。
    # 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
    @staticmethod
    def _persist_failure(run_id: str, error: BaseException) -> None:
        # 变量说明：parent_bridge_event 表示当前步骤使用的 parent_bridge_event 值。
        parent_bridge_event: dict[str, Any] | None = None
        with database_module.SessionLocal() as db:
            # 变量说明：claim 表示当前步骤使用的 claim 值。
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
                # 变量说明：run 表示当前步骤使用的 run 值。
                run = db.get(Run, run_id)
                if run is not None and RunCoordinator._user_interrupt_locked(run):
                    append_run_event(
                        db,
                        run_id=run_id,
                        event_type="run_failure_discarded",
                        payload={"reason": USER_INTERRUPT_REASON, "error_type": type(error).__name__},
                    )
                    db.commit()
                else:
                    db.rollback()
                RunCoordinator._clear_stream_buffer(run_id, interrupted=True)
                coordinator.unregister_tool_canceller(run_id)
                return
            # 变量说明：run 表示当前步骤使用的 run 值。
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
            # 变量说明：raw_error 表示当前步骤使用的 raw_error 值。
            raw_error = str(error) or type(error).__name__
            # 变量说明：normalized_error_code 表示当前步骤使用的 normalized_error_code 值。
            normalized_error_code = classify_exception(error)
            # 变量说明：safe_error_message 表示当前步骤使用的 safe_error_message 值。
            safe_error_message = public_error_message(normalized_error_code)
            # 变量说明：status 表示当前对象或运行的状态。
            run.status = "failed"
            # 变量说明：error_code 表示当前步骤使用的 error_code 值。
            run.error_code = normalized_error_code
            # 变量说明：error_message 表示当前步骤使用的 error_message 值。
            run.error_message = safe_error_message
            # 变量说明：finished_at 表示finished_at 对应的时间信息。
            run.finished_at = _utcnow()
            transition_run_task(db, run, status="failed")
            append_run_event(
                db,
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
            )
            # 变量说明：parent_bridge_event 表示当前步骤使用的 parent_bridge_event 值。
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
            # 变量说明：error_message 表示当前步骤使用的 error_message 值。
            error_message = run.error_message
        run_stream_broker.publish(run_id, {"type": "integration_failed", "error": error_message})
        if parent_bridge_event is not None and parent_bridge_event.get("parent_run_id"):
            run_stream_broker.publish(str(parent_bridge_event["parent_run_id"]), parent_bridge_event)
        coordinator.launch_parent_continuation_if_queued(parent_bridge_event)
        RunCoordinator._clear_stream_buffer(run_id)
        coordinator.unregister_tool_canceller(run_id)


# 变量说明：coordinator 表示当前步骤使用的 coordinator 值。
coordinator = RunCoordinator()
