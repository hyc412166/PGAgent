"""Persistence bridge between HTTP sessions and the AgentRuntime."""

from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import case, or_, select

from app import database as database_module
from app.config import settings
from app.database import (
    Agent,
    Approval,
    ChatMessage,
    DEFAULT_AGENT_ID,
    DEFAULT_WORKSPACE_ID,
    Memory,
    ModelConnection,
    Run,
    RunEvent,
    Session,
    UsageRecord,
    Workspace,
)
from app.runtime import AgentRuntime, ContextManager, RunOutcome, RuntimeConfig, normalize_usage
from app.runtime.context import estimate_tokens, message_tokens
from app.tools import create_default_registry

from .model_gateway import ModelConfigurationError, ProviderConfig, build_model_call
from .run_stream import run_stream_broker


ACTIVE_STATUSES = {"received", "preparing_context", "planning", "acting", "observing", "running"}


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(item) for item in value]
    if isinstance(value, datetime):
        return value.isoformat()
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _canonical_arguments(arguments: Any) -> str:
    return json.dumps(
        _json_safe(arguments or {}),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _configuration_digest(value: Any) -> str:
    return hashlib.sha256(_canonical_arguments(value).encode("utf-8")).hexdigest()


def approval_matches_pending(approval: Approval, pending: dict[str, Any] | None) -> bool:
    if not pending:
        return False
    return (
        approval.tool_name == str(pending.get("tool_name") or "")
        and _canonical_arguments(approval.arguments) == _canonical_arguments(pending.get("arguments"))
    )


def _message_payload(message: ChatMessage) -> dict[str, Any]:
    payload: dict[str, Any] = {"role": message.role, "content": message.content}
    if message.tool_name:
        payload["name"] = message.tool_name
    if message.tool_call_id:
        payload["tool_call_id"] = message.tool_call_id
    return payload


def _conversation_token_count(summary: str, messages: list[dict[str, Any]]) -> int:
    summary_tokens = estimate_tokens(summary) if summary.strip() else 0
    return summary_tokens + sum(message_tokens(message) for message in messages)


def _prepare_session_history(db: Any, session: Session) -> list[dict[str, Any]]:
    """Return uncompacted history and compact it once the 90% threshold is hit."""

    query = select(ChatMessage).where(ChatMessage.session_id == session.id)
    if session.last_compacted_at is not None:
        query = query.where(ChatMessage.created_at > session.last_compacted_at)
    rows = list(db.scalars(query.order_by(ChatMessage.created_at.asc())))
    messages = [_message_payload(item) for item in rows]
    used_tokens = _conversation_token_count(session.context_summary or "", messages)

    if used_tokens >= settings.compact_threshold_tokens and rows:
        recent_budget = max(1, settings.compact_threshold_tokens // 2)
        keep_from = len(rows)
        kept_tokens = 0
        for index in range(len(rows) - 1, -1, -1):
            cost = message_tokens(messages[index])
            if kept_tokens and kept_tokens + cost > recent_budget:
                break
            if not kept_tokens and cost > recent_budget:
                # Preserve the newest user turn verbatim; ContextManager will
                # safely trim it for the provider if one message exceeds 100k.
                keep_from = index
                kept_tokens = cost
                break
            keep_from = index
            kept_tokens += cost

        # last_compacted_at is the only durable cursor. Never split rows that
        # share its timestamp, otherwise an un-compacted sibling could be lost
        # by the next `created_at > cursor` query.
        while (
            0 < keep_from < len(rows)
            and rows[keep_from - 1].created_at == rows[keep_from].created_at
        ):
            keep_from -= 1

        compacted_rows = rows[:keep_from]
        compacted_messages = messages[:keep_from]
        if compacted_rows:
            summary_source: list[dict[str, Any]] = []
            if session.context_summary:
                summary_source.append({"role": "summary", "content": session.context_summary})
            summary_source.extend(compacted_messages)
            session.context_summary = ContextManager(max_tokens=settings.context_limit_tokens).compact_messages(
                summary_source,
                token_budget=min(12_000, settings.compact_threshold_tokens // 4),
            )
            session.last_compacted_at = compacted_rows[-1].created_at
            rows = rows[keep_from:]
            messages = messages[keep_from:]
            used_tokens = _conversation_token_count(session.context_summary, messages)

    # This is the durable conversation footprint. The provider-facing builder
    # separately guarantees it never emits more than context_limit_tokens.
    session.context_tokens = min(used_tokens, settings.context_limit_tokens)
    return messages


def _explicit_setting(*values: str | None) -> str | None:
    for value in values:
        normalized = (value or "").strip()
        if normalized and normalized != "auto":
            return normalized
    return None


def _fallback_connection(db: Any, *, provider: str | None = None) -> ModelConnection | None:
    query = select(ModelConnection).where(ModelConnection.enabled.is_(True))
    if provider:
        query = query.where(ModelConnection.provider == provider)
    query = query.order_by(
        case(
            (ModelConnection.status.in_(("connected", "healthy", "online")), 0),
            (ModelConnection.last_checked_at.is_not(None), 1),
            else_=2,
        ),
        ModelConnection.last_checked_at.desc(),
        ModelConnection.created_at.asc(),
    )
    return db.scalar(query)


def _effective_connection(db: Any, session: Session | None, agent: Agent) -> ModelConnection | None:
    candidate_ids = [session.model_connection_id if session else None, agent.model_connection_id]
    for connection_id in dict.fromkeys(item for item in candidate_ids if item):
        connection = db.get(ModelConnection, connection_id)
        if connection is not None and connection.enabled:
            return connection
    return _fallback_connection(db)


class RunCoordinator:
    """Launch, persist and resume local runs without blocking HTTP requests."""

    def __init__(self) -> None:
        self.checkpointer: Any | None = None
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._queued_resumes: set[str] = set()
        self._shutting_down = False

    def set_checkpointer(self, checkpointer: Any | None) -> None:
        self.checkpointer = checkpointer
        if checkpointer is not None:
            self._shutting_down = False

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

    async def shutdown(self) -> None:
        self._shutting_down = True
        self._queued_resumes.clear()
        tasks = [task for task in self._tasks.values() if not task.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()

    @staticmethod
    def reconcile_interrupted_runs() -> None:
        """Make crashes visible instead of leaving runs permanently active."""

        with database_module.SessionLocal() as db:
            runs = list(db.scalars(select(Run).where(Run.status.in_(ACTIVE_STATUSES))))
            for run in runs:
                run.status = "stopped"
                run.stop_reason = "interrupted_restart"
                run.error_message = "PGAgent 在运行期间被关闭，可重新发送消息继续任务"
                run.finished_at = _utcnow()
                db.add(RunEvent(run_id=run.id, event_type="run_interrupted", payload={"reason": "process_restart"}))
            db.commit()

    @staticmethod
    def _event_sink(run_id: str):
        status_by_event = {
            "context_prepared": "preparing_context",
            "context_resumed": "acting",
            "model_step_started": "acting",
            "tool_finished": "observing",
        }
        deferred_stream_events = {"approval_requested", "run_completed", "run_stopped", "model_failed"}

        def sink(event: dict[str, Any]) -> None:
            event_type = str(event.get("type") or "runtime_event")
            payload = {key: _json_safe(value) for key, value in event.items() if key != "type"}
            with database_module.SessionLocal() as db:
                run = db.get(Run, run_id)
                if run is None:
                    return
                db.add(RunEvent(run_id=run_id, event_type=event_type, step=payload.get("step"), payload=payload))
                if event_type in status_by_event:
                    run.status = status_by_event[event_type]
                db.commit()
            # Approval and terminal events are published only after the outcome
            # transaction makes their corresponding rows/messages visible.
            if event_type not in deferred_stream_events:
                run_stream_broker.publish(run_id, {"type": event_type, **payload})

        return sink

    @staticmethod
    def _stream_sink(run_id: str):
        def sink(event: dict[str, Any]) -> None:
            run_stream_broker.publish(run_id, _json_safe(event))

        return sink

    @staticmethod
    def _runtime_snapshot(outcome: RunOutcome) -> dict[str, Any]:
        return _json_safe({
            "status": outcome.status,
            "output": outcome.output,
            "messages": outcome.messages,
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
        )

    @staticmethod
    def _resolve_runtime(
        run_id: str,
        *,
        runtime_binding: dict[str, Any] | None = None,
    ) -> tuple[AgentRuntime, dict[str, Any]]:
        with database_module.SessionLocal() as db:
            run = db.get(Run, run_id)
            if run is None:
                raise LookupError("运行不存在")
            session = db.get(Session, run.session_id) if run.session_id else None
            agent_id = run.agent_id or (session.agent_id if session else None) or DEFAULT_AGENT_ID
            agent = db.get(Agent, agent_id) or db.get(Agent, DEFAULT_AGENT_ID)
            if agent is None:
                raise ModelConfigurationError("当前会话没有选择 Agent")
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
            memory_filters = [Memory.scope == "global"]
            if workspace:
                memory_filters.append((Memory.scope == "workspace") & (Memory.scope_id == workspace.id))
            if session:
                memory_filters.append((Memory.scope == "session") & (Memory.scope_id == session.id))
            memories = list(db.scalars(select(Memory).where(or_(*memory_filters)).order_by(Memory.pinned.desc(), Memory.updated_at.desc())))

            frozen_binding = dict(runtime_binding or {})
            if frozen_binding:
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
                provider_config = ProviderConfig(
                    provider=str(frozen_binding["provider"]),
                    base_url=str(frozen_binding["base_url"]),
                    secret_ref=str(frozen_binding["secret_ref"]),
                    model_id=str(frozen_binding["model_id"]),
                    model_connection_id=str(frozen_binding["model_connection_id"]),
                    thinking_level=str(frozen_binding.get("thinking_level") or "auto"),
                    custom_headers=dict(connection.custom_headers or {}),
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
                    "model_connection_id": connection.id,
                    "provider": connection.provider,
                    "base_url": connection.base_url,
                    "secret_ref": connection.secret_ref,
                    "model_id": model_id,
                    "thinking_level": thinking_level,
                    # Header values may contain credentials. Persist only a
                    # digest and reject resume if the live values drift.
                    "custom_headers_digest": _configuration_digest(connection.custom_headers or {}),
                }

            context = {
                "system_prompt": agent.system_prompt,
                # Description is UI metadata. Model behavior is controlled only
                # by system_prompt, and the fixed default Agent has no prompt.
                "agent_instructions": "",
                "workspace_rules": f"仅访问工作区：{workspace_root}",
                "summary": session.context_summary if session else "",
                "memories": [{"content": item.content, "pinned": item.pinned} for item in memories],
                "recent_messages": messages,
                "mode": "auto",
                "workspace_root": workspace_root,
                "provider": provider_config,
                "model_connection_id": provider_config.model_connection_id,
                "runtime_binding": frozen_binding,
            }
            db.commit()

        runtime = AgentRuntime(
            model_call=build_model_call(context["provider"]),
            tool_registry=create_default_registry(context["workspace_root"]),
            context_manager=ContextManager(max_tokens=settings.context_limit_tokens),
            event_sink=RunCoordinator._event_sink(run_id),
            stream_sink=RunCoordinator._stream_sink(run_id),
            config=RuntimeConfig(
                max_steps=settings.max_steps,
                max_tool_calls=settings.max_tool_calls,
                identical_call_limit=settings.max_identical_calls,
                no_progress_limit=settings.no_progress_limit,
                model_timeout_seconds=settings.model_timeout_seconds,
                max_run_seconds=settings.max_run_seconds,
            ),
            checkpointer=coordinator.checkpointer,
        )
        return runtime, context

    @staticmethod
    def _persist_outcome(run_id: str, outcome: RunOutcome) -> None:
        snapshot = RunCoordinator._runtime_snapshot(outcome)
        publish_type = {
            "awaiting_approval": "approval_requested",
            "completed": "run_completed",
            "stopped": "run_stopped",
            "failed": "model_failed",
        }.get(outcome.status)
        publish_event = next(
            (
                _json_safe(event)
                for event in reversed(outcome.events)
                if event.get("type") == publish_type
            ),
            None,
        )
        persisted = False
        with database_module.SessionLocal() as db:
            run = db.get(Run, run_id)
            if run is None:
                return
            run.status = outcome.status
            run.current_step = outcome.steps
            run.tool_calls = outcome.tool_calls
            run.stop_reason = outcome.stop_reason
            run.error_message = outcome.error
            if outcome.status in {"completed", "failed", "stopped"}:
                run.finished_at = _utcnow()
            db.add(RunEvent(run_id=run_id, event_type="runtime_snapshot", payload=snapshot))

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

            if outcome.status == "awaiting_approval" and outcome.pending_approval:
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
            elif outcome.status == "completed" and outcome.output and run.session_id:
                db.add(ChatMessage(session_id=run.session_id, role="assistant", content=outcome.output, extra={"run_id": run_id}))
                db.flush()

            if run.session_id:
                session = db.get(Session, run.session_id)
                if session is not None:
                    session.updated_at = _utcnow()
                    _prepare_session_history(db, session)
            db.commit()
            persisted = True
        if persisted and publish_event is not None:
            run_stream_broker.publish(run_id, publish_event)

    async def _execute(self, run_id: str) -> None:
        try:
            runtime, context = self._resolve_runtime(run_id)
            outcome = await runtime.run(
                system_prompt=context["system_prompt"],
                agent_instructions=context["agent_instructions"],
                workspace_rules=context["workspace_rules"],
                summary=context["summary"],
                memories=context["memories"],
                recent_messages=context["recent_messages"],
                mode=context["mode"],
                thread_id=run_id,
            )
            outcome.runtime_binding = dict(context["runtime_binding"])
            self._persist_outcome(run_id, outcome)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._persist_failure(run_id, exc)

    async def _resume(self, run_id: str) -> None:
        try:
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
            runtime, context = self._resolve_runtime(run_id, runtime_binding=prior.runtime_binding)
            outcome = await runtime.resume_after_approval(prior, thread_id=run_id)
            outcome.runtime_binding = dict(context["runtime_binding"])
            self._persist_outcome(run_id, outcome)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._persist_failure(run_id, exc)

    @staticmethod
    def _persist_failure(run_id: str, error: BaseException) -> None:
        with database_module.SessionLocal() as db:
            run = db.get(Run, run_id)
            if run is None:
                return
            run.status = "failed"
            run.error_code = type(error).__name__
            run.error_message = str(error) or type(error).__name__
            run.finished_at = _utcnow()
            db.add(RunEvent(run_id=run_id, event_type="integration_failed", payload={"error": run.error_message}))
            db.commit()
            error_message = run.error_message
        run_stream_broker.publish(run_id, {"type": "integration_failed", "error": error_message})


coordinator = RunCoordinator()
