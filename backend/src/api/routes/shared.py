"""Shared API serialization and resource lookup helpers."""
# 文件职责：负责HTTP 接口、数据契约与依赖装配中的 shared 子模块。
# 逻辑关系：上层通过 api/routes/shared.py 使用本模块；本模块把处理结果交给同领域服务、持久化层或 API 响应层。

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import re
import shutil
from typing import Any, TypeVar
from urllib.parse import urlsplit, urlunsplit

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from src.config import settings
from src.persistence.database import (
    Agent,
    Approval,
    BackgroundJob,
    ChatMessage,
    CollaborationEvent,
    CollaborationMessage,
    CollaborationTeam,
    DelegatedTask,
    DurableTask,
    DraftLaunch,
    Memory,
    ModelConnection,
    Run,
    RunEvent,
    Session as ChatSession,
    TeammateWorker,
    Workspace,
    UsageRecord,
    DEFAULT_AGENT_ID,
    DEFAULT_WORKSPACE_ID,
    get_db,
    next_chat_message_sequence,
)
from src.api.schemas import (
    AgentCreate,
    AgentRead,
    AgentUpdate,
    ApprovalRead,
    BackgroundJobRead,
    ChatMessageCreate,
    ChatMessageRead,
    CollaborationEventRead,
    CollaborationMessageRead,
    DashboardRead,
    DelegatedTaskRead,
    DurableTaskRead,
    MemoryCreate,
    MemoryRead,
    MemoryUpdate,
    RunCreate,
    RunEventCreate,
    RunEventRead,
    RunRead,
    RunUpdate,
    SessionCreate,
    SessionRead,
    SessionUpdate,
    TeammateRead,
    WorkspaceCreate,
    WorkspaceRead,
    WorkspaceUpdate,
)
from src.memory.service import recall_memories, refresh_memory_markdown_projection, store_memory
from src.skills.registry import replace_agent_capabilities, replace_session_skills
from src.tasks.state import latest_resumable_task, task_payload
from src.agents.collaboration import cleanup_session_worktrees
# 变量说明：T 表示当前步骤使用的 T 值。
T = TypeVar("T")
# 变量说明：_ACTIVE_SESSION_RUN_STATUSES 表示当前流程使用的 _ACTIVE_SESSION_RUN_STATUSES 集合。
_ACTIVE_SESSION_RUN_STATUSES = frozenset(
    {"received", "preparing_context", "planning", "acting", "observing", "running", "awaiting_approval"}
)
# 变量说明：_SESSION_RUNTIME_SETTING_FIELDS 表示当前流程使用的 _SESSION_RUNTIME_SETTING_FIELDS 集合。
_SESSION_RUNTIME_SETTING_FIELDS = frozenset({
    "model_connection_id", "model_id", "thinking_level", "use_memories", "mcp_server_names",
})
# 变量说明：_PUBLIC_RUN_EVENT_TYPES 表示当前流程使用的 _PUBLIC_RUN_EVENT_TYPES 集合。
_PUBLIC_RUN_EVENT_TYPES = frozenset({
    "approval_rejected",
    "approval_requested",
    "completed",
    "context_prepared",
    "context_compacted",
    "context_protocol_repaired",
    "context_resumed",
    "context_compaction_started",
    "context_compaction_finished",
    "context_compaction_failed",
    "delegated_child_started",
    "delegated_child_awaiting_approval",
    "delegated_child_completed",
    "delegated_child_stopped",
    "delegated_child_failed",
    "delegated_child_continuation_started",
    "failed",
    "integration_failed",
    "model_failed",
    "model_retry",
    "model_step_finished",
    "model_step_started",
    "mcp_catalog_loading",
    "mcp_connecting",
    "mcp_server_ready",
    "mcp_ready",
    "mcp_degraded",
    "completion_verification_started",
    "completion_verification_rejected",
    "completion_verification_passed",
    "progress",
    "agent_progress",
    "thought_summary",
    "activity_update",
    "assistant_message_started",
    "assistant_message_delta",
    "assistant_message_completed",
    "model_response_completed",
    "run_completed",
    "run_interrupted",
    "run_stopped",
    "turn_completed",
    "turn_failed",
    "turn_stopped",
    "stopped",
    "terminal_response_persisted",
    "tool_call",
    "tool_finished",
    "tool_result",
    "tool_started",
    "user_question_requested",
})
# 变量说明：_TOOL_START_EVENT_TYPES 表示当前流程使用的 _TOOL_START_EVENT_TYPES 集合。
_TOOL_START_EVENT_TYPES = frozenset({"tool_call", "tool_started"})
# 变量说明：_TOOL_FINISH_EVENT_TYPES 表示当前流程使用的 _TOOL_FINISH_EVENT_TYPES 集合。
_TOOL_FINISH_EVENT_TYPES = frozenset({"tool_finished", "tool_result"})
# 变量说明：_TERMINAL_EVENT_TYPES 表示当前流程使用的 _TERMINAL_EVENT_TYPES 集合。
_TERMINAL_EVENT_TYPES = frozenset({
    "completed", "failed", "integration_failed", "model_failed", "run_completed", "run_interrupted", "run_stopped", "stopped", "turn_completed", "turn_failed", "turn_stopped"
})
# 变量说明：_PUBLIC_EVENT_NUMBER_FIELDS 表示当前流程使用的 _PUBLIC_EVENT_NUMBER_FIELDS 集合。
_PUBLIC_EVENT_NUMBER_FIELDS = frozenset({
    "after_tokens",
    "attempt",
    "before_tokens",
    "delay_seconds",
    "duration_ms",
    "elapsed_ms",
    "estimated_tokens",
    "omitted_messages",
    "removed_messages",
    "affected_call_count",
    "output_chars",
    "question_chars",
    "remaining_call_count",
    "source_count",
    "step",
    "thought_duration_ms",
    "tool_count",
})
# 变量说明：_PUBLIC_EVENT_BOOLEAN_FIELDS 表示当前流程使用的 _PUBLIC_EVENT_BOOLEAN_FIELDS 集合。
_PUBLIC_EVENT_BOOLEAN_FIELDS = frozenset({
    "accepted", "complete",
    "changed", "has_output", "ok", "pending_approval", "requires_next_message", "terminal",
})


# 函数职责：完成 require 对应的业务处理。
# 参数关系：db 表示当前数据库会话；model 表示当前选择的模型；object_id 表示object 对象的唯一标识；label 表示当前步骤使用的 label 值。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def _require(db: Session, model: type[T], object_id: str, label: str) -> T:
    # 变量说明：item 表示当前步骤使用的 item 值。
    item = db.get(model, object_id)
    if item is None:
        raise HTTPException(status_code=404, detail=f"{label} not found")
    return item


# 函数职责：完成 commit 对应的业务处理。
# 参数关系：db 表示当前数据库会话；conflict_message 表示当前步骤使用的 conflict_message 值。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def _commit(db: Session, conflict_message: str = "Related resource does not exist") -> None:
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=conflict_message) from exc


# 函数职责：完成 apply 对应的业务处理。
# 参数关系：item 表示当前步骤使用的 item 值；payload 表示跨层传递的数据载荷；field_map 表示按键快速定位 field_map 数据的映射。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def _apply(item: Any, payload: Any, *, field_map: dict[str, str] | None = None) -> None:
    # 变量说明：mapping 表示当前步骤使用的 mapping 值。
    mapping = field_map or {}
    for key, value in payload.model_dump(exclude_unset=True).items():
        setattr(item, mapping.get(key, key), value)


# 函数职责：完成 require_enabled_model_connection 对应的业务处理。
# 参数关系：db 表示当前数据库会话；connection_id 表示connection 对象的唯一标识。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def _require_enabled_model_connection(db: Session, connection_id: str | None) -> None:
    if connection_id is None:
        return
    # 变量说明：connection 表示当前步骤使用的 connection 值。
    connection = db.get(ModelConnection, connection_id)
    if connection is None:
        raise HTTPException(status_code=409, detail="Selected model connection does not exist")
    if not connection.enabled:
        raise HTTPException(status_code=409, detail="Selected model connection is disabled")


# 函数职责：完成 public_event_text 对应的业务处理。
# 参数关系：value 表示当前字段或计算值；limit 表示当前步骤使用的 limit 值。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def _public_event_text(value: Any, *, limit: int = 160) -> str | None:
    """Return a bounded display label, never arbitrary event payload content."""

    if not isinstance(value, str):
        return None
    return " ".join(value.replace("\x00", "").split())[:limit] or None


def _public_event_identifier(value: Any) -> str | None:
    """公开事件只接受短错误标识符，拒绝正文式供应商内容。"""

    if not isinstance(value, str) or re.fullmatch(r"[A-Za-z0-9_.:-]{1,120}", value) is None:
        return None
    return value


# 函数职责：完成 public_thought_text 对应的业务处理。
# 参数关系：value 表示当前字段或计算值；limit 表示当前步骤使用的 limit 值。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def _public_thought_text(value: Any, *, limit: int = 20_000) -> str | None:
    if not isinstance(value, str):
        return None
    # 变量说明：text 表示当前步骤使用的 text 值。
    text = value.replace("\x00", "").replace("\r\n", "\n").replace("\r", "\n")[:limit]
    return text or None


# 函数职责：完成 public_event_url 对应的业务处理。
# 参数关系：value 表示当前字段或计算值。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def _public_event_url(value: Any) -> str | None:
    """Keep a URL target useful to the timeline without query/fragment secrets."""

    # 变量说明：text 表示当前步骤使用的 text 值。
    text = _public_event_text(value, limit=2_048)
    if text is None:
        return None
    try:
        # 变量说明：parsed 表示当前步骤使用的 parsed 值。
        parsed = urlsplit(text)
    except ValueError:
        return None
    # 变量说明：host 表示当前步骤使用的 host 值。
    host = parsed.hostname
    if parsed.scheme not in {"http", "https"} or not host:
        return None
    try:
        # 变量说明：port 表示当前步骤使用的 port 值。
        port = parsed.port
    except ValueError:
        return None
    if ":" in host and not host.startswith("["):
        # 变量说明：host 表示当前步骤使用的 host 值。
        host = f"[{host}]"
    # 变量说明：netloc 表示当前步骤使用的 netloc 值。
    netloc = f"{host}:{port}" if port is not None else host
    return urlunsplit((parsed.scheme, netloc, parsed.path, "", ""))[:300]


# 变量说明：_PRIVATE_ARGUMENT_MARKERS 表示当前流程使用的 _PRIVATE_ARGUMENT_MARKERS 集合。
_PRIVATE_ARGUMENT_MARKERS = (
    "api_key", "apikey", "authorization", "token", "secret", "password",
    "cookie", "content", "old_string", "new_string",
)


# 函数职责：完成 public_tool_argument_summary 对应的业务处理。
# 参数关系：value 表示当前字段或计算值。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def _public_tool_argument_summary(value: Any) -> dict[str, Any]:
    """Expose only a display target from a tool's already-scrubbed summary.

    Runtime events are normally written with ``safe_tool_argument_summary``.
    The HTTP read boundary still cannot trust every historical or manually
    created row, so it deliberately accepts no arbitrary argument keys.
    """

    if not isinstance(value, dict):
        return {}
    # 变量说明：public 表示当前步骤使用的 public 值。
    public: dict[str, Any] = {}
    for raw_key, raw_value in value.items():
        # 变量说明：key 表示用于查找或映射的键。
        key = str(raw_key)[:80]
        # 变量说明：lowered 表示当前步骤使用的 lowered 值。
        lowered = key.casefold()
        if any(marker in lowered for marker in _PRIVATE_ARGUMENT_MARKERS):
            continue
        if key in {"path", "file_path", "target"}:
            # 变量说明：text 表示当前步骤使用的 text 值。
            text = _public_event_text(raw_value, limit=300)
            if text is not None:
                public[key] = text
            continue
        if key == "url":
            # 变量说明：url 表示当前步骤使用的 url 值。
            url = _public_event_url(raw_value)
            if url is not None:
                public[key] = url
            continue
        if key == "command" and isinstance(raw_value, dict):
            # 变量说明：command 表示当前步骤使用的 command 值。
            command: dict[str, Any] = {}
            # 变量说明：command_text 表示command 的文本表示。
            command_text = _public_event_text(raw_value.get("text"), limit=320)
            if command_text is not None:
                command["text"] = command_text
            # 变量说明：executable 表示当前步骤使用的 executable 值。
            executable = _public_event_text(raw_value.get("executable"), limit=120)
            if executable is not None:
                command["executable"] = executable
            # 变量说明：argument_count 表示argument 的数量。
            argument_count = raw_value.get("argument_count")
            if isinstance(argument_count, int) and not isinstance(argument_count, bool):
                command["argument_count"] = max(0, argument_count)
            if command:
                # 变量说明：public 的索引项 表示该语句创建或更新的目标数据。
                public[key] = command
            continue
        if isinstance(raw_value, dict):
            # 变量说明：metadata 表示当前步骤使用的 metadata 值。
            metadata = {
                metadata_key: metadata_value
                for metadata_key in ("text", "chars", "count", "argument_count", "provided")
                if isinstance((metadata_value := raw_value.get(metadata_key)), (int, float, bool))
            }
            items = raw_value.get("items")
            if isinstance(items, list):
                safe_items = [_public_event_text(item, limit=320) for item in items[:3] if isinstance(item, str)]
                safe_items = [item for item in safe_items if item is not None]
                if safe_items:
                    metadata["items"] = safe_items
            # 变量说明：text_value 表示当前步骤使用的 text_value 值。
            text_value = _public_event_text(raw_value.get("text"), limit=320)
            if text_value is not None:
                metadata["text"] = text_value
            if metadata:
                # 变量说明：public 的索引项 表示该语句创建或更新的目标数据。
                public[key] = metadata
            continue
        if isinstance(raw_value, str):
            # 变量说明：public 的索引项 表示该语句创建或更新的目标数据。
            public[key] = {"chars": len(raw_value)}
        elif isinstance(raw_value, (int, float, bool)) or raw_value is None:
            # 变量说明：public 的索引项 表示该语句创建或更新的目标数据。
            public[key] = raw_value
    return public


# 函数职责：完成 public_run_event_payload 对应的业务处理。
# 参数关系：event_type 表示当前步骤使用的 event_type 值；payload 表示跨层传递的数据载荷。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def _public_run_event_payload(event_type: str, payload: Any) -> dict[str, Any]:
    """Project a persisted event into the minimal timeline-safe contract.

    A RunEvent is also used as the private runtime checkpoint store.  Never
    expose an unfiltered payload here: it can contain model messages, original
    arguments/results, credential references, or loaded Skill source text.
    """

    # 变量说明：source 表示当前步骤使用的 source 值。
    source = payload if isinstance(payload, dict) else {}
    # 变量说明：public 表示当前步骤使用的 public 值。
    public: dict[str, Any] = {}
    for key in _PUBLIC_EVENT_NUMBER_FIELDS:
        # 变量说明：value 表示当前字段或计算值。
        value = source.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            public[key] = value
    for key in _PUBLIC_EVENT_BOOLEAN_FIELDS:
        # 变量说明：value 表示当前字段或计算值。
        value = source.get(key)
        if isinstance(value, bool):
            public[key] = value

    if event_type == "model_failed":
        for key in ("error_kind", "error_type", "provider_error_code", "provider_error_type"):
            identifier = _public_event_identifier(source.get(key))
            if identifier is not None:
                public[key] = identifier

    if event_type in _TOOL_START_EVENT_TYPES | _TOOL_FINISH_EVENT_TYPES | {"user_question_requested"}:
        for key in ("tool_name", "tool_call_id", "response_id", "item_id", "call_id"):
            # 变量说明：text 表示当前步骤使用的 text 值。
            text = _public_event_text(source.get(key), limit=160)
            if text is not None:
                public[key] = text
        # 变量说明：arguments 表示当前流程使用的 arguments 集合。
        arguments = _public_tool_argument_summary(source.get("arguments"))
        if arguments:
            public["arguments"] = arguments
        # tool_finished 的 result_summary 已由运行时按工具类型生成并做过边界限制；
        # 这里只透传这个安全摘要，不暴露原始工具结果或 metadata。
        if event_type in _TOOL_FINISH_EVENT_TYPES:
            result_summary = _public_event_text(source.get("result_summary"), limit=480)
            if result_summary is not None:
                public["result_summary"] = result_summary

    if event_type in {"assistant_message_started", "assistant_message_delta", "assistant_message_completed", "model_response_completed"}:
        for key in ("response_id", "item_id"):
            text = _public_event_text(source.get(key), limit=160)
            if text is not None:
                public[key] = text
        for key in ("response_id", "item_id", "output_index", "step"):
            value = source.get(key)
            if isinstance(value, int) and not isinstance(value, bool):
                public[key] = value
        if isinstance(source.get("has_tool_calls"), bool):
            public["has_tool_calls"] = source["has_tool_calls"]
        phase = source.get("phase")
        if phase in {"commentary", "final_answer", "unknown"}:
            public["phase"] = phase
        content = _public_thought_text(source.get("content"), limit=100_000)
        if content is not None:
            public["content"] = content
        delta = _public_thought_text(source.get("delta"), limit=100_000)
        if delta is not None:
            public["delta"] = delta

    if event_type in {
        "context_protocol_repaired",
        "model_retry",
        "context_compaction_started",
        "context_compaction_finished",
        "context_compaction_failed",
        "completion_verification_started",
        "completion_verification_rejected",
        "completion_verification_passed",
        "progress",
        "agent_progress",
        "thought_summary",
        "activity_update",
    }:
        for key in ("reason", "failure_reason", "phase", "model", "error_kind", "error_type", "progress", "summary", "status_text", "activity"):
            # 变量说明：text 表示当前步骤使用的 text 值。
            text = _public_thought_text(source.get(key)) if event_type == "thought_summary" and key == "summary" else _public_event_text(source.get(key), limit=240)
            if text is not None:
                public[key] = text

    if event_type == "approval_requested":
        # 变量说明：request 表示调用方传入的请求数据。
        request = source.get("request")
        if isinstance(request, dict):
            # 变量说明：request_public 表示当前步骤使用的 request_public 值。
            request_public: dict[str, Any] = {}
            # 变量说明：tool_name 表示当前步骤使用的 tool_name 值。
            tool_name = _public_event_text(request.get("tool_name"), limit=160)
            if tool_name is not None:
                request_public["tool_name"] = tool_name
            # 变量说明：arguments 表示当前流程使用的 arguments 集合。
            arguments = _public_tool_argument_summary(request.get("arguments"))
            if arguments:
                request_public["arguments"] = arguments
            # 变量说明：count 表示当前步骤使用的 count 值。
            count = request.get("remaining_call_count")
            if isinstance(count, (int, float)) and not isinstance(count, bool):
                request_public["remaining_call_count"] = count
            if request_public:
                # 变量说明：public 的索引项 表示该语句创建或更新的目标数据。
                public["request"] = request_public

    if event_type in {"mcp_catalog_loading", "mcp_connecting"}:
        # 变量说明：servers 表示当前流程使用的 servers 集合。
        servers = source.get("servers")
        if isinstance(servers, list):
            public["servers"] = [
                text for item in servers
                if (text := _public_event_text(item, limit=100)) is not None
            ]
    if event_type in {"mcp_ready", "mcp_degraded"}:
        # 变量说明：failed_servers 表示当前流程使用的 failed_servers 集合。
        failed_servers = source.get("failed_servers")
        if isinstance(failed_servers, list):
            public["failed_servers"] = [
                {
                    key: text
                    for key in ("name", "error_code")
                    if (text := _public_event_text(item.get(key), limit=100)) is not None
                }
                for item in failed_servers
                if isinstance(item, dict)
            ]
        for key in ("dormant_servers", "connected_servers"):
            # 变量说明：values 表示当前流程使用的 values 集合。
            values = source.get(key)
            if isinstance(values, list):
                public[key] = [
                    text for item in values
                    if (text := _public_event_text(item, limit=100)) is not None
                ]
    if event_type == "mcp_server_ready":
        # 变量说明：server 表示当前步骤使用的 server 值。
        server = _public_event_text(source.get("server"), limit=100)
        if server is not None:
            public["server"] = server

    if event_type == "terminal_response_persisted":
        for key in ("turn_id", "message_id", "trace_id", "status", "error_code", "source"):
            # 变量说明：text 表示当前步骤使用的 text 值。
            text = _public_event_text(source.get(key), limit=200)
            if text is not None:
                public[key] = text

    if event_type in {"turn_completed", "turn_failed", "turn_stopped"}:
        for key in ("turn_id", "message_id", "status", "error_code"):
            text = _public_event_text(source.get(key), limit=200)
            if text is not None:
                public[key] = text

    if event_type.startswith("delegated_child_"):
        for key in ("task_id", "delegation_id", "child_run_id", "child_agent_id", "child_agent_name", "task_title", "status"):
            # 变量说明：text 表示当前步骤使用的 text 值。
            text = _public_event_text(source.get(key), limit=200)
            if text is not None:
                public[key] = text

    # Controlled status labels are useful for diagnostics, while free-form
    # error/reason/output text is intentionally kept out of this endpoint.
    if event_type in _TERMINAL_EVENT_TYPES | {"run_interrupted", "approval_rejected"}:
        for key in ("code", "error_type", "reason"):
            # 变量说明：text 表示当前步骤使用的 text 值。
            text = _public_event_text(source.get(key), limit=160)
            if text is not None:
                public[key] = text
    # An explicit user interruption is the one exception where the browser
    # needs the bounded partial draft to offer "重新编辑" after an SSE
    # reconnect.  It is not copied into ChatMessage/context and is never
    # exposed for ordinary completed or failed runs.
    if event_type == "run_interrupted":
        for key in ("partial_output", "partial_thought"):
            # 变量说明：text 表示当前步骤使用的 text 值。
            text = _public_event_text(source.get(key), limit=100_000)
            if text is not None:
                public[key] = text
    return public


# 函数职责：完成 public_run_event 对应的业务处理。
# 参数关系：event 表示当前运行事件。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def _public_run_event(event: RunEvent) -> RunEventRead | None:
    """Return one safe timeline event, omitting private snapshots/checkpoints."""

    # 变量说明：event_type 表示当前步骤使用的 event_type 值。
    event_type = str(event.event_type or "").strip().casefold()
    # Snapshots (including future checkpoint event names) remain in SQLite for
    # recovery only.  Their full payload is never a front-end API response.
    if "checkpoint" in event_type or event_type.endswith("_snapshot"):
        return None
    if event_type not in _PUBLIC_RUN_EVENT_TYPES:
        return None
    return RunEventRead(
        id=event.id,
        run_id=event.run_id,
        event_type=event_type,
        trace_id=event.trace_id,
        sequence=event.sequence,
        step=event.step,
        payload=_public_run_event_payload(event_type, event.payload),
        created_at=event.created_at,
    )


# 函数职责：完成 normalized_workspace_root 对应的业务处理。
# 参数关系：root_path 表示root_path 对应的文件系统位置。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def _normalized_workspace_root(root_path: str) -> str:
    """Return the canonical absolute root stored for a selected project."""

    return str(Path(root_path).expanduser().resolve())


# 函数职责：完成 workspace_name_from_root 对应的业务处理。
# 参数关系：root_path 表示root_path 对应的文件系统位置。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def _workspace_name_from_root(root_path: str) -> str:
    """Derive a project label from an absolute path, including filesystem roots."""

    # 变量说明：name 表示当前对象名称。
    name = Path(root_path).name.strip()
    return name or "项目"
