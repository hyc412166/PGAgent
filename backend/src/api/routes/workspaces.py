"""Workspace lifecycle endpoints."""

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
from src.sessions.deletion import (
    SessionDeletionConflict,
    finalize_session_deletions,
    stage_session_deletions,
)
router = APIRouter(prefix="/api", tags=["workspaces"])

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

@router.get("/workspaces", response_model=list[WorkspaceRead])
def list_workspaces(db: Session = Depends(get_db)) -> list[Workspace]:
    return list(db.scalars(select(Workspace).order_by(Workspace.updated_at.desc())))


@router.post("/workspaces", response_model=WorkspaceRead, status_code=status.HTTP_201_CREATED)
def create_workspace(
    payload: WorkspaceCreate,
    response: Response,
    db: Session = Depends(get_db),
) -> Workspace:
    root_path = _normalized_workspace_root(payload.root_path)
    # Existing databases may contain relative roots. Compare canonical paths
    # so choosing the same folder is idempotent across old and new clients.
    for existing in db.scalars(select(Workspace).order_by(Workspace.created_at.asc())):
        if _normalized_workspace_root(existing.root_path) == root_path:
            if existing.root_path != root_path:
                existing.root_path = root_path
                _commit(db)
                db.refresh(existing)
            response.status_code = status.HTTP_200_OK
            return existing

    name = (payload.name or "").strip() or _workspace_name_from_root(root_path)
    item = Workspace(
        name=name,
        description=payload.description,
        root_path=root_path,
        enabled=payload.enabled,
        validation_runtime=payload.validation_runtime.model_dump(exclude_none=True),
    )
    db.add(item)
    _commit(db)
    db.refresh(item)
    return item


@router.get("/workspaces/{workspace_id}", response_model=WorkspaceRead)
def get_workspace(workspace_id: str, db: Session = Depends(get_db)) -> Workspace:
    return _require(db, Workspace, workspace_id, "Workspace")


@router.patch("/workspaces/{workspace_id}", response_model=WorkspaceRead)
def update_workspace(
    workspace_id: str, payload: WorkspaceUpdate, db: Session = Depends(get_db)
) -> Workspace:
    item = _require(db, Workspace, workspace_id, "Workspace")
    _apply(item, payload)
    if payload.root_path is not None:
        item.root_path = _normalized_workspace_root(payload.root_path)
    _commit(db)
    db.refresh(item)
    refresh_memory_markdown_projection()
    return item


@router.delete("/workspaces/{workspace_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_workspace(workspace_id: str, db: Session = Depends(get_db)) -> Response:
    item = _require(db, Workspace, workspace_id, "Workspace")
    if item.id == DEFAULT_WORKSPACE_ID:
        raise HTTPException(status_code=409, detail="The default workspace cannot be deleted")
    conversations = list(
        db.scalars(
            select(ChatSession)
            .where(ChatSession.workspace_id == workspace_id)
            .order_by(ChatSession.created_at.asc(), ChatSession.id.asc())
        )
    )
    try:
        effects = stage_session_deletions(db, conversations)
    except SessionDeletionConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    db.execute(delete(Memory).where(Memory.scope == "workspace", Memory.scope_id == workspace_id))
    db.delete(item)
    _commit(db)
    finalize_session_deletions(effects)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# Agents ---------------------------------------------------------------------
