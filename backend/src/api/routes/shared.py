"""Shared API serialization and resource lookup helpers."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
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
T = TypeVar("T")
_ACTIVE_SESSION_RUN_STATUSES = frozenset(
    {"received", "preparing_context", "planning", "acting", "observing", "running", "awaiting_approval"}
)
_SESSION_RUNTIME_SETTING_FIELDS = frozenset({
    "model_connection_id", "model_id", "thinking_level", "use_memories", "mcp_server_names",
})
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
    "run_completed",
    "run_interrupted",
    "run_stopped",
    "stopped",
    "terminal_response_persisted",
    "tool_call",
    "tool_finished",
    "tool_result",
    "tool_started",
    "user_question_requested",
})
_TOOL_START_EVENT_TYPES = frozenset({"tool_call", "tool_started"})
_TOOL_FINISH_EVENT_TYPES = frozenset({"tool_finished", "tool_result"})
_TERMINAL_EVENT_TYPES = frozenset({
    "completed", "failed", "integration_failed", "model_failed", "run_completed", "run_interrupted", "run_stopped", "stopped"
})
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
_PUBLIC_EVENT_BOOLEAN_FIELDS = frozenset({
    "accepted", "complete",
    "changed", "has_output", "ok", "pending_approval", "requires_next_message", "terminal",
})


def _require(db: Session, model: type[T], object_id: str, label: str) -> T:
    item = db.get(model, object_id)
    if item is None:
        raise HTTPException(status_code=404, detail=f"{label} not found")
    return item


def _commit(db: Session, conflict_message: str = "Related resource does not exist") -> None:
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=conflict_message) from exc


def _apply(item: Any, payload: Any, *, field_map: dict[str, str] | None = None) -> None:
    mapping = field_map or {}
    for key, value in payload.model_dump(exclude_unset=True).items():
        setattr(item, mapping.get(key, key), value)


def _require_enabled_model_connection(db: Session, connection_id: str | None) -> None:
    if connection_id is None:
        return
    connection = db.get(ModelConnection, connection_id)
    if connection is None:
        raise HTTPException(status_code=409, detail="Selected model connection does not exist")
    if not connection.enabled:
        raise HTTPException(status_code=409, detail="Selected model connection is disabled")


def _public_event_text(value: Any, *, limit: int = 160) -> str | None:
    """Return a bounded display label, never arbitrary event payload content."""

    if not isinstance(value, str):
        return None
    return " ".join(value.replace("\x00", "").split())[:limit] or None


def _public_thought_text(value: Any, *, limit: int = 20_000) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.replace("\x00", "").replace("\r\n", "\n").replace("\r", "\n")[:limit]
    return text or None


def _public_event_url(value: Any) -> str | None:
    """Keep a URL target useful to the timeline without query/fragment secrets."""

    text = _public_event_text(value, limit=2_048)
    if text is None:
        return None
    try:
        parsed = urlsplit(text)
    except ValueError:
        return None
    host = parsed.hostname
    if parsed.scheme not in {"http", "https"} or not host:
        return None
    try:
        port = parsed.port
    except ValueError:
        return None
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    netloc = f"{host}:{port}" if port is not None else host
    return urlunsplit((parsed.scheme, netloc, parsed.path, "", ""))[:300]


_PRIVATE_ARGUMENT_MARKERS = (
    "api_key", "apikey", "authorization", "token", "secret", "password",
    "cookie", "content", "old_string", "new_string",
)


def _public_tool_argument_summary(value: Any) -> dict[str, Any]:
    """Expose only a display target from a tool's already-scrubbed summary.

    Runtime events are normally written with ``safe_tool_argument_summary``.
    The HTTP read boundary still cannot trust every historical or manually
    created row, so it deliberately accepts no arbitrary argument keys.
    """

    if not isinstance(value, dict):
        return {}
    public: dict[str, Any] = {}
    for raw_key, raw_value in value.items():
        key = str(raw_key)[:80]
        lowered = key.casefold()
        if any(marker in lowered for marker in _PRIVATE_ARGUMENT_MARKERS):
            continue
        if key in {"path", "file_path", "target"}:
            text = _public_event_text(raw_value, limit=300)
            if text is not None:
                public[key] = text
            continue
        if key == "url":
            url = _public_event_url(raw_value)
            if url is not None:
                public[key] = url
            continue
        if key == "command" and isinstance(raw_value, dict):
            command: dict[str, Any] = {}
            command_text = _public_event_text(raw_value.get("text"), limit=320)
            if command_text is not None:
                command["text"] = command_text
            executable = _public_event_text(raw_value.get("executable"), limit=120)
            if executable is not None:
                command["executable"] = executable
            argument_count = raw_value.get("argument_count")
            if isinstance(argument_count, int) and not isinstance(argument_count, bool):
                command["argument_count"] = max(0, argument_count)
            if command:
                public[key] = command
            continue
        if isinstance(raw_value, dict):
            metadata = {
                metadata_key: metadata_value
                for metadata_key in ("text", "chars", "count", "argument_count", "provided")
                if isinstance((metadata_value := raw_value.get(metadata_key)), (int, float, bool))
            }
            text_value = _public_event_text(raw_value.get("text"), limit=320)
            if text_value is not None:
                metadata["text"] = text_value
            if metadata:
                public[key] = metadata
            continue
        if isinstance(raw_value, str):
            public[key] = {"chars": len(raw_value)}
        elif isinstance(raw_value, (int, float, bool)) or raw_value is None:
            public[key] = raw_value
    return public


def _public_run_event_payload(event_type: str, payload: Any) -> dict[str, Any]:
    """Project a persisted event into the minimal timeline-safe contract.

    A RunEvent is also used as the private runtime checkpoint store.  Never
    expose an unfiltered payload here: it can contain model messages, original
    arguments/results, credential references, or loaded Skill source text.
    """

    source = payload if isinstance(payload, dict) else {}
    public: dict[str, Any] = {}
    for key in _PUBLIC_EVENT_NUMBER_FIELDS:
        value = source.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            public[key] = value
    for key in _PUBLIC_EVENT_BOOLEAN_FIELDS:
        value = source.get(key)
        if isinstance(value, bool):
            public[key] = value

    if event_type in _TOOL_START_EVENT_TYPES | _TOOL_FINISH_EVENT_TYPES | {"user_question_requested"}:
        for key in ("tool_name", "tool_call_id"):
            text = _public_event_text(source.get(key), limit=160)
            if text is not None:
                public[key] = text
        arguments = _public_tool_argument_summary(source.get("arguments"))
        if arguments:
            public["arguments"] = arguments

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
            text = _public_thought_text(source.get(key)) if event_type == "thought_summary" and key == "summary" else _public_event_text(source.get(key), limit=240)
            if text is not None:
                public[key] = text

    if event_type == "approval_requested":
        request = source.get("request")
        if isinstance(request, dict):
            request_public: dict[str, Any] = {}
            tool_name = _public_event_text(request.get("tool_name"), limit=160)
            if tool_name is not None:
                request_public["tool_name"] = tool_name
            arguments = _public_tool_argument_summary(request.get("arguments"))
            if arguments:
                request_public["arguments"] = arguments
            count = request.get("remaining_call_count")
            if isinstance(count, (int, float)) and not isinstance(count, bool):
                request_public["remaining_call_count"] = count
            if request_public:
                public["request"] = request_public

    if event_type in {"mcp_catalog_loading", "mcp_connecting"}:
        servers = source.get("servers")
        if isinstance(servers, list):
            public["servers"] = [
                text for item in servers
                if (text := _public_event_text(item, limit=100)) is not None
            ]
    if event_type in {"mcp_ready", "mcp_degraded"}:
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
            values = source.get(key)
            if isinstance(values, list):
                public[key] = [
                    text for item in values
                    if (text := _public_event_text(item, limit=100)) is not None
                ]
    if event_type == "mcp_server_ready":
        server = _public_event_text(source.get("server"), limit=100)
        if server is not None:
            public["server"] = server

    if event_type == "terminal_response_persisted":
        for key in ("turn_id", "message_id", "trace_id", "status", "error_code", "source"):
            text = _public_event_text(source.get(key), limit=200)
            if text is not None:
                public[key] = text

    if event_type.startswith("delegated_child_"):
        for key in ("task_id", "delegation_id", "child_run_id", "child_agent_id", "child_agent_name", "task_title", "status"):
            text = _public_event_text(source.get(key), limit=200)
            if text is not None:
                public[key] = text

    # Controlled status labels are useful for diagnostics, while free-form
    # error/reason/output text is intentionally kept out of this endpoint.
    if event_type in _TERMINAL_EVENT_TYPES | {"run_interrupted", "approval_rejected"}:
        for key in ("code", "error_type", "reason"):
            text = _public_event_text(source.get(key), limit=160)
            if text is not None:
                public[key] = text
    # An explicit user interruption is the one exception where the browser
    # needs the bounded partial draft to offer "重新编辑" after an SSE
    # reconnect.  It is not copied into ChatMessage/context and is never
    # exposed for ordinary completed or failed runs.
    if event_type == "run_interrupted":
        for key in ("partial_output", "partial_thought"):
            text = _public_event_text(source.get(key), limit=100_000)
            if text is not None:
                public[key] = text
    return public


def _public_run_event(event: RunEvent) -> RunEventRead | None:
    """Return one safe timeline event, omitting private snapshots/checkpoints."""

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
        step=event.step,
        payload=_public_run_event_payload(event_type, event.payload),
        created_at=event.created_at,
    )


def _normalized_workspace_root(root_path: str) -> str:
    """Return the canonical absolute root stored for a selected project."""

    return str(Path(root_path).expanduser().resolve())


def _workspace_name_from_root(root_path: str) -> str:
    """Derive a project label from an absolute path, including filesystem roots."""

    name = Path(root_path).name.strip()
    return name or "项目"
