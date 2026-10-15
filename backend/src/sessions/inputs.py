"""会话输入收件箱：接收、运行内消费和下一轮调度共享同一份持久状态。"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from fastapi import HTTPException
from sqlalchemy import select, update

from src.persistence import database
from src.persistence.base import new_id
from src.persistence.models import Artifact, ChatMessage, Run, Session, SessionInput, next_chat_message_sequence
from src.sessions.delivery import is_terminal_delivery
from src.attachments.storage import attachment_message_content


def lock_session(db: Any, session_id: str) -> Session:
    # 同一会话的接收与领取串行执行；两个浏览器同时提交时不能各自启动一个 Run。
    claimed = db.execute(update(Session).where(Session.id == session_id).values(updated_at=Session.updated_at))
    if not claimed.rowcount:
        raise HTTPException(404, "Session not found")
    return db.get(Session, session_id)


def active_root_run(db: Any, session_id: str) -> Run | None:
    runs = db.scalars(select(Run).where(Run.session_id == session_id, Run.turn_id.is_not(None))
                      .order_by(Run.started_at.desc(), Run.id.desc()))
    return next((run for run in runs if not is_terminal_delivery(run.status, run.stop_reason)), None)


def require_steer_target(db: Any, session_id: str, run_id: str | None) -> Run:
    run = db.get(Run, run_id) if run_id else None
    if (run is None or run.session_id != session_id or run.turn_id is None
            or run.input_closed or is_terminal_delivery(run.status, run.stop_reason)):
        raise HTTPException(409, "当前运行已结束或不再接收补充，请重新选择发送方式。")
    return run


def input_view(item: SessionInput) -> dict[str, Any]:
    return {key: getattr(item, key) for key in (
        "id", "session_id", "content", "attachments", "mode", "status", "run_id", "error", "created_at",
    )}


def reconcile_inputs(db: Any, session_id: str) -> None:
    # 停止、失败、进程重启都保留原文，并明确说明未消费的补充不会再被执行。
    for item in db.scalars(select(SessionInput).where(
        SessionInput.session_id == session_id, SessionInput.status == "pending",
    )):
        run = db.get(Run, item.run_id)
        if run is None or is_terminal_delivery(run.status, run.stop_reason):
            item.status = "failed"
            item.error = "运行已结束，这条补充尚未送入模型。"


def consume_steering(run_id: str, state: dict[str, Any], finishing: bool) -> list[dict[str, Any]]:
    """在模型请求边界消费；最后一次空检查与关闭接收在同一事务内完成。"""
    from src.context.compaction import (
        _append_runtime_transcript, _compaction_source_is_current, _persist_conversation_compaction,
    )

    with database.SessionLocal() as db:
        run = db.get(Run, run_id)
        if run is None or not run.session_id or not run.turn_id:
            return []
        lock_session(db, run.session_id)
        db.refresh(run)
        if is_terminal_delivery(run.status, run.stop_reason):
            return []
        items = list(db.scalars(select(SessionInput).where(
            SessionInput.run_id == run_id, SessionInput.status == "pending",
        ).order_by(SessionInput.created_at, SessionInput.id)))
        if not items:
            if finishing:
                run.input_closed = True
            db.commit()
            return []

        # 先落盘已完成的 assistant/tool 前缀，再插入 steer，保证恢复后的历史顺序与模型所见一致。
        prefix = SimpleNamespace(
            messages=state.get("messages", []), transcript_delta=state.get("transcript_delta", []),
            artifact_refs=state.get("context_artifact_refs", []), output=None,
            compaction_state=state.get("compaction_state", {}), events=state.get("events", []),
        )
        session = db.get(Session, run.session_id)
        source_valid = _compaction_source_is_current(db, session, prefix)
        _append_runtime_transcript(db, run_id, run.session_id, prefix, terminal_managed=True)
        db.flush()
        _persist_conversation_compaction(db, session, run_id, prefix, source_sequence_valid=source_valid)
        sequence = next_chat_message_sequence(db, run.session_id)
        messages = []
        for item in items:
            for ref in item.attachments:
                db.get(Artifact, ref["id"]).status = "available"
            db.add(ChatMessage(
                session_id=run.session_id, turn_id=run.turn_id, role="user", content=item.content,
                sequence=sequence, message_kind="steer",
                extra={"input_id": item.id, "input_mode": "steer", "attachments": item.attachments},
                provider_payload={"attachment_refs": item.attachments},
            ))
            messages.append({"role": "user", "content": attachment_message_content(item.content, item.attachments)})
            item.status = "delivered"
            sequence += 1
        db.commit()
        # 前缀已经持久化，后续压缩/恢复只记录新的增量，避免附件用户消息被重复序列化。
        state["transcript_delta"] = []
        state["context"] = {**state.get("context", {}), "context_sequence": sequence - 1}
        return messages


async def dispatch_follow_up(coordinator: Any, session_id: str, input_id: str | None = None) -> None:
    """只有正常完成才自动领取；停止/失败后的队列需要显式 start。"""
    from fastapi import Response
    from src.api.runtime import SessionRunRequest, _launch_session_run_core

    with database.SessionLocal() as db:
        lock_session(db, session_id)
        if active_root_run(db, session_id) is not None:
            if input_id:
                raise HTTPException(409, "当前任务尚未结束")
            return
        latest = db.scalar(select(Run).where(Run.session_id == session_id, Run.turn_id.is_not(None))
                           .order_by(Run.started_at.desc(), Run.id.desc()))
        if not input_id and latest is not None and latest.status != "completed":
            return
        query = select(SessionInput).where(SessionInput.session_id == session_id, SessionInput.status == "queued")
        head = db.scalar(query.order_by(SessionInput.created_at, SessionInput.id))
        if input_id:
            item = db.get(SessionInput, input_id)
            if item is None or item.session_id != session_id:
                raise HTTPException(404, "消息不存在")
            if item.status == "delivered":
                return
            if item.status not in {"queued", "failed"}:
                raise HTTPException(409, "这条消息已发送或已撤回")
            if head is not None and head.id != input_id:
                raise HTTPException(409, "请先处理队列中更早的消息")
        else:
            item = head
        if item is None:
            if input_id:
                raise HTTPException(409, "这条消息已发送或已撤回")
            return
        item.mode, item.error = "follow_up", None
        # 输入收件回执保持不变；显式重试产生新一轮，避免复用已经失败的 Run。
        await _launch_session_run_core(session_id, SessionRunRequest(
            content=item.content, idempotency_key=new_id(),
        ), Response(), db, queued_input=item)
