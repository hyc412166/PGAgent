"""Run records and run-event endpoints."""

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
router = APIRouter(prefix="/api", tags=["runs"])

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

@router.get("/runs", response_model=list[RunRead])
def list_runs(
    session_id: str | None = None,
    run_status: str | None = Query(default=None, alias="status"),
    limit: int = Query(default=100, ge=1, le=500),
    db: Session = Depends(get_db),
) -> list[Run]:
    query = select(Run)
    if session_id:
        query = query.where(Run.session_id == session_id)
    if run_status:
        query = query.where(Run.status == run_status)
    return list(db.scalars(query.order_by(Run.started_at.desc()).limit(limit)))


@router.post("/runs", response_model=RunRead, status_code=status.HTTP_201_CREATED)
def create_run(payload: RunCreate, db: Session = Depends(get_db)) -> Run:
    data = payload.model_dump()
    if data.get("session_id"):
        chat_session = _require(db, ChatSession, data["session_id"], "Session")
        data["agent_id"] = DEFAULT_AGENT_ID
        data["workspace_id"] = data.get("workspace_id") or chat_session.workspace_id or DEFAULT_WORKSPACE_ID
    item = Run(**data)
    db.add(item)
    _commit(db)
    db.refresh(item)
    return item


@router.get("/runs/{run_id}", response_model=RunRead)
def get_run(run_id: str, db: Session = Depends(get_db)) -> Run:
    return _require(db, Run, run_id, "Run")


@router.patch("/runs/{run_id}", response_model=RunRead)
def update_run(run_id: str, payload: RunUpdate, db: Session = Depends(get_db)) -> Run:
    item = _require(db, Run, run_id, "Run")
    _apply(item, payload)
    _commit(db)
    db.refresh(item)
    return item


@router.get("/runs/{run_id}/events", response_model=list[RunEventRead])
def list_run_events(run_id: str, db: Session = Depends(get_db)) -> list[RunEventRead]:
    _require(db, Run, run_id, "Run")
    events = list(
        db.scalars(select(RunEvent).where(RunEvent.run_id == run_id).order_by(RunEvent.created_at.asc()))
    )
    return [public for event in events if (public := _public_run_event(event)) is not None]


@router.post(
    "/runs/{run_id}/events", response_model=RunEventRead, status_code=status.HTTP_201_CREATED
)
def create_run_event(
    run_id: str, payload: RunEventCreate, db: Session = Depends(get_db)
) -> RunEvent:
    _require(db, Run, run_id, "Run")
    item = RunEvent(run_id=run_id, **payload.model_dump())
    db.add(item)
    _commit(db)
    db.refresh(item)
    return item


# Approvals ------------------------------------------------------------------
