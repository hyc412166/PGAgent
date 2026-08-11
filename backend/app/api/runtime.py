"""Endpoints that launch and resume real agent executions."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field
from sse_starlette.sse import EventSourceResponse
from sqlalchemy import select, update
from sqlalchemy.orm import Session as OrmSession

from app import database as database_module
from app.config import settings
from app.database import (
    DEFAULT_AGENT_ID,
    DEFAULT_WORKSPACE_ID,
    Agent,
    Approval,
    ChatMessage,
    Run,
    RunEvent,
    Session,
    Workspace,
    get_db,
)
from app.schemas import ApprovalRead, RunRead
from app.services.run_service import (
    ACTIVE_STATUSES,
    _prepare_session_history,
    approval_matches_pending,
    coordinator,
)
from app.services.run_stream import TERMINAL_EVENT_TYPES, run_stream_broker


router = APIRouter(prefix="/api", tags=["runtime"])


class SessionRunRequest(BaseModel):
    content: str = Field(min_length=1, max_length=100_000)


class SessionContextRead(BaseModel):
    used_tokens: int
    limit_tokens: int
    compact_threshold_tokens: int
    percent: float
    last_compacted_at: datetime | None


class ApprovalRuntimeDecision(BaseModel):
    decision: Literal["approve", "reject"]
    reason: str | None = None


@router.post("/sessions/{session_id}/run", response_model=RunRead, status_code=status.HTTP_202_ACCEPTED)
async def launch_session_run(
    session_id: str, payload: SessionRunRequest, db: OrmSession = Depends(get_db)
) -> Run:
    chat_session = db.get(Session, session_id)
    if chat_session is None:
        raise HTTPException(status_code=404, detail="Session not found")
    agent = db.get(Agent, chat_session.agent_id or DEFAULT_AGENT_ID)
    if agent is None:
        agent = db.get(Agent, DEFAULT_AGENT_ID)
    if agent is None:
        raise HTTPException(status_code=409, detail="请先为会话选择 Agent")
    workspace_id = chat_session.workspace_id or agent.workspace_id or DEFAULT_WORKSPACE_ID
    if not workspace_id or db.get(Workspace, workspace_id) is None:
        raise HTTPException(status_code=409, detail="请先为会话或 Agent 选择有效工作区")
    active = db.scalar(select(Run).where(Run.session_id == session_id, Run.status.in_(ACTIVE_STATUSES | {"awaiting_approval"})))
    if active is not None:
        raise HTTPException(status_code=409, detail="当前会话已有运行或待审批工具，请先处理后再发送")

    mode = "auto"
    message = ChatMessage(session_id=session_id, role="user", content=payload.content.strip(), extra={"mode": mode})
    run = Run(session_id=session_id, workspace_id=workspace_id, agent_id=agent.id, mode=mode, status="received")
    db.add_all([message, run])
    chat_session.updated_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(run)
    coordinator.launch(run.id)
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
            "terminal": run.status in {"completed", "failed", "stopped"},
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
                if str(event.get("type") or "") in TERMINAL_EVENT_TYPES:
                    return
            if initial_state["terminal"]:
                return
            while not await request.is_disconnected():
                event = await subscription.queue.get()
                yield encode(event)
                if str(event.get("type") or "") in TERMINAL_EVENT_TYPES:
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
    return SessionContextRead(
        used_tokens=used_tokens,
        limit_tokens=settings.context_limit_tokens,
        compact_threshold_tokens=settings.compact_threshold_tokens,
        percent=round((used_tokens / settings.context_limit_tokens) * 100, 2),
        last_compacted_at=chat_session.last_compacted_at,
    )


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
    return approval
