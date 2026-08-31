"""Conversation compaction persistence and history preparation."""

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
    Memory,
    MemoryJob,
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
from src.attachments.storage import attachment_message_content
from src.context.assembly import COMPACTION_SCHEMA, CONTINUATION_PREFIX
from src.tools import create_default_registry
from src.tools.registry import TOOL_SCHEMAS
from src.tools.types import ToolResult

from src.tasks import background as background_job_service
from src.model.gateway import ModelConfigurationError, ProviderConfig, build_model_call
from src.artifacts.storage import ArtifactToolStore
from src.tasks.background import BackgroundJobToolStore
from src.memory.service import (
    MemoryToolStore,
    ensure_user_memory_snapshot,
    extraction_prompt,
    memory_payload,
    parse_extraction_response,
    recall_memories,
    refresh_memory_markdown_projection,
    render_memory_snapshot,
    store_memory,
    visible_memory_query,
)
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
    provider_payload = message.provider_payload if isinstance(message.provider_payload, dict) else {}
    rendered_content = provider_payload.get("rendered_content")
    attachment_refs = provider_payload.get("attachment_refs")
    user_content: Any = (
        rendered_content
        if message.role == "user" and isinstance(rendered_content, str)
        else message.content
    )
    if message.role == "user" and isinstance(attachment_refs, list) and attachment_refs:
        user_content = attachment_message_content(
            str(user_content),
            [item for item in attachment_refs if isinstance(item, Mapping)],
        )
    payload: dict[str, Any] = {
        "role": message.role,
        "content": user_content,
    }
    if message.tool_name:
        payload["name"] = message.tool_name
    if message.tool_call_id:
        payload["tool_call_id"] = message.tool_call_id
    metadata = message.extra if isinstance(message.extra, dict) else {}
    tool_calls = metadata.get("tool_calls")
    if message.role == "assistant" and isinstance(tool_calls, list) and tool_calls:
        payload["tool_calls"] = _json_safe(tool_calls)
    reasoning_content = provider_payload.get("reasoning_content")
    if message.role == "assistant" and isinstance(reasoning_content, str) and reasoning_content:
        payload["reasoning_content"] = reasoning_content
    native_provider = provider_payload.get("native")
    if message.role == "assistant" and isinstance(native_provider, dict):
        payload["_pgagent_provider"] = _json_safe(native_provider)
    return payload


def _compaction_from_db(db: Any, session: Session) -> dict[str, Any]:
    """Load only the newest full transcript replacement for a session."""

    compaction = db.scalar(
        select(ConversationCompaction)
        .where(ConversationCompaction.session_id == session.id)
        .order_by(
            ConversationCompaction.source_sequence.desc(),
            ConversationCompaction.created_at.desc(),
            ConversationCompaction.id.desc(),
        )
    )
    if compaction is None:
        return {}
    continuation_messages = _json_safe(compaction.continuation_messages or [])
    first_content = str(continuation_messages[0].get("content") or "") if continuation_messages else ""
    return {
        "schema": COMPACTION_SCHEMA if first_content.startswith(CONTINUATION_PREFIX) else "claude_compaction_v1",
        "id": compaction.id,
        "session_id": session.id,
        "source_sequence": int(compaction.source_sequence or 0),
        "active_request": str(compaction.active_request or ""),
        "todo_state": _json_safe(compaction.todo_state or []),
        "summary": str(compaction.summary or ""),
        "messages": continuation_messages,
        "transcript_artifact": _json_safe(compaction.transcript_artifact or {}),
        "artifact_refs": _json_safe(compaction.artifact_refs or []),
        "reason": str(compaction.reason or "threshold"),
        "before_tokens": int(compaction.before_tokens or 0),
        "after_tokens": int(compaction.after_tokens or 0),
        "removed_message_count": int(compaction.removed_message_count or 0),
        "used_model": bool(compaction.used_model),
        "fallback": bool(compaction.fallback),
        "created_at": compaction.created_at.isoformat() if compaction.created_at else None,
    }


def _chat_message_key(message: Mapping[str, Any]) -> str:
    """Stable multiset key used to append only new runtime transcript items."""

    selected = {
        "role": message.get("role"),
        "content": message.get("content"),
        "name": message.get("name"),
        "tool_call_id": message.get("tool_call_id"),
        "tool_calls": message.get("tool_calls") or [],
        "reasoning_content": message.get("reasoning_content") or "",
        "provider_payload": message.get("_pgagent_provider") or {},
    }
    return json.dumps(_json_safe(selected), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _append_runtime_transcript(
    db: Any,
    run_id: str,
    session_id: str,
    outcome: RunOutcome,
    *,
    terminal_managed: bool = False,
) -> None:
    """Persist newly produced assistant/tool items without duplicating history.

    Runtime state may contain a compacted provider view.  Only its explicit
    transcript delta is appended; compacted continuation messages remain in
    ``ConversationCompaction`` and are never duplicated as chat rows.
    """

    existing_rows = list(
        db.scalars(
            select(ChatMessage)
            .where(ChatMessage.session_id == session_id)
            .order_by(ChatMessage.sequence.asc(), ChatMessage.created_at.asc(), ChatMessage.id.asc())
        )
    )
    existing = Counter(_chat_message_key(_message_payload(row)) for row in existing_rows)
    next_sequence = max((int(getattr(row, "sequence", 0) or 0) for row in existing_rows), default=0) + 1
    final_reply_persisted = False
    runtime_messages = list(outcome.messages if outcome.transcript_delta is None else outcome.transcript_delta)
    accepted_output = str(outcome.output or "").strip()
    terminal_candidate_index: int | None = None
    if terminal_managed and accepted_output:
        for index, candidate in enumerate(runtime_messages):
            if (
                isinstance(candidate, dict)
                and str(candidate.get("role") or "").strip().lower() == "assistant"
                and not candidate.get("tool_calls")
                and str(candidate.get("content") or "").strip() == accepted_output
            ):
                terminal_candidate_index = index
    for index, raw in enumerate(runtime_messages):
        if not isinstance(raw, dict):
            continue
        role = str(raw.get("role") or "").strip().lower()
        if role == "system" or not role:
            continue
        if terminal_managed and index == terminal_candidate_index:
            # The turn finalizer writes the accepted output with a database
            # exactly-once key. Keeping a second provider transcript copy would
            # duplicate it on the next context reconstruction.
            continue
        content = str(raw.get("content") or "")
        if role == "tool" and content.startswith("[artifact:"):
            closing = content.find("]")
            artifact_id = content[len("[artifact:") : closing] if closing > 0 else ""
            ref = next(
                (
                    item for item in outcome.artifact_refs
                    if str(item.get("artifact_id") or item.get("id") or "") == artifact_id
                ),
                None,
            )
            storage_path = str((ref or {}).get("storage_key") or "")
            if storage_path:
                path = Path(storage_path)
                if path.is_file():
                    content = path.read_bytes().decode("utf-8", errors="replace")
        payload = {
            "role": role,
            "content": content,
        }
        if raw.get("name"):
            payload["name"] = str(raw["name"])
        if raw.get("tool_call_id"):
            payload["tool_call_id"] = str(raw["tool_call_id"])
        if role == "assistant" and isinstance(raw.get("tool_calls"), list):
            payload["tool_calls"] = _json_safe(raw["tool_calls"])
        if role == "assistant" and isinstance(raw.get("reasoning_content"), str):
            payload["reasoning_content"] = str(raw["reasoning_content"])
        native_provider = raw.get("_pgagent_provider")
        if role == "assistant" and isinstance(native_provider, dict):
            payload["_pgagent_provider"] = _json_safe(native_provider)
        if role == "tool" and payload.get("name") == "task" and payload.get("tool_call_id"):
            existing_task_result = db.scalar(
                select(ChatMessage)
                .where(
                    ChatMessage.session_id == session_id,
                    ChatMessage.role == "tool",
                    ChatMessage.tool_name == "task",
                    ChatMessage.tool_call_id == payload["tool_call_id"],
                )
                .order_by(ChatMessage.sequence.desc(), ChatMessage.created_at.desc())
            )
            if existing_task_result is not None:
                # A delegated terminal result is a new observation, not a
                # mutation of the placeholder row. Store it as a provider-valid
                # assistant observation so the append-only log gains a new
                # sequence without introducing an orphan tool result.
                db.add(ChatMessage(
                    session_id=session_id,
                    role="assistant",
                    content=(
                        "Delegated task terminal result:\n"
                        f"{payload['content']}"
                    ),
                    sequence=next_sequence,
                    extra={
                        "runtime_run_id": run_id,
                        "source": "runtime_transcript",
                        "delegated_result_revision": True,
                        "supersedes_message_id": existing_task_result.id,
                        "tool_call_id": payload["tool_call_id"],
                    },
                ))
                next_sequence += 1
                continue
        key = _chat_message_key(payload)
        if existing[key] > 0:
            existing[key] -= 1
            continue
        # ``run_id`` remains reserved for the user-visible final assistant
        # reply for backwards-compatible chat APIs. Internal tool turns are
        # still durable, but use ``runtime_run_id`` so they do not appear as
        # duplicate assistant replies in existing consumers.
        is_final_assistant = (
            not terminal_managed
            and role == "assistant"
            and not payload.get("tool_calls")
        )
        final_reply_persisted = final_reply_persisted or is_final_assistant
        metadata: dict[str, Any] = {
            ("run_id" if is_final_assistant else "runtime_run_id"): run_id,
            "source": "runtime_transcript",
        }
        if payload.get("tool_calls"):
            metadata["tool_calls"] = payload["tool_calls"]
        db.add(
            ChatMessage(
                session_id=session_id,
                role=role,
                content=payload["content"],
                tool_name=payload.get("name"),
                tool_call_id=payload.get("tool_call_id"),
                sequence=next_sequence,
                extra=metadata,
                provider_payload=(
                    {
                        **({"reasoning_content": payload["reasoning_content"]} if payload.get("reasoning_content") else {}),
                        **({"native": payload["_pgagent_provider"]} if payload.get("_pgagent_provider") else {}),
                    }
                ),
            )
        )
        next_sequence += 1
        # Consume this item in case the same runtime message appears twice in
        # one outcome (for example a resumed approval path).
        existing[key] = 0

    # Tool-driven terminal paths (for example ``question``) carry their final
    # user-facing text in RunOutcome.output but intentionally do not append a
    # second assistant provider message to the tool transcript.
    if outcome.output and not final_reply_persisted and not terminal_managed:
        final_payload = {"role": "assistant", "content": str(outcome.output)}
        db.add(ChatMessage(
            session_id=session_id,
            role="assistant",
            content=str(outcome.output),
            sequence=next_sequence,
            extra={"run_id": run_id, "source": "runtime_transcript"},
        ))


def _compaction_source_is_current(db: Any, session: Session, outcome: RunOutcome) -> bool | None:
    """Check the append-only cursor captured before this runtime began."""

    compaction = dict(outcome.compaction_state or {})
    if not compaction or compaction.get("source_sequence") is None:
        return None
    if compaction.get("id"):
        return None
    source_sequence = max(0, int(compaction.get("source_sequence") or 0))
    delta_count = max(0, int(compaction.get("source_delta_count") or 0))
    expected_base = max(
        0,
        int(compaction.get("base_sequence") or (source_sequence - delta_count)),
    )
    current = int(db.scalar(
        select(func.max(ChatMessage.sequence)).where(ChatMessage.session_id == session.id)
    ) or 0)
    return current == expected_base


def _persist_conversation_compaction(
    db: Any,
    session: Session,
    run_id: str,
    outcome: RunOutcome,
    *,
    source_sequence_valid: bool | None = None,
) -> None:
    """Persist one immutable full-compaction replacement."""

    compaction = dict(outcome.compaction_state or {})
    all_artifact_refs = [
        *list(outcome.artifact_refs or []),
        *list(compaction.get("artifact_refs") or []),
    ]
    _persist_artifact_refs(db, session.id, all_artifact_refs)
    if not compaction or compaction.get("schema") not in {"claude_compaction_v1", COMPACTION_SCHEMA}:
        return

    source_sequence = max(0, int(compaction.get("source_sequence") or 0))
    current_sequence = int(db.scalar(
        select(func.max(ChatMessage.sequence)).where(ChatMessage.session_id == session.id)
    ) or 0)
    existing = db.scalar(select(ConversationCompaction).where(
        ConversationCompaction.session_id == session.id,
        ConversationCompaction.source_sequence == source_sequence,
    ))
    if existing is not None:
        session.context_tokens = max(0, int(existing.after_tokens or session.context_tokens))
        return
    newer = db.scalar(
        select(ConversationCompaction.id).where(
            ConversationCompaction.session_id == session.id,
            ConversationCompaction.source_sequence > source_sequence,
        )
    )
    if source_sequence_valid is False or source_sequence > current_sequence or newer is not None:
        db.add(RunEvent(
            run_id=run_id,
            event_type="context_compaction_conflict",
            payload={
                "source_sequence": source_sequence,
                "current_sequence": current_sequence,
                "newer_compaction": newer is not None,
            },
        ))
        return

    finished = next(
        (
            event for event in reversed(outcome.events)
            if event.get("type") == "context_compaction_finished"
            and bool(event.get("effective"))
        ),
        {},
    )
    messages = [
        _json_safe(message)
        for message in compaction.get("messages") or []
        if isinstance(message, dict)
    ]
    first_content = str(messages[0].get("content") or "") if messages else ""
    if not messages or not first_content.startswith(("<compacted-context>", CONTINUATION_PREFIX)):
        db.add(RunEvent(
            run_id=run_id,
            event_type="context_compaction_rejected",
            payload={"reason": "missing_compacted_continuation", "source_sequence": source_sequence},
        ))
        return

    db.add(ConversationCompaction(
        session_id=session.id,
        source_run_id=run_id,
        source_sequence=source_sequence,
        active_request=str(compaction.get("active_request") or ""),
        todo_state=_json_safe(compaction.get("todo_state") or []),
        summary=str(compaction.get("summary") or ""),
        continuation_messages=messages,
        transcript_artifact=_json_safe(compaction.get("transcript_artifact") or {}),
        artifact_refs=_json_safe(compaction.get("artifact_refs") or []),
        reason=str(finished.get("reason") or compaction.get("reason") or "threshold"),
        before_tokens=max(0, int(finished.get("before_tokens") or compaction.get("before_tokens") or 0)),
        after_tokens=max(0, int(finished.get("after_tokens") or compaction.get("after_tokens") or 0)),
        removed_message_count=max(0, int(compaction.get("removed_message_count") or 0)),
        used_model=bool(finished.get("used_model") or compaction.get("used_model")),
        fallback=bool(finished.get("fallback") or compaction.get("fallback")),
    ))
    session.context_tokens = max(
        0,
        int(finished.get("after_tokens") or compaction.get("after_tokens") or session.context_tokens),
    )



def _persist_artifact_refs(
    db: Any,
    session_id: str,
    refs: list[dict[str, Any]],
) -> None:
    """Persist artifact metadata idempotently; bytes already live in storage_key."""

    seen: set[str] = set()
    for raw_ref in refs:
        if not isinstance(raw_ref, dict):
            continue
        artifact_id = str(raw_ref.get("artifact_id") or raw_ref.get("id") or "").strip()
        storage_path = str(raw_ref.get("storage_key") or "").strip()
        if not artifact_id or not storage_path or storage_path in seen:
            continue
        seen.add(storage_path)
        existing = db.scalar(select(Artifact).where(
            Artifact.session_id == session_id,
            Artifact.storage_path == storage_path,
        ))
        if existing is not None:
            continue
        db.add(Artifact(
            session_id=session_id,
            kind=str(raw_ref.get("kind") or "tool_output"),
            name=artifact_id[:255],
            storage_path=storage_path,
            sha256=str(raw_ref.get("sha256") or "") or None,
            mime_type=str(raw_ref.get("mime_type") or "text/plain"),
            size_bytes=max(0, int(raw_ref.get("size") or 0)),
            preview=str(raw_ref.get("preview") or ""),
            metadata_json={"runtime_artifact_id": artifact_id},
        ))


def _prepare_session_history(db: Any, session: Session) -> list[dict[str, Any]]:
    """Rebuild the provider transcript from one compaction plus its new tail."""

    compaction = _compaction_from_db(db, session)
    boundary = max(0, int(compaction.get("source_sequence") or 0))
    query = select(ChatMessage).where(ChatMessage.session_id == session.id)
    if compaction:
        query = query.where(ChatMessage.sequence > boundary)
    rows = list(db.scalars(query.order_by(
        ChatMessage.sequence.asc(),
        ChatMessage.created_at.asc(),
        ChatMessage.id.asc(),
    )))
    messages = [
        *[
            dict(message)
            for message in compaction.get("messages") or []
            if isinstance(message, dict)
        ],
        *[_message_payload(row) for row in rows],
    ]
    session.context_tokens = min(
        sum(message_tokens(message) for message in messages),
        settings.context_limit_tokens,
    )
    return messages
