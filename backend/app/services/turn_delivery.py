"""Durable, model-independent delivery for accepted conversation turns."""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select

from app.database import (
    ChatMessage,
    ConversationTurn,
    DelegatedTask,
    Run,
    RunEvent,
    next_chat_message_sequence,
)


TERMINAL_RUN_STATUSES = frozenset({"completed", "failed", "stopped"})
NON_TERMINAL_STOP_REASONS = frozenset({
    "delegated_child_awaiting_approval",
    "delegated_child_waiting_event",
    "waiting_background",
})


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def request_fingerprint(content: str) -> str:
    normalized = content.strip().encode("utf-8")
    return hashlib.sha256(normalized).hexdigest()


def is_terminal_delivery(status: str, stop_reason: str | None = None) -> bool:
    return status in TERMINAL_RUN_STATUSES and not (
        status == "stopped" and stop_reason in NON_TERMINAL_STOP_REASONS
    )


def find_turn_by_client_message(
    db: Any,
    *,
    session_id: str,
    client_message_id: str | None,
) -> ConversationTurn | None:
    if not client_message_id:
        return None
    return db.scalar(select(ConversationTurn).where(
        ConversationTurn.session_id == session_id,
        ConversationTurn.client_message_id == client_message_id,
    ))


def run_for_turn(db: Any, turn_id: str) -> Run | None:
    return db.scalar(select(Run).where(Run.turn_id == turn_id))


def stage_user_turn(
    db: Any,
    *,
    session_id: str,
    workspace_id: str | None,
    agent_id: str | None,
    content: str,
    mode: str,
    client_message_id: str | None,
    fingerprint: str | None = None,
    message_extra: dict[str, Any] | None = None,
) -> tuple[ConversationTurn, ChatMessage, Run]:
    """Stage the receipt, user message and root run in one transaction."""

    normalized_content = content.strip()
    turn = ConversationTurn(
        session_id=session_id,
        client_message_id=client_message_id,
        request_fingerprint=fingerprint or request_fingerprint(normalized_content),
        execution_status="received",
        reply_status="pending",
    )
    db.add(turn)
    db.flush()
    message = ChatMessage(
        session_id=session_id,
        role="user",
        content=normalized_content,
        turn_id=turn.id,
        message_kind="user_request",
        sequence=next_chat_message_sequence(db, session_id),
        extra={
            **dict(message_extra or {}),
            "turn_id": turn.id,
            "trace_id": turn.trace_id,
        },
    )
    run = Run(
        session_id=session_id,
        workspace_id=workspace_id,
        agent_id=agent_id,
        turn_id=turn.id,
        mode=mode,
        status="received",
    )
    db.add_all([message, run])
    db.flush()
    turn.user_message_id = message.id
    return turn, message, run


def ensure_run_turn(db: Any, run: Run) -> ConversationTurn | None:
    """Return a run's receipt, conservatively adopting legacy accepted runs."""

    if run.turn_id:
        return db.get(ConversationTurn, run.turn_id)
    if not run.session_id:
        return None
    if db.scalar(select(DelegatedTask.id).where(DelegatedTask.child_run_id == run.id).limit(1)):
        return None

    # A root Run created through generic resource APIs is not necessarily a
    # user turn. Only adopt it when an unclaimed user message proves acceptance.
    user_message = db.scalar(
        select(ChatMessage)
        .where(
            ChatMessage.session_id == run.session_id,
            ChatMessage.role == "user",
            ChatMessage.turn_id.is_(None),
        )
        .order_by(ChatMessage.sequence.desc(), ChatMessage.created_at.desc(), ChatMessage.id.desc())
        .limit(1)
    )
    if user_message is None:
        return None

    turn = ConversationTurn(
        session_id=run.session_id,
        request_fingerprint=request_fingerprint(user_message.content),
        user_message_id=user_message.id,
        execution_status=str(run.status or "received"),
        reply_status="pending",
        heartbeat_at=run.finished_at or run.started_at or utcnow(),
    )
    db.add(turn)
    db.flush()
    run.turn_id = turn.id
    user_message.turn_id = turn.id
    user_message.message_kind = "user_request"
    user_message.extra = {
        **dict(user_message.extra or {}),
        "turn_id": turn.id,
        "trace_id": turn.trace_id,
        "legacy_turn_adopted": True,
    }

    existing_replies = list(db.scalars(
        select(ChatMessage)
        .where(ChatMessage.session_id == run.session_id, ChatMessage.role == "assistant")
        .order_by(ChatMessage.sequence.desc(), ChatMessage.created_at.desc(), ChatMessage.id.desc())
    ))
    existing_reply = next(
        (
            message for message in existing_replies
            if str((message.extra or {}).get("run_id") or "") == run.id
            and (message.extra or {}).get("runtime_run_id") is None
        ),
        None,
    )
    if existing_reply is not None:
        existing_reply.turn_id = turn.id
        existing_reply.message_kind = "terminal"
        existing_reply.terminal_for_turn_id = turn.id
        existing_reply.extra = {
            **dict(existing_reply.extra or {}),
            "turn_id": turn.id,
            "trace_id": turn.trace_id,
        }
        turn.terminal_message_id = existing_reply.id
        turn.reply_status = "delivered"
        turn.finished_at = run.finished_at or utcnow()
    return turn


def sync_turn_progress(db: Any, run: Run) -> ConversationTurn | None:
    turn = ensure_run_turn(db, run)
    if turn is None:
        return None
    turn.execution_status = str(run.status or turn.execution_status)
    turn.heartbeat_at = utcnow()
    return turn


def classify_error_details(
    error_type: str | None,
    message: str | None,
    *,
    status_code: Any = None,
) -> str:
    """Normalize provider/integration details without exposing their text."""

    name = str(error_type or "").casefold()
    normalized_message = str(message or "").casefold()
    try:
        status_number = int(status_code) if status_code is not None else None
    except (TypeError, ValueError):
        status_number = None
    if "partialmodelstream" in name:
        return "model_stream_interrupted"
    if "timeout" in name or "timed out" in normalized_message:
        return "model_timeout"
    if status_number == 429 or "ratelimit" in name or "rate limit" in normalized_message:
        return "model_rate_limited"
    if status_number in {401, 403} or "authentication" in name or "unauthorized" in normalized_message:
        return "model_authentication_error"
    if (
        status_number in {400, 404, 405, 409, 415, 422}
        or "badrequest" in name
        or "bad request" in normalized_message
    ):
        return "model_bad_request"
    if status_number is not None and status_number >= 500:
        return "model_unavailable"
    if "modelconfiguration" in name:
        return "model_configuration_error"
    if "tool" in name:
        return "tool_error"
    if (
        "context" in name
        or "role 'tool'" in normalized_message
        or "tool_calls" in normalized_message
    ):
        return "context_protocol_error"
    return "integration_error"


def classify_exception(error: BaseException) -> str:
    return classify_error_details(
        type(error).__name__,
        str(error),
        status_code=getattr(error, "status_code", None),
    )


_PUBLIC_REASONS = {
    "acceptance_failed": "结果没有通过确定性验收，系统已停止继续尝试。",
    "approval_rejected": "所需操作没有获得批准，任务已停止。",
    "coordinator_orphaned": "执行任务已失去运行进程，系统已将其安全结束。",
    "empty_model_output": "模型没有返回可用内容。",
    "integration_error": "Agent 内部执行发生错误。",
    "interrupted_restart": "PGAgent 在执行期间重启，本次任务已经中断。",
    "launch_unavailable": "任务已经保存，但本地执行器未能启动。",
    "max_run_time": "任务达到最长运行时间，系统已停止执行。",
    "max_steps": "任务达到最大执行步骤数，系统已停止执行。",
    "max_tool_calls": "任务达到最大工具调用次数，系统已停止执行。",
    "model_authentication_error": "模型服务身份验证失败。",
    "model_bad_request": "模型服务拒绝了本次请求。",
    "model_configuration_error": "当前会话没有可用的模型配置。",
    "model_rate_limited": "模型服务当前请求过多，请稍后重试。",
    "model_stream_interrupted": "模型输出过程中连接中断。",
    "model_timeout": "模型服务响应超时。",
    "model_unavailable": "模型服务暂时不可用。",
    "no_progress": "Agent 连续多步没有取得有效进展，系统已停止执行。",
    "repeated_tool_call": "Agent 连续重复相同工具调用，系统已停止执行。",
    "run_failed": "本次任务执行失败。",
    "run_interrupted": "任务已按你的要求停止。",
    "tool_error": "工具执行过程中发生错误。",
    "user_interrupted": "任务已按你的要求停止。",
}


def public_error_message(error_code: str | None, status: str = "failed") -> str:
    code = str(error_code or "").strip()
    if code in _PUBLIC_REASONS:
        return _PUBLIC_REASONS[code]
    if status == "stopped":
        return "本次任务已停止，没有生成完整结果。"
    return "本次任务没有完成。"


def terminal_error_code(
    *,
    status: str,
    stop_reason: str | None,
    error_code: str | None,
) -> str | None:
    if status == "completed":
        return None
    if stop_reason:
        return str(stop_reason)
    return str(error_code or "run_failed")


def _delegated_summary(db: Any, run_id: str) -> tuple[list[str], bool, bool]:
    tasks = list(db.scalars(
        select(DelegatedTask)
        .where(DelegatedTask.parent_run_id == run_id)
        .order_by(DelegatedTask.created_at.asc(), DelegatedTask.id.asc())
    ))
    lines: list[str] = []
    completed = False
    incomplete = False
    changed = False
    labels = {
        "completed": "已完成",
        "failed": "未完成",
        "stopped": "已停止",
        "blocked": "未执行",
        "awaiting_approval": "等待审批",
        "in_progress": "未完成",
    }
    for index, task in enumerate(tasks, start=1):
        result = dict(task.result or {})
        status = str(result.get("status") or task.status or "in_progress")
        label = labels.get(status, "未完成")
        title = " ".join(str(task.title or f"子任务 {index}").split())[:160]
        error_code = str(result.get("error_code") or "").strip()
        suffix = f"（错误代码：{error_code[:100]}）" if error_code and status != "completed" else ""
        lines.append(f"{index}. {title}：{label}{suffix}")
        completed = completed or status == "completed"
        incomplete = incomplete or status != "completed"
        changed = changed or bool(result.get("workspace_changed"))
    return lines, completed and incomplete, changed


def _run_has_recorded_change(db: Any, run_id: str) -> bool:
    for event in db.scalars(select(RunEvent).where(RunEvent.run_id == run_id)):
        payload = event.payload if isinstance(event.payload, dict) else {}
        if bool(payload.get("changed")):
            return True
    return False


def _terminal_content(
    db: Any,
    *,
    run: Run,
    turn: ConversationTurn,
    output: str | None,
    error_code: str | None,
) -> tuple[str, str]:
    accepted_output = str(output or "").strip()
    if accepted_output:
        return accepted_output, "model_output"

    lines, partial_failure, delegated_changed = _delegated_summary(db, run.id)
    parts = [public_error_message(error_code, str(run.status or "failed"))]
    if lines:
        parts.append("子任务结果：\n" + "\n".join(lines))
    if partial_failure or delegated_changed or _run_has_recorded_change(db, run.id):
        parts.append("执行过程中已经产生部分修改，请检查当前结果。")
    parts.append(f"追踪号：{turn.trace_id}")
    return "\n\n".join(parts), "deterministic_fallback"


def persist_terminal_response(
    db: Any,
    run: Run,
    *,
    output: str | None = None,
    error_code: str | None = None,
    error_message: str | None = None,
    provider_payload: dict[str, Any] | None = None,
) -> ChatMessage | None:
    """Persist exactly one terminal assistant reply for a root user turn."""

    if not is_terminal_delivery(str(run.status or ""), run.stop_reason):
        sync_turn_progress(db, run)
        return None
    turn = ensure_run_turn(db, run)
    if turn is None or not run.session_id:
        return None

    existing = db.scalar(select(ChatMessage).where(ChatMessage.terminal_for_turn_id == turn.id))
    _delegated_lines, delegated_partial_failure, _delegated_changed = _delegated_summary(db, run.id)
    execution_status = "partial_failure" if delegated_partial_failure else str(run.status or "failed")
    normalized_code = terminal_error_code(
        status=str(run.status or "failed"),
        stop_reason=run.stop_reason,
        error_code=error_code or run.error_code,
    )
    if existing is not None:
        if provider_payload:
            existing.provider_payload = dict(provider_payload)
        turn.terminal_message_id = existing.id
        turn.reply_status = "delivered"
        turn.execution_status = execution_status
        turn.error_code = normalized_code
        turn.error_message = error_message or run.error_message
        turn.heartbeat_at = utcnow()
        turn.finished_at = run.finished_at or utcnow()
        return existing

    content, source = _terminal_content(
        db,
        run=run,
        turn=turn,
        output=output,
        error_code=normalized_code,
    )
    message = ChatMessage(
        session_id=run.session_id,
        role="assistant",
        content=content,
        turn_id=turn.id,
        message_kind="terminal",
        terminal_for_turn_id=turn.id,
        sequence=next_chat_message_sequence(db, run.session_id),
        extra={
            "run_id": run.id,
            "turn_id": turn.id,
            "trace_id": turn.trace_id,
            "source": source,
            "terminal_status": run.status,
            "execution_status": execution_status,
            "error_code": normalized_code,
        },
        provider_payload=dict(provider_payload or {}),
    )
    db.add(message)
    db.flush()
    turn.terminal_message_id = message.id
    turn.reply_status = "delivered"
    turn.execution_status = execution_status
    turn.error_code = normalized_code
    turn.error_message = error_message or run.error_message
    turn.heartbeat_at = utcnow()
    turn.finished_at = run.finished_at or utcnow()
    db.add(RunEvent(
        run_id=run.id,
        event_type="terminal_response_persisted",
        payload={
            "turn_id": turn.id,
            "message_id": message.id,
            "trace_id": turn.trace_id,
            "status": run.status,
            "error_code": normalized_code,
            "source": source,
        },
    ))
    return message
