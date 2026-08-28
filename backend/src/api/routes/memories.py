"""Workspace memory CRUD and recall endpoints."""

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
    MemorySettingsRead,
    MemorySettingsUpdate,
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
from src.memory.preferences import get_memory_settings
from src.memory.service import recall_memories, refresh_memory_markdown_projection, store_memory
from src.skills.registry import replace_agent_capabilities, replace_session_skills
from src.tasks.state import latest_resumable_task, task_payload
from src.agents.collaboration import cleanup_session_worktrees
router = APIRouter(prefix="/api", tags=["memories"])

from src.api.routes.shared import (
    _ACTIVE_SESSION_RUN_STATUSES,
    _SESSION_RUNTIME_SETTING_FIELDS,
    _apply,
    _commit,
    _normalized_workspace_root,
    _public_run_event,
    _require,
    _require_enabled_model_connection,
    _workspace_name_from_root,
)


@router.get("/memories/settings", response_model=MemorySettingsRead)
def read_memory_settings(db: Session = Depends(get_db)) -> MemorySettingsRead:
    return MemorySettingsRead(enabled=get_memory_settings(db).enabled)


@router.put("/memories/settings", response_model=MemorySettingsRead)
def update_memory_settings(
    payload: MemorySettingsUpdate,
    db: Session = Depends(get_db),
) -> MemorySettingsRead:
    item = get_memory_settings(db)
    item.enabled = payload.enabled
    _commit(db)
    return MemorySettingsRead(enabled=item.enabled)

@router.get("/memory-recalls")
def list_memory_recalls(
    session_id: str | None = None,
    limit: int = Query(default=20, ge=1, le=100),
    db: Session = Depends(get_db),
) -> list[dict[str, Any]]:
    query = select(ChatMessage).where(ChatMessage.role == "user")
    if session_id:
        query = query.where(ChatMessage.session_id == session_id)
    rows = list(db.scalars(query.order_by(
        ChatMessage.created_at.desc(), ChatMessage.sequence.desc(), ChatMessage.id.desc(),
    ).limit(limit)))
    result: list[dict[str, Any]] = []
    for row in rows:
        provider_payload = row.provider_payload if isinstance(row.provider_payload, dict) else {}
        snapshot = provider_payload.get("memory_snapshot")
        if not isinstance(snapshot, list):
            continue
        result.append({
            "message_id": row.id,
            "session_id": row.session_id,
            "turn_id": row.turn_id,
            "request": row.content,
            "memories": [
                {
                    "id": item.get("id"),
                    "name": item.get("name"),
                    "memory_type": item.get("memory_type"),
                    "updated_at": item.get("updated_at"),
                }
                for item in snapshot if isinstance(item, dict)
            ],
            "created_at": row.created_at,
        })
    return result


@router.get("/memories", response_model=list[MemoryRead])
def list_memories(
    scope: str | None = None,
    scope_id: str | None = None,
    pinned: bool | None = None,
    memory_type: str | None = None,
    memory_status: str = "active",
    db: Session = Depends(get_db),
) -> list[Memory]:
    query = select(Memory)
    if scope:
        query = query.where(Memory.scope == scope)
    if scope_id:
        query = query.where(Memory.scope_id == scope_id)
    if pinned is not None:
        query = query.where(Memory.pinned == pinned)
    if memory_type:
        query = query.where(Memory.memory_type == memory_type)
    if memory_status:
        query = query.where(Memory.status == memory_status)
    return list(db.scalars(query.order_by(Memory.pinned.desc(), Memory.updated_at.desc())))


@router.post("/memories", response_model=MemoryRead, status_code=status.HTTP_201_CREATED)
def create_memory(payload: MemoryCreate, db: Session = Depends(get_db)) -> Memory:
    if payload.scope == "workspace" and db.get(Workspace, payload.scope_id) is None:
        raise HTTPException(status_code=422, detail="scope_id does not reference an existing workspace")
    if payload.scope == "session" and db.get(ChatSession, payload.scope_id) is None:
        raise HTTPException(status_code=422, detail="scope_id does not reference an existing session")
    try:
        item = store_memory(
            db,
            name=payload.name or payload.title,
            content=payload.content,
            memory_type=payload.memory_type,
            description=payload.description,
            tags=payload.tags,
            scope=payload.scope,
            workspace_id=payload.scope_id if payload.scope == "workspace" else None,
            session_id=payload.scope_id if payload.scope == "session" else None,
            pinned=payload.pinned,
            metadata=payload.metadata,
        )
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail="an active memory already uses that name in this scope") from exc
    _commit(db)
    db.refresh(item)
    refresh_memory_markdown_projection()
    return item


@router.get("/memories/search", response_model=list[MemoryRead])
def search_memories(
    query: str = Query(..., min_length=1),
    workspace_id: str | None = None,
    session_id: str | None = None,
    limit: int = Query(default=5, ge=1, le=5),
    db: Session = Depends(get_db),
) -> list[Memory]:
    snapshots = recall_memories(
        db,
        query,
        workspace_id=workspace_id,
        session_id=session_id,
        limit=limit,
    )
    ids = [str(item["id"]) for item in snapshots]
    rows = {item.id: item for item in db.scalars(select(Memory).where(Memory.id.in_(ids)))}
    return [rows[item_id] for item_id in ids if item_id in rows]


@router.patch("/memories/{memory_id}", response_model=MemoryRead)
def update_memory(memory_id: str, payload: MemoryUpdate, db: Session = Depends(get_db)) -> Memory:
    item = _require(db, Memory, memory_id, "Memory")
    changes = payload.model_dump(exclude_none=True)
    requested_status = changes.pop("status", None)
    if requested_status is not None:
        if requested_status != "archived" or changes:
            raise HTTPException(
                status_code=409,
                detail="status changes may only archive a memory; create a new version to restore it",
            )
        item.status = "archived"
        _commit(db)
        db.refresh(item)
        refresh_memory_markdown_projection()
        return item
    if item.status != "active":
        raise HTTPException(status_code=409, detail="only an active memory can be versioned")
    if not changes:
        return item
    next_name = str(changes.get("name") or changes.get("title") or item.name)
    renamed = next_name.casefold() != item.name.casefold()
    if renamed:
        name_conflict = db.scalar(select(Memory.id).where(
            Memory.id != item.id,
            Memory.scope == item.scope,
            Memory.scope_id.is_(None) if item.scope_id is None else Memory.scope_id == item.scope_id,
            Memory.status == "active",
            func.lower(Memory.name) == next_name.casefold(),
        ))
        if name_conflict is not None:
            raise HTTPException(status_code=409, detail="an active memory already uses that name in this scope")
    try:
        next_item = store_memory(
            db,
            name=next_name,
            content=str(changes.get("content", item.content)),
            memory_type=str(changes.get("memory_type", item.memory_type)),
            description=str(changes.get("description", item.description)),
            tags=changes.get("tags", item.tags or []),
            scope=item.scope,
            workspace_id=item.scope_id if item.scope == "workspace" else None,
            session_id=(item.scope_id if item.scope == "session" else item.source_session_id),
            source_turn_id=item.source_turn_id,
            pinned=bool(changes.get("pinned", item.pinned)),
            metadata=changes.get("metadata", item.extra or {}),
            force_new=True,
        )
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail="an active memory already uses that name in this scope") from exc
    if renamed:
        item.status = "superseded"
        item.superseded_by = next_item.id
    _commit(db)
    db.refresh(next_item)
    refresh_memory_markdown_projection()
    return next_item


@router.delete("/memories/{memory_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_memory(memory_id: str, db: Session = Depends(get_db)) -> Response:
    _require(db, Memory, memory_id, "Memory").status = "archived"
    _commit(db)
    refresh_memory_markdown_projection()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.delete("/memories", status_code=status.HTTP_204_NO_CONTENT)
def clear_memories(
    scope: str = Query(...), scope_id: str | None = None, db: Session = Depends(get_db)
) -> Response:
    query = update(Memory).where(Memory.scope == scope)
    if scope_id is None:
        query = query.where(Memory.scope_id.is_(None))
    else:
        query = query.where(Memory.scope_id == scope_id)
    db.execute(query.values(status="archived"))
    _commit(db)
    refresh_memory_markdown_projection()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
