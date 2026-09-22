"""Durable, model-independent delivery for accepted conversation turns."""
# 文件职责：负责会话交付与清理中的 delivery 子模块。
# 逻辑关系：上层通过 sessions/delivery.py 使用本模块；本模块把处理结果交给同领域服务、持久化层或 API 响应层。

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select

from src.persistence.database import (
    ChatMessage,
    ConversationTurn,
    DelegatedTask,
    Run,
    RunEvent,
    next_chat_message_sequence,
)
from src.persistence.run_events import append_run_event
from src.coding.changes import aggregate_change_sets


# 变量说明：TERMINAL_RUN_STATUSES 表示当前流程使用的 TERMINAL_RUN_STATUSES 集合。
TERMINAL_RUN_STATUSES = frozenset({"completed", "failed", "stopped"})
# 变量说明：NON_TERMINAL_STOP_REASONS 表示当前流程使用的 NON_TERMINAL_STOP_REASONS 集合。
NON_TERMINAL_STOP_REASONS = frozenset({
    "delegated_child_awaiting_approval",
    "delegated_child_waiting_event",
    "waiting_background",
})


# 函数职责：完成 utcnow 对应的业务处理。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def utcnow() -> datetime:
    return datetime.now(timezone.utc)


# 函数职责：完成 request_fingerprint 对应的业务处理。
# 参数关系：content 表示待处理或返回的正文内容。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def request_fingerprint(content: str) -> str:
    # 变量说明：normalized 表示当前步骤使用的 normalized 值。
    normalized = content.strip().encode("utf-8")
    return hashlib.sha256(normalized).hexdigest()


# 函数职责：完成 is_terminal_delivery 对应的业务处理。
# 参数关系：status 表示当前对象或运行的状态；stop_reason 表示当前步骤使用的 stop_reason 值。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def is_terminal_delivery(status: str, stop_reason: str | None = None) -> bool:
    return status in TERMINAL_RUN_STATUSES and not (
        status == "stopped" and stop_reason in NON_TERMINAL_STOP_REASONS
    )


def is_stale_restart_terminal_response(message: ChatMessage, run: Run) -> bool:
    """Return whether a restart fallback was superseded by this run's real outcome."""

    extra = dict(message.extra or {})
    was_restart_fallback = (
        str(extra.get("terminal_status") or "") == "stopped"
        and str(extra.get("error_code") or "") == "interrupted_restart"
    )
    remains_restart_interrupted = (
        str(run.status or "") == "stopped"
        and str(run.stop_reason or "") == "interrupted_restart"
    )
    return was_restart_fallback and not remains_restart_interrupted


def run_change_summary(db: Any, run_id: str) -> dict[str, Any] | None:
    """Aggregate structured file changes already persisted for one run."""

    events = db.scalars(
        select(RunEvent)
        .where(
            RunEvent.run_id == run_id,
            RunEvent.event_type.in_(("tool_finished", "tool_result")),
        )
        .order_by(RunEvent.sequence.asc(), RunEvent.created_at.asc(), RunEvent.id.asc())
    )
    values: list[object] = []
    call_indexes: dict[str, int] = {}
    for event in events:
        payload = event.payload if isinstance(event.payload, dict) else {}
        change_set = payload.get("change_set")
        if change_set is None:
            continue
        call_id = str(payload.get("tool_call_id") or "").strip()
        if call_id and call_id in call_indexes:
            # 兼容同时持久化 tool_finished/tool_result 的旧链路，同一次调用只计一次。
            values[call_indexes[call_id]] = change_set
            continue
        if call_id:
            call_indexes[call_id] = len(values)
        values.append(change_set)
    return aggregate_change_sets(values)


# 函数职责：查找 turn_by_client_message 对应的数据或流程。
# 参数关系：db 表示当前数据库会话；session_id 表示所属会话标识；client_message_id 表示client_message 对象的唯一标识。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
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


# 函数职责：执行 for_turn 对应的数据或流程。
# 参数关系：db 表示当前数据库会话；turn_id 表示turn 对象的唯一标识。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def run_for_turn(db: Any, turn_id: str) -> Run | None:
    return db.scalar(select(Run).where(Run.turn_id == turn_id))


# 函数职责：完成 stage_user_turn 对应的业务处理。
# 参数关系：db 表示当前数据库会话；session_id 表示所属会话标识；workspace_id 表示工作区标识；agent_id 表示智能体标识；content 表示待处理或返回的正文内容；mode 表示当前步骤使用的 mode 值；client_message_id 表示client_message 对象的唯一标识；fingerprint 表示当前步骤使用的 fingerprint 值；其余参数沿用调用方提供的扩展选项。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
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

    # 变量说明：normalized_content 表示当前步骤使用的 normalized_content 值。
    normalized_content = content.strip()
    # 变量说明：turn 表示当前步骤使用的 turn 值。
    turn = ConversationTurn(
        session_id=session_id,
        client_message_id=client_message_id,
        request_fingerprint=fingerprint or request_fingerprint(normalized_content),
        execution_status="received",
        reply_status="pending",
    )
    db.add(turn)
    db.flush()
    # 变量说明：message 表示当前消息。
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
    # 变量说明：run 表示当前步骤使用的 run 值。
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
    # 变量说明：user_message_id 表示user_message 对象的唯一标识。
    turn.user_message_id = message.id
    return turn, message, run


# 函数职责：确保 run_turn 对应的数据或流程。
# 参数关系：db 表示当前数据库会话；run 表示当前步骤使用的 run 值。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
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
    # 变量说明：user_message 表示当前步骤使用的 user_message 值。
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

    # 变量说明：turn 表示当前步骤使用的 turn 值。
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
    # 变量说明：turn_id 表示turn 对象的唯一标识。
    run.turn_id = turn.id
    # 变量说明：turn_id 表示turn 对象的唯一标识。
    user_message.turn_id = turn.id
    # 变量说明：message_kind 表示当前步骤使用的 message_kind 值。
    user_message.message_kind = "user_request"
    # 变量说明：extra 表示当前步骤使用的 extra 值。
    user_message.extra = {
        **dict(user_message.extra or {}),
        "turn_id": turn.id,
        "trace_id": turn.trace_id,
        "legacy_turn_adopted": True,
    }

    # 变量说明：existing_replies 表示当前流程使用的 existing_replies 集合。
    existing_replies = list(db.scalars(
        select(ChatMessage)
        .where(ChatMessage.session_id == run.session_id, ChatMessage.role == "assistant")
        .order_by(ChatMessage.sequence.desc(), ChatMessage.created_at.desc(), ChatMessage.id.desc())
    ))
    # 变量说明：existing_reply 表示当前步骤使用的 existing_reply 值。
    existing_reply = next(
        (
            message for message in existing_replies
            if str((message.extra or {}).get("run_id") or "") == run.id
            and (message.extra or {}).get("runtime_run_id") is None
        ),
        None,
    )
    if existing_reply is not None:
        # 变量说明：turn_id 表示turn 对象的唯一标识。
        existing_reply.turn_id = turn.id
        # 变量说明：message_kind 表示当前步骤使用的 message_kind 值。
        existing_reply.message_kind = "terminal"
        # 变量说明：terminal_for_turn_id 表示terminal_for_turn 对象的唯一标识。
        existing_reply.terminal_for_turn_id = turn.id
        # 变量说明：extra 表示当前步骤使用的 extra 值。
        existing_reply.extra = {
            **dict(existing_reply.extra or {}),
            "turn_id": turn.id,
            "trace_id": turn.trace_id,
        }
        # 变量说明：terminal_message_id 表示terminal_message 对象的唯一标识。
        turn.terminal_message_id = existing_reply.id
        # 变量说明：reply_status 表示当前流程使用的 reply_status 集合。
        turn.reply_status = "delivered"
        # 变量说明：finished_at 表示finished_at 对应的时间信息。
        turn.finished_at = run.finished_at or utcnow()
    return turn


# 函数职责：完成 sync_turn_progress 对应的业务处理。
# 参数关系：db 表示当前数据库会话；run 表示当前步骤使用的 run 值。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def sync_turn_progress(db: Any, run: Run) -> ConversationTurn | None:
    # 变量说明：turn 表示当前步骤使用的 turn 值。
    turn = ensure_run_turn(db, run)
    if turn is None:
        return None
    # 变量说明：execution_status 表示当前流程使用的 execution_status 集合。
    turn.execution_status = str(run.status or turn.execution_status)
    # 变量说明：heartbeat_at 表示heartbeat_at 对应的时间信息。
    turn.heartbeat_at = utcnow()
    return turn


# 函数职责：完成 classify_error_details 对应的业务处理。
# 参数关系：error_type 表示当前步骤使用的 error_type 值；message 表示当前消息；status_code 表示当前步骤使用的 status_code 值。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def classify_error_details(
    error_type: str | None,
    message: str | None,
    *,
    status_code: Any = None,
    error_kind: str | None = None,
    provider_error_code: str | None = None,
) -> str:
    """Normalize provider/integration details without exposing their text."""

    # 变量说明：name 表示当前对象名称。
    name = str(error_type or "").casefold()
    kind = str(error_kind or "").casefold()
    provider_code = str(provider_error_code or "").casefold()
    # 变量说明：normalized_message 表示当前步骤使用的 normalized_message 值。
    normalized_message = str(message or "").casefold()
    try:
        # 变量说明：status_number 表示当前步骤使用的 status_number 值。
        status_number = int(status_code) if status_code is not None else None
    except (TypeError, ValueError):
        # 变量说明：status_number 表示当前步骤使用的 status_number 值。
        status_number = None
    if provider_code == "max_output_tokens":
        return "model_output_limit"
    if provider_code in {"bio_policy", "content_filter", "image_content_policy_violation"}:
        return "model_content_filtered"
    if provider_code == "data_residency_mismatch":
        return "model_data_residency_error"
    if provider_code in {
        "empty_image_file",
        "failed_to_download_image",
        "image_file_not_found",
        "image_file_too_large",
        "image_parse_error",
        "image_too_large",
        "image_too_small",
        "invalid_base64_image",
        "invalid_image",
        "invalid_image_format",
        "invalid_image_mode",
        "invalid_image_url",
        "invalid_prompt",
        "unsupported_image_media_type",
    }:
        return "model_input_error"
    if kind == "rate_limit" or provider_code == "rate_limit_exceeded":
        return "model_rate_limited"
    if kind == "timeout" or provider_code == "vector_store_timeout":
        return "model_timeout"
    if kind in {"server", "connection"} or provider_code in {"server_error", "server_is_overloaded"}:
        return "model_unavailable"
    if kind == "auth":
        return "model_authentication_error"
    if provider_code:
        return "model_provider_error"
    if "partialmodelstream" in name:
        return "model_stream_interrupted"
    if "streaminterrupted" in name:
        return "model_unavailable"
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
    if "connection" in name or "connecterror" in name or "network" in name:
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


# 函数职责：完成 classify_exception 对应的业务处理。
# 参数关系：error 表示当前捕获或准备上报的错误。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def classify_exception(error: BaseException) -> str:
    from src.agent.errors import classify_api_error, status_code_from_error

    return classify_error_details(
        type(error).__name__,
        str(error),
        status_code=status_code_from_error(error),
        error_kind=classify_api_error(error).value,
        provider_error_code=getattr(error, "provider_error_code", None),
    )


# 变量说明：_PUBLIC_REASONS 表示当前流程使用的 _PUBLIC_REASONS 集合。
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
    "model_content_filtered": "模型输出被内容安全策略阻止。",
    "model_data_residency_error": "模型服务的数据驻留配置与本次请求不匹配。",
    "model_input_error": "模型服务无法处理本次输入内容。",
    "model_output_limit": "模型输出达到长度上限，未能完整结束。",
    "model_provider_error": "模型服务返回了无法识别的错误。",
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


# 函数职责：完成 public_error_message 对应的业务处理。
# 参数关系：error_code 表示当前步骤使用的 error_code 值；status 表示当前对象或运行的状态。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def public_error_message(error_code: str | None, status: str = "failed") -> str:
    # 变量说明：code 表示当前步骤使用的 code 值。
    code = str(error_code or "").strip()
    if code in _PUBLIC_REASONS:
        return _PUBLIC_REASONS[code]
    if status == "stopped":
        return "本次任务已停止，没有生成完整结果。"
    return "本次任务没有完成。"


# 函数职责：完成 terminal_error_code 对应的业务处理。
# 参数关系：status 表示当前对象或运行的状态；stop_reason 表示当前步骤使用的 stop_reason 值；error_code 表示当前步骤使用的 error_code 值。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
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


# 函数职责：完成 delegated_summary 对应的业务处理。
# 参数关系：db 表示当前数据库会话；run_id 表示当前运行标识。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def _delegated_summary(db: Any, run_id: str) -> tuple[list[str], bool, bool]:
    # 变量说明：tasks 表示当前流程使用的 tasks 集合。
    tasks = list(db.scalars(
        select(DelegatedTask)
        .where(DelegatedTask.parent_run_id == run_id)
        .order_by(DelegatedTask.created_at.asc(), DelegatedTask.id.asc())
    ))
    # 变量说明：lines 表示当前流程使用的 lines 集合。
    lines: list[str] = []
    # 变量说明：completed 表示当前步骤使用的 completed 值。
    completed = False
    # 变量说明：incomplete 表示当前步骤使用的 incomplete 值。
    incomplete = False
    # 变量说明：changed 表示当前步骤使用的 changed 值。
    changed = False
    # 变量说明：labels 表示当前流程使用的 labels 集合。
    labels = {
        "completed": "已完成",
        "failed": "未完成",
        "stopped": "已停止",
        "blocked": "未执行",
        "awaiting_approval": "等待审批",
        "in_progress": "未完成",
    }
    for index, task in enumerate(tasks, start=1):
        # 变量说明：result 表示本步骤产生的结果。
        result = dict(task.result or {})
        # 变量说明：status 表示当前对象或运行的状态。
        status = str(result.get("status") or task.status or "in_progress")
        # 变量说明：label 表示当前步骤使用的 label 值。
        label = labels.get(status, "未完成")
        # 变量说明：title 表示当前步骤使用的 title 值。
        title = " ".join(str(task.title or f"子任务 {index}").split())[:160]
        # 变量说明：error_code 表示当前步骤使用的 error_code 值。
        error_code = str(result.get("error_code") or "").strip()
        # 变量说明：suffix 表示当前步骤使用的 suffix 值。
        suffix = f"（错误代码：{error_code[:100]}）" if error_code and status != "completed" else ""
        lines.append(f"{index}. {title}：{label}{suffix}")
        # 变量说明：completed 表示当前步骤使用的 completed 值。
        completed = completed or status == "completed"
        # 变量说明：incomplete 表示当前步骤使用的 incomplete 值。
        incomplete = incomplete or status != "completed"
        # 变量说明：changed 表示当前步骤使用的 changed 值。
        changed = changed or bool(result.get("workspace_changed"))
    return lines, completed and incomplete, changed


# 函数职责：执行 has_recorded_change 对应的数据或流程。
# 参数关系：db 表示当前数据库会话；run_id 表示当前运行标识。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def _run_has_recorded_change(db: Any, run_id: str) -> bool:
    for event in db.scalars(select(RunEvent).where(RunEvent.run_id == run_id)):
        # 变量说明：payload 表示跨层传递的数据载荷。
        payload = event.payload if isinstance(event.payload, dict) else {}
        if bool(payload.get("changed")):
            return True
    return False


# 函数职责：完成 terminal_content 对应的业务处理。
# 参数关系：db 表示当前数据库会话；run 表示当前步骤使用的 run 值；turn 表示当前步骤使用的 turn 值；output 表示当前步骤使用的 output 值；error_code 表示当前步骤使用的 error_code 值。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def _terminal_content(
    db: Any,
    *,
    run: Run,
    turn: ConversationTurn,
    output: str | None,
    error_code: str | None,
) -> tuple[str, str]:
    # 变量说明：accepted_output 表示当前步骤使用的 accepted_output 值。
    accepted_output = str(output or "").strip()
    if accepted_output:
        return accepted_output, "model_output"

    # 变量说明：lines 表示当前流程使用的 lines 集合；partial_failure 表示当前步骤使用的 partial_failure 值；delegated_changed 表示当前步骤使用的 delegated_changed 值。
    lines, partial_failure, delegated_changed = _delegated_summary(db, run.id)
    # 变量说明：parts 表示当前流程使用的 parts 集合。
    parts = [public_error_message(error_code, str(run.status or "failed"))]
    if lines:
        parts.append("子任务结果：\n" + "\n".join(lines))
    if partial_failure or delegated_changed or _run_has_recorded_change(db, run.id):
        parts.append("执行过程中已经产生部分修改，请检查当前结果。")
    parts.append(f"追踪号：{turn.trace_id}")
    return "\n\n".join(parts), "deterministic_fallback"


# 函数职责：完成 persist_terminal_response 对应的业务处理。
# 参数关系：db 表示当前数据库会话；run 表示当前步骤使用的 run 值；output 表示当前步骤使用的 output 值；error_code 表示当前步骤使用的 error_code 值；error_message 表示当前步骤使用的 error_message 值；provider_payload 表示当前步骤使用的 provider_payload 值。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
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
    # 变量说明：turn 表示当前步骤使用的 turn 值。
    turn = ensure_run_turn(db, run)
    if turn is None or not run.session_id:
        return None

    # 变量说明：existing 表示当前步骤使用的 existing 值。
    existing = db.scalar(select(ChatMessage).where(ChatMessage.terminal_for_turn_id == turn.id))
    # 变量说明：_delegated_lines 表示当前流程使用的 _delegated_lines 集合；delegated_partial_failure 表示当前步骤使用的 delegated_partial_failure 值；_delegated_changed 表示当前步骤使用的 _delegated_changed 值。
    _delegated_lines, delegated_partial_failure, _delegated_changed = _delegated_summary(db, run.id)
    # 变量说明：execution_status 表示当前流程使用的 execution_status 集合。
    execution_status = "partial_failure" if delegated_partial_failure else str(run.status or "failed")
    # 变量说明：normalized_code 表示当前步骤使用的 normalized_code 值。
    normalized_code = terminal_error_code(
        status=str(run.status or "failed"),
        stop_reason=run.stop_reason,
        error_code=error_code or run.error_code,
    )
    if existing is not None:
        change_summary = run_change_summary(db, run.id)
        revise_restart_terminal = is_stale_restart_terminal_response(existing, run)
        if revise_restart_terminal:
            # 重复启动可能先写入“重启中断”，而原执行进程随后仍会返回真实终态。
            # 这里复用同一消息主键，只修正被后续事实推翻的重启兜底内容。
            content, source = _terminal_content(
                db,
                run=run,
                turn=turn,
                output=output,
                error_code=normalized_code,
            )
            revised_extra = {
                "run_id": run.id,
                "turn_id": turn.id,
                "trace_id": turn.trace_id,
                "source": source,
                "terminal_status": run.status,
                "execution_status": execution_status,
                "error_code": normalized_code,
            }
            if change_summary is not None:
                revised_extra["change_summary"] = change_summary
            existing.content = content
            existing.extra = revised_extra
            existing.provider_payload = dict(provider_payload or {})
            append_run_event(
                db,
                run_id=run.id,
                event_type="terminal_response_revised",
                payload={
                    "turn_id": turn.id,
                    "message_id": existing.id,
                    "trace_id": turn.trace_id,
                    "status": run.status,
                    "error_code": normalized_code,
                    "source": source,
                    "previous_error_code": "interrupted_restart",
                },
            )
        elif change_summary is not None:
            existing.extra = {**dict(existing.extra or {}), "change_summary": change_summary}
        if provider_payload and not revise_restart_terminal:
            # 变量说明：provider_payload 表示当前步骤使用的 provider_payload 值。
            existing.provider_payload = dict(provider_payload)
        # 变量说明：terminal_message_id 表示terminal_message 对象的唯一标识。
        turn.terminal_message_id = existing.id
        # 变量说明：reply_status 表示当前流程使用的 reply_status 集合。
        turn.reply_status = "delivered"
        # 变量说明：execution_status 表示当前流程使用的 execution_status 集合。
        turn.execution_status = execution_status
        # 变量说明：error_code 表示当前步骤使用的 error_code 值。
        turn.error_code = normalized_code
        # 变量说明：error_message 表示当前步骤使用的 error_message 值。
        turn.error_message = error_message or run.error_message
        # 变量说明：heartbeat_at 表示heartbeat_at 对应的时间信息。
        turn.heartbeat_at = utcnow()
        # 变量说明：finished_at 表示finished_at 对应的时间信息。
        turn.finished_at = run.finished_at or utcnow()
        return existing

    # 变量说明：content 表示待处理或返回的正文内容；source 表示当前步骤使用的 source 值。
    content, source = _terminal_content(
        db,
        run=run,
        turn=turn,
        output=output,
        error_code=normalized_code,
    )
    change_summary = run_change_summary(db, run.id)
    message_extra = {
        "run_id": run.id,
        "turn_id": turn.id,
        "trace_id": turn.trace_id,
        "source": source,
        "terminal_status": run.status,
        "execution_status": execution_status,
        "error_code": normalized_code,
    }
    if change_summary is not None:
        message_extra["change_summary"] = change_summary
    # 变量说明：message 表示当前消息。
    message = ChatMessage(
        session_id=run.session_id,
        role="assistant",
        content=content,
        turn_id=turn.id,
        message_kind="terminal",
        terminal_for_turn_id=turn.id,
        sequence=next_chat_message_sequence(db, run.session_id),
        extra=message_extra,
        provider_payload=dict(provider_payload or {}),
    )
    db.add(message)
    db.flush()
    # 变量说明：terminal_message_id 表示terminal_message 对象的唯一标识。
    turn.terminal_message_id = message.id
    # 变量说明：reply_status 表示当前流程使用的 reply_status 集合。
    turn.reply_status = "delivered"
    # 变量说明：execution_status 表示当前流程使用的 execution_status 集合。
    turn.execution_status = execution_status
    # 变量说明：error_code 表示当前步骤使用的 error_code 值。
    turn.error_code = normalized_code
    # 变量说明：error_message 表示当前步骤使用的 error_message 值。
    turn.error_message = error_message or run.error_message
    # 变量说明：heartbeat_at 表示heartbeat_at 对应的时间信息。
    turn.heartbeat_at = utcnow()
    # 变量说明：finished_at 表示finished_at 对应的时间信息。
    turn.finished_at = run.finished_at or utcnow()
    append_run_event(
        db,
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
    )
    return message
