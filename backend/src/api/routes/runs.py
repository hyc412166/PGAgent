"""Run records and run-event endpoints."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import shutil
from typing import Any, TypeVar
from urllib.parse import urlsplit, urlunsplit

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy import and_, delete, func, or_, select, update
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


def _run_read(
    run: Run,
    *,
    session_titles: dict[str, str] | None = None,
    agent_names: dict[str, str] | None = None,
    db: Session | None = None,
) -> RunRead:
    if session_titles is None:
        session = db.get(ChatSession, run.session_id) if db is not None and run.session_id else None
        session_title = session.title if session is not None else None
    else:
        session_title = session_titles.get(run.session_id or "")
    if agent_names is None:
        agent = db.get(Agent, run.agent_id) if db is not None and run.agent_id else None
        agent_name = agent.name if agent is not None else None
    else:
        agent_name = agent_names.get(run.agent_id or "")
    return RunRead.model_validate(run).model_copy(update={
        "session_title": session_title,
        "agent_name": agent_name,
    })


def _message_excerpt(content: str, limit: int = 180) -> str:
    normalized = " ".join(content.replace("\x00", "").split())
    if len(normalized) <= limit:
        return normalized
    return normalized[: limit - 1].rstrip() + "…"

@router.get("/runs", response_model=list[RunRead])
def list_runs(
    session_id: str | None = None,
    run_status: str | None = Query(default=None, alias="status"),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    before_started_at: datetime | None = None,
    before_id: str | None = None,
    db: Session = Depends(get_db),
) -> list[RunRead]:
    query = select(Run)
    if session_id:
        query = query.where(Run.session_id == session_id)
    if run_status:
        query = query.where(Run.status == run_status)
    if (before_started_at is None) != (before_id is None):
        raise HTTPException(status_code=422, detail="before_started_at and before_id must be provided together")
    if before_started_at is not None and before_id is not None:
        query = query.where(or_(
            Run.started_at < before_started_at,
            and_(Run.started_at == before_started_at, Run.id < before_id),
        ))
    runs = list(db.scalars(
        query.order_by(Run.started_at.desc(), Run.id.desc()).offset(offset).limit(limit)
    ))
    session_ids = {run.session_id for run in runs if run.session_id}
    agent_ids = {run.agent_id for run in runs if run.agent_id}
    session_titles = dict(db.execute(
        select(ChatSession.id, ChatSession.title).where(ChatSession.id.in_(session_ids))
    ).all()) if session_ids else {}
    agent_names = dict(db.execute(
        select(Agent.id, Agent.name).where(Agent.id.in_(agent_ids))
    ).all()) if agent_ids else {}
    return [
        _run_read(run, session_titles=session_titles, agent_names=agent_names)
        for run in runs
    ]


@router.post("/runs", response_model=RunRead, status_code=status.HTTP_201_CREATED)
def create_run(payload: RunCreate, db: Session = Depends(get_db)) -> RunRead:
    data = payload.model_dump()
    if data.get("session_id"):
        chat_session = _require(db, ChatSession, data["session_id"], "Session")
        data["agent_id"] = DEFAULT_AGENT_ID
        data["workspace_id"] = data.get("workspace_id") or chat_session.workspace_id or DEFAULT_WORKSPACE_ID
    item = Run(**data)
    db.add(item)
    _commit(db)
    db.refresh(item)
    return _run_read(item, db=db)


@router.get("/runs/{run_id}", response_model=RunRead)
def get_run(run_id: str, db: Session = Depends(get_db)) -> RunRead:
    return _run_read(_require(db, Run, run_id, "Run"), db=db)


@router.patch("/runs/{run_id}", response_model=RunRead)
def update_run(run_id: str, payload: RunUpdate, db: Session = Depends(get_db)) -> RunRead:
    item = _require(db, Run, run_id, "Run")
    _apply(item, payload)
    _commit(db)
    db.refresh(item)
    return _run_read(item, db=db)


@router.get("/runs/{run_id}/events", response_model=list[RunEventRead])
def list_run_events(run_id: str, db: Session = Depends(get_db)) -> list[RunEventRead]:
    run = _require(db, Run, run_id, "Run")
    user_message = db.scalar(
        select(ChatMessage.content)
        .where(ChatMessage.turn_id == run.turn_id, ChatMessage.role == "user")
        .order_by(ChatMessage.sequence.asc(), ChatMessage.created_at.asc())
        .limit(1)
    ) if run.turn_id else None
    message_excerpt = _message_excerpt(user_message) if user_message else None
    events = list(
        db.scalars(select(RunEvent).where(RunEvent.run_id == run_id).order_by(RunEvent.created_at.asc()))
    )
    public_events: list[RunEventRead] = []
    for event in events:
        public = _public_run_event(event)
        if public is None:
            continue
        if message_excerpt and public.event_type in {"context_prepared", "context_resumed"}:
            public = public.model_copy(update={
                "payload": {**public.payload, "message_excerpt": message_excerpt},
            })
        public_events.append(public)
    return public_events


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
