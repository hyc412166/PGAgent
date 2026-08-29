"""Endpoints that launch and resume real agent executions."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, Field
from sse_starlette.sse import EventSourceResponse
from sqlalchemy import select, text, update
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session as OrmSession

from src.persistence import database as database_module
from src.config import settings
from src.persistence.database import (
    DEFAULT_AGENT_ID,
    DEFAULT_WORKSPACE_ID,
    Agent,
    Approval,
    ConversationCompaction,
    DraftLaunch,
    ModelConnection,
    Run,
    RunEvent,
    Session,
    Workspace,
    get_db,
)
from src.api.schemas import (
    ApprovalRead,
    DraftLaunchRead,
    DraftLaunchRequest,
    RunRead,
    SessionRead,
    WorkspaceRead,
)
from src.runs.service import (
    ACTIVE_STATUSES,
    _prepare_session_history,
    approval_matches_pending,
    coordinator,
)
from src.runs.stream import TERMINAL_EVENT_TYPES, run_stream_broker
from src.skills.registry import replace_session_skills, validate_skill_ids
from src.tasks.state import (
    bind_recovery_task,
    cancel_durable_task,
    is_task_cancellation_request,
    latest_resumable_task,
)
from src.sessions.delivery import (
    find_turn_by_client_message,
    is_terminal_delivery,
    persist_terminal_response,
    request_fingerprint,
    run_for_turn,
    stage_user_turn,
)


router = APIRouter(prefix="/api", tags=["runtime"])


def _stream_event_is_terminal(event: dict) -> bool:
    event_type = str(event.get("type") or "")
    if event_type not in TERMINAL_EVENT_TYPES:
        return False
    if event_type in {"run_stopped", "stopped"}:
        return is_terminal_delivery(
            "stopped",
            str(event.get("stop_reason") or event.get("reason") or "") or None,
        )
    return True


class SessionRunRequest(BaseModel):
    content: str = Field(min_length=1, max_length=100_000)
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=255)


class SessionContextRead(BaseModel):
    used_tokens: int
    limit_tokens: int
    compact_threshold_tokens: int
    percent: float
    last_compaction_at: datetime | None


class ApprovalRuntimeDecision(BaseModel):
    decision: Literal["approve", "reject"]
    reason: str | None = None


class StopRunRequest(BaseModel):
    # Keep the public API intentionally narrow.  Runtime safety/guard stops
    # are produced by the engine itself; clients may only request a user stop.
    reason: Literal["user_interrupted"] = "user_interrupted"


def _normalized_workspace_root(root_path: str) -> str:
    """Canonicalize a selected project directory before comparing it."""

    return str(Path(root_path).expanduser().resolve())


def _workspace_name_from_root(root_path: str) -> str:
    name = Path(root_path).name.strip()
    return name or "项目"


def _begin_draft_transaction(db: OrmSession) -> None:
    """Serialize local SQLite draft launches before checking project roots.

    ``root_path`` has no legacy database uniqueness constraint.  Taking an
    immediate write lock before the lookup makes two distinct first-send
    requests for the same normalized path observe one project row instead of
    racing to insert two.  Other database engines retain their normal
    transaction semantics and the draft idempotency unique key still protects
    retry requests.
    """

    if db.get_bind().dialect.name != "sqlite":
        return
    try:
        db.execute(text("BEGIN IMMEDIATE"))
    except OperationalError as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Draft launch is busy; retry with the same idempotency key",
        ) from exc


def _draft_request_fingerprint(payload: DraftLaunchRequest, normalized_root: str | None) -> str:
    """Bind an idempotency key to its exact materialisation request."""

    canonical = {
        "title": payload.title,
        "content": payload.content,
        "root_path": normalized_root,
        "model_connection_id": payload.model_connection_id,
        "model_id": payload.model_id,
        "thinking_level": payload.thinking_level,
        "permission_mode": payload.permission_mode,
        "use_memories": payload.use_memories,
        "skill_ids": sorted({skill_id.strip() for skill_id in payload.skill_ids}),
    }
    encoded = json.dumps(canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _workspace_for_root(db: OrmSession, normalized_root: str) -> Workspace | None:
    """Find an existing project while repairing legacy non-canonical paths."""

    for workspace in db.scalars(select(Workspace).order_by(Workspace.created_at.asc(), Workspace.id.asc())):
        if _normalized_workspace_root(workspace.root_path) == normalized_root:
            workspace.root_path = normalized_root
            return workspace
    return None


def _draft_launch_response(
    record: DraftLaunch,
    *,
    expected_fingerprint: str,
    db: OrmSession,
    reused: bool,
) -> DraftLaunchRead:
    if record.request_fingerprint != expected_fingerprint:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Idempotency key was already used with a different draft payload",
        )
    chat_session = db.get(Session, record.session_id) if record.session_id else None
    run = db.get(Run, record.run_id) if record.run_id else None
    if chat_session is None or run is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="The resources for this draft launch are no longer available",
        )
    workspace_id = record.workspace_id or chat_session.workspace_id
    workspace = db.get(Workspace, workspace_id) if workspace_id else None
    return DraftLaunchRead(
        workspace=WorkspaceRead.model_validate(workspace) if workspace is not None else None,
        session=SessionRead.model_validate(chat_session),
        run=RunRead.model_validate(run),
        reused=reused,
    )


def _mark_unscheduled_run(
    db: OrmSession,
    run: Run,
    error: BaseException | None = None,
) -> None:
    """Persist a terminal outcome if the post-commit coordinator cannot start."""

    run.status = "failed"
    run.error_code = "launch_unavailable"
    run.error_message = "任务已经保存，但本地执行器未能启动。"
    run.finished_at = datetime.now(timezone.utc)
    event_payload = {"error_type": "launch_unavailable", "phase": "launch"}
    if error is not None:
        event_payload.update({
            "exception_type": type(error).__name__,
            # Private diagnostic evidence. Public event serializers omit this
            # field, and the user-facing reply remains deterministic.
            "internal_error": (str(error) or type(error).__name__)[:20_000],
        })
    db.add(RunEvent(
        run_id=run.id,
        event_type="integration_failed",
        payload=event_payload,
    ))
    persist_terminal_response(
        db,
        run,
        error_code=run.error_code,
        error_message=run.error_message,
    )
    db.commit()


@router.post("/drafts/launch", response_model=DraftLaunchRead, status_code=status.HTTP_202_ACCEPTED)
async def launch_draft(
    payload: DraftLaunchRequest,
    response: Response,
    db: OrmSession = Depends(get_db),
) -> DraftLaunchRead:
    """Atomically turn a client-only draft into its first persisted run.

    The browser may retry a request after a navigation or transport failure.
    All rows are created in one transaction and the coordinator is scheduled
    only after that transaction commits, so reusing the same key is a pure
    read of the original launch rather than another side effect.
    """

    normalized_root = _normalized_workspace_root(payload.root_path) if payload.root_path else None
    fingerprint = _draft_request_fingerprint(payload, normalized_root)
    try:
        _begin_draft_transaction(db)
        existing = db.scalar(
            select(DraftLaunch).where(DraftLaunch.idempotency_key == payload.idempotency_key)
        )
        if existing is not None:
            result = _draft_launch_response(
                existing,
                expected_fingerprint=fingerprint,
                db=db,
                reused=True,
            )
            db.commit()
            response.status_code = status.HTTP_200_OK
            return result

        agent = db.get(Agent, DEFAULT_AGENT_ID)
        if agent is None or not agent.enabled:
            raise HTTPException(status_code=409, detail="PGAgent main coordinator is unavailable")
        if payload.model_connection_id is not None:
            connection = db.get(ModelConnection, payload.model_connection_id)
            if connection is None:
                raise HTTPException(status_code=409, detail="Selected model connection does not exist")
            if not connection.enabled:
                raise HTTPException(status_code=409, detail="Selected model connection is disabled")
        # Validate before any workspace/session/message/run row is staged so
        # an invalid or disabled Skill leaves an unsent draft fully ephemeral.
        skill_ids = validate_skill_ids(db, payload.skill_ids)

        if normalized_root is None:
            workspace = db.get(Workspace, DEFAULT_WORKSPACE_ID)
            if workspace is None or not workspace.enabled:
                raise HTTPException(status_code=409, detail="The default task workspace is unavailable")
        else:
            workspace = _workspace_for_root(db, normalized_root)
            if workspace is None:
                workspace = Workspace(
                    name=_workspace_name_from_root(normalized_root),
                    description="",
                    root_path=normalized_root,
                    enabled=True,
                )
                db.add(workspace)
                db.flush()

        chat_session = Session(
            title=payload.title,
            workspace_id=workspace.id,
            agent_id=DEFAULT_AGENT_ID,
            model_connection_id=payload.model_connection_id,
            model_id=payload.model_id,
            thinking_level=payload.thinking_level,
            permission_mode=payload.permission_mode,
            use_memories=payload.use_memories,
            status="active",
        )
        db.add(chat_session)
        db.flush()
        replace_session_skills(db, chat_session, skill_ids)
        _turn, message, run = stage_user_turn(
            db,
            session_id=chat_session.id,
            workspace_id=workspace.id,
            agent_id=DEFAULT_AGENT_ID,
            content=payload.content,
            mode="auto",
            client_message_id=payload.idempotency_key,
            fingerprint=fingerprint,
            message_extra={"mode": "auto", "source": "draft_launch"},
        )
        record = DraftLaunch(
            idempotency_key=payload.idempotency_key,
            request_fingerprint=fingerprint,
            workspace_id=workspace.id,
            session_id=chat_session.id,
            run_id=run.id,
        )
        db.add(record)
        db.commit()
    except HTTPException:
        db.rollback()
        raise
    except IntegrityError as exc:
        # A non-SQLite deployment can still race the idempotency unique key.
        # Its losing transaction is wholly rolled back before returning the
        # winning launch, so no partial workspace/session/message/run remains.
        db.rollback()
        existing = db.scalar(
            select(DraftLaunch).where(DraftLaunch.idempotency_key == payload.idempotency_key)
        )
        if existing is not None:
            result = _draft_launch_response(
                existing,
                expected_fingerprint=fingerprint,
                db=db,
                reused=True,
            )
            db.commit()
            response.status_code = status.HTTP_200_OK
            return result
        raise HTTPException(status_code=409, detail="Draft launch could not be persisted") from exc
    except Exception:
        db.rollback()
        raise

    result = _draft_launch_response(record, expected_fingerprint=fingerprint, db=db, reused=False)
    try:
        scheduled = coordinator.launch(run.id)
    except Exception as exc:
        _mark_unscheduled_run(db, run, exc)
        return _draft_launch_response(record, expected_fingerprint=fingerprint, db=db, reused=False)
    if not scheduled:
        _mark_unscheduled_run(db, run)
        return _draft_launch_response(record, expected_fingerprint=fingerprint, db=db, reused=False)
    return result


@router.post("/sessions/{session_id}/run", response_model=RunRead, status_code=status.HTTP_202_ACCEPTED)
async def launch_session_run(
    session_id: str,
    payload: SessionRunRequest,
    response: Response,
    db: OrmSession = Depends(get_db),
) -> Run:
    chat_session = db.get(Session, session_id)
    if chat_session is None:
        raise HTTPException(status_code=404, detail="Session not found")
    content = payload.content.strip()
    if not content:
        raise HTTPException(status_code=422, detail="消息内容不能为空")
    client_message_id = payload.idempotency_key.strip() if payload.idempotency_key else None
    fingerprint = request_fingerprint(content)
    existing_turn = find_turn_by_client_message(
        db,
        session_id=session_id,
        client_message_id=client_message_id,
    )
    if existing_turn is not None:
        if existing_turn.request_fingerprint != fingerprint:
            raise HTTPException(status_code=409, detail="该消息标识已用于不同内容")
        existing_run = run_for_turn(db, existing_turn.id)
        if existing_run is None:
            raise HTTPException(status_code=409, detail="已接收消息对应的运行记录不存在")
        response.status_code = status.HTTP_200_OK
        return existing_run
    # A session always runs through the fixed PGAgent coordinator. This also
    # repairs a legacy session lazily if it predates the coordinator migration.
    agent = db.get(Agent, DEFAULT_AGENT_ID)
    if agent is None:
        raise HTTPException(status_code=409, detail="PGAgent 主控不可用")
    chat_session.agent_id = DEFAULT_AGENT_ID
    workspace_id = chat_session.workspace_id or agent.workspace_id or DEFAULT_WORKSPACE_ID
    if not workspace_id or db.get(Workspace, workspace_id) is None:
        raise HTTPException(status_code=409, detail="请先为会话或 Agent 选择有效工作区")
    cancellation_request = is_task_cancellation_request(content)
    if cancellation_request:
        task = latest_resumable_task(db, session_id)
        if task is not None:
            linked_run_ids = list(db.scalars(
                select(Run.id).where(Run.task_id == task.id).order_by(Run.started_at.desc())
            ))
            for linked_run_id in linked_run_ids:
                coordinator.stop(linked_run_id, db=db, reason="user_interrupted")
            cancel_durable_task(db, task)
            db.commit()

    active = db.scalar(select(Run).where(Run.session_id == session_id, Run.status.in_(ACTIVE_STATUSES | {"awaiting_approval"})))
    if active is not None:
        raise HTTPException(status_code=409, detail="当前会话已有运行或待审批工具，请先处理后再发送")

    mode = "auto"
    _turn, _message, run = stage_user_turn(
        db,
        session_id=session_id,
        workspace_id=workspace_id,
        agent_id=agent.id,
        content=content,
        mode=mode,
        client_message_id=client_message_id,
        fingerprint=fingerprint,
        message_extra={"mode": mode},
    )
    if not cancellation_request:
        bind_recovery_task(db, run, content)
    chat_session.updated_at = datetime.now(timezone.utc)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        existing_turn = find_turn_by_client_message(
            db,
            session_id=session_id,
            client_message_id=client_message_id,
        )
        if existing_turn is None or existing_turn.request_fingerprint != fingerprint:
            raise HTTPException(status_code=409, detail="消息接收发生冲突") from exc
        existing_run = run_for_turn(db, existing_turn.id)
        if existing_run is None:
            raise HTTPException(status_code=409, detail="已接收消息对应的运行记录不存在") from exc
        response.status_code = status.HTTP_200_OK
        return existing_run
    db.refresh(run)
    try:
        scheduled = coordinator.launch(run.id)
    except Exception as exc:
        _mark_unscheduled_run(db, run, exc)
        db.refresh(run)
        return run
    if not scheduled:
        _mark_unscheduled_run(db, run)
        db.refresh(run)
    return run


@router.get("/runs/{run_id}/stream")
async def stream_run(run_id: str, request: Request) -> EventSourceResponse:
    """Stream transient JSON events for one local run using SSE."""

    with database_module.SessionLocal() as db:
        run = db.get(Run, run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="Run not found")
        initial_state = {
            "type": "run_state",
            "run_id": run.id,
            "status": run.status,
            "current_step": run.current_step,
            "tool_calls": run.tool_calls,
            "stop_reason": run.stop_reason,
            "error_code": run.error_code,
            "error": run.error_message,
            "terminal": is_terminal_delivery(run.status, run.stop_reason),
        }

    replay, subscription = run_stream_broker.subscribe(run_id)
    last_event_id = request.headers.get("last-event-id")
    if last_event_id:
        for index, event in enumerate(replay):
            if str(event.get("event_id") or "") == last_event_id:
                replay = replay[index + 1 :]
                break

    def encode(event: dict) -> dict[str, str]:
        encoded: dict[str, str] = {
            "event": str(event.get("type") or "message"),
            "data": json.dumps(event, ensure_ascii=False, separators=(",", ":")),
        }
        event_id = event.get("event_id")
        if event_id is not None:
            encoded["id"] = str(event_id)
        return encoded

    async def events():  # type: ignore[no-untyped-def]
        try:
            yield encode(initial_state)
            for event in replay:
                yield encode(event)
                if _stream_event_is_terminal(event):
                    return
            if initial_state["terminal"]:
                return
            while not await request.is_disconnected():
                event = await subscription.queue.get()
                yield encode(event)
                if _stream_event_is_terminal(event):
                    return
        finally:
            run_stream_broker.unsubscribe(subscription)

    return EventSourceResponse(
        events(),
        ping=15,
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/sessions/{session_id}/context", response_model=SessionContextRead)
def session_context(session_id: str, db: OrmSession = Depends(get_db)) -> SessionContextRead:
    chat_session = db.get(Session, session_id)
    if chat_session is None:
        raise HTTPException(status_code=404, detail="Session not found")
    _prepare_session_history(db, chat_session)
    db.commit()
    used_tokens = max(0, min(int(chat_session.context_tokens or 0), settings.context_limit_tokens))
    latest_compaction = db.scalar(
        select(ConversationCompaction)
        .where(ConversationCompaction.session_id == session_id)
        .order_by(ConversationCompaction.source_sequence.desc(), ConversationCompaction.created_at.desc())
    )
    return SessionContextRead(
        used_tokens=used_tokens,
        limit_tokens=settings.context_limit_tokens,
        compact_threshold_tokens=settings.compact_threshold_tokens,
        percent=round((used_tokens / settings.context_limit_tokens) * 100, 2),
        last_compaction_at=latest_compaction.created_at if latest_compaction is not None else None,
    )


@router.post("/runs/{run_id}/stop", response_model=RunRead)
async def stop_run(
    run_id: str,
    payload: StopRunRequest | None = None,
    db: OrmSession = Depends(get_db),
) -> Run:
    """Interrupt a running model/tool loop at the user's request.

    The coordinator commits the terminal stop marker before cancelling its
    asyncio task.  Repeating the request is safe: an already terminal run is
    returned unchanged and a completed/failed run is never rewritten as an
    interruption.
    """

    run = coordinator.stop(run_id, db=db, reason=(payload.reason if payload else "user_interrupted"))
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found")
    return run


@router.post("/approvals/{approval_id}/decide", response_model=ApprovalRead)
async def decide_and_resume(
    approval_id: str, payload: ApprovalRuntimeDecision, db: OrmSession = Depends(get_db)
) -> Approval:
    approval = db.get(Approval, approval_id)
    if approval is None:
        raise HTTPException(status_code=404, detail="Approval not found")
    if approval.status != "pending":
        raise HTTPException(status_code=409, detail="该审批已经处理")
    run = db.get(Run, approval.run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found")
    if run.status != "awaiting_approval":
        raise HTTPException(status_code=409, detail="该运行当前不在等待审批状态")
    snapshot = db.scalar(
        select(RunEvent)
        .where(RunEvent.run_id == run.id, RunEvent.event_type == "runtime_snapshot")
        .order_by(RunEvent.created_at.desc(), RunEvent.id.desc())
    )
    snapshot_payload = snapshot.payload if snapshot is not None and isinstance(snapshot.payload, dict) else {}
    pending = snapshot_payload.get("pending_approval")
    if snapshot_payload.get("status") != "awaiting_approval" or not approval_matches_pending(approval, pending):
        raise HTTPException(status_code=409, detail="审批记录与最新运行快照不匹配，已拒绝继续执行")

    original_approval_reason = approval.reason
    decided_at = datetime.now(timezone.utc)
    approval_status = "approved" if payload.decision == "approve" else "rejected"
    parent_bridge_event: dict[str, object] | None = None
    approval_update = db.execute(
        update(Approval)
        .where(Approval.id == approval.id, Approval.status == "pending")
        .values(status=approval_status, reason=payload.reason, decided_at=decided_at)
    )
    if approval_update.rowcount != 1:
        db.rollback()
        raise HTTPException(status_code=409, detail="该审批已被其他请求处理")
    if payload.decision == "reject":
        run_values = {
            "status": "stopped",
            "stop_reason": "approval_rejected",
            "error_code": "approval_rejected",
            "error_message": "所需操作没有获得批准，任务已停止。",
            "finished_at": decided_at,
        }
        db.add(RunEvent(run_id=run.id, event_type="approval_rejected", payload={"approval_id": approval.id}))
    else:
        run_values = {"status": "received", "finished_at": None}
    run_update = db.execute(
        update(Run)
        .where(Run.id == run.id, Run.status == "awaiting_approval")
        .values(**run_values)
    )
    if run_update.rowcount != 1:
        db.rollback()
        raise HTTPException(status_code=409, detail="运行状态已变更，未执行审批决定")
    if payload.decision == "reject":
        # A delegated child is linked to the parent session for approval
        # visibility.  Rejection must settle the child task in this same
        # transaction; otherwise the delegation record would remain falsely active.
        db.refresh(run)
        parent_bridge_event = coordinator.reconcile_delegated_child_terminal(
            db,
            run,
            status="stopped",
            stop_reason="approval_rejected",
            error=payload.reason or "Child approval was rejected",
            error_code="approval_rejected",
        )
        persist_terminal_response(
            db,
            run,
            error_code="approval_rejected",
            error_message=run.error_message,
        )
    db.commit()
    approval = db.get(Approval, approval_id)
    if approval is None:
        raise HTTPException(status_code=404, detail="Approval not found")
    if payload.decision == "approve":
        if not coordinator.launch(run.id, resume=True):
            db.execute(
                update(Approval)
                .where(Approval.id == approval.id, Approval.status == "approved")
                .values(status="pending", reason=original_approval_reason, decided_at=None)
            )
            db.execute(
                update(Run)
                .where(Run.id == run.id, Run.status == "received")
                .values(status="awaiting_approval")
            )
            db.commit()
            raise HTTPException(status_code=503, detail="续跑任务暂时无法启动，请重试审批")
    else:
        run_stream_broker.publish(run.id, {
            "type": "run_stopped",
            "code": "approval_rejected",
            "reason": payload.reason or "approval rejected",
        })
        if parent_bridge_event is not None and parent_bridge_event.get("parent_run_id"):
            run_stream_broker.publish(str(parent_bridge_event["parent_run_id"]), parent_bridge_event)
        coordinator.launch_parent_continuation_if_queued(parent_bridge_event)
    return approval
