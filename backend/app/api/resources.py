"""Core PGAgent resource CRUD endpoints."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, TypeVar
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.database import (
    Agent,
    AgentMessage,
    Approval,
    ChatMessage,
    Memory,
    ModelConnection,
    Run,
    RunEvent,
    Session as ChatSession,
    TeamTask,
    Workspace,
    DEFAULT_AGENT_ID,
    DEFAULT_WORKSPACE_ID,
    get_db,
)
from app.schemas import (
    AgentCreate,
    AgentMessageCreate,
    AgentMessageRead,
    AgentRead,
    AgentUpdate,
    ApprovalRead,
    ChatMessageCreate,
    ChatMessageRead,
    DashboardRead,
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
    TeamTaskClaim,
    TeamTaskCreate,
    TeamTaskRead,
    TeamTaskUpdate,
    WorkspaceCreate,
    WorkspaceRead,
    WorkspaceUpdate,
)


router = APIRouter(prefix="/api", tags=["resources"])
T = TypeVar("T")
_ACTIVE_SESSION_RUN_STATUSES = frozenset(
    {"received", "preparing_context", "planning", "acting", "observing", "running", "awaiting_approval"}
)
_SESSION_RUNTIME_SETTING_FIELDS = frozenset({"model_connection_id", "model_id", "thinking_level"})


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


@router.get("/dashboard", response_model=DashboardRead)
def dashboard(db: Session = Depends(get_db)) -> DashboardRead:
    count = lambda model: db.scalar(select(func.count()).select_from(model)) or 0
    active_runs = db.scalar(
        select(func.count()).select_from(Run).where(
            Run.status.not_in(("completed", "failed", "stopped"))
        )
    ) or 0
    pending_approvals = db.scalar(
        select(func.count()).select_from(Approval).where(Approval.status == "pending")
    ) or 0
    return DashboardRead(
        workspaces=count(Workspace),
        agents=count(Agent),
        sessions=count(ChatSession),
        active_runs=active_runs,
        pending_approvals=pending_approvals,
        model_connections=count(ModelConnection),
    )


# Workspaces -----------------------------------------------------------------


@router.get("/workspaces", response_model=list[WorkspaceRead])
def list_workspaces(db: Session = Depends(get_db)) -> list[Workspace]:
    return list(db.scalars(select(Workspace).order_by(Workspace.updated_at.desc())))


@router.post("/workspaces", response_model=WorkspaceRead, status_code=status.HTTP_201_CREATED)
def create_workspace(payload: WorkspaceCreate, db: Session = Depends(get_db)) -> Workspace:
    item = Workspace(**payload.model_dump())
    item.root_path = str(Path(item.root_path).expanduser().resolve())
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
        item.root_path = str(Path(payload.root_path).expanduser().resolve())
    _commit(db)
    db.refresh(item)
    return item


@router.delete("/workspaces/{workspace_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_workspace(workspace_id: str, db: Session = Depends(get_db)) -> Response:
    item = _require(db, Workspace, workspace_id, "Workspace")
    if item.id == DEFAULT_WORKSPACE_ID:
        raise HTTPException(status_code=409, detail="The default workspace cannot be deleted")
    db.delete(item)
    _commit(db)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# Agents ---------------------------------------------------------------------


@router.get("/agents", response_model=list[AgentRead])
def list_agents(db: Session = Depends(get_db)) -> list[Agent]:
    return list(db.scalars(select(Agent).order_by(Agent.updated_at.desc())))


@router.post("/agents", response_model=AgentRead, status_code=status.HTTP_201_CREATED)
def create_agent(payload: AgentCreate, db: Session = Depends(get_db)) -> Agent:
    item = Agent(**payload.model_dump())
    db.add(item)
    _commit(db)
    db.refresh(item)
    return item


@router.get("/agents/{agent_id}", response_model=AgentRead)
def get_agent(agent_id: str, db: Session = Depends(get_db)) -> Agent:
    return _require(db, Agent, agent_id, "Agent")


@router.patch("/agents/{agent_id}", response_model=AgentRead)
def update_agent(agent_id: str, payload: AgentUpdate, db: Session = Depends(get_db)) -> Agent:
    item = _require(db, Agent, agent_id, "Agent")
    updates = payload.model_dump(exclude_unset=True)
    is_default = item.id == DEFAULT_AGENT_ID or item.is_default
    if is_default and updates.get("system_prompt", "") != "":
        raise HTTPException(status_code=409, detail="The default agent must keep an empty system prompt")
    _apply(item, payload)
    if is_default:
        item.system_prompt = ""
    _commit(db)
    db.refresh(item)
    return item


@router.delete("/agents/{agent_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_agent(agent_id: str, db: Session = Depends(get_db)) -> Response:
    item = _require(db, Agent, agent_id, "Agent")
    if item.id == DEFAULT_AGENT_ID or item.is_default:
        raise HTTPException(status_code=409, detail="The default agent cannot be deleted")
    db.delete(item)
    _commit(db)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# Sessions and messages -------------------------------------------------------


@router.get("/sessions", response_model=list[SessionRead])
def list_sessions(
    workspace_id: str | None = None,
    agent_id: str | None = None,
    db: Session = Depends(get_db),
) -> list[ChatSession]:
    query = select(ChatSession)
    if workspace_id:
        query = query.where(ChatSession.workspace_id == workspace_id)
    if agent_id:
        query = query.where(ChatSession.agent_id == agent_id)
    return list(db.scalars(query.order_by(ChatSession.updated_at.desc())))


@router.post("/sessions", response_model=SessionRead, status_code=status.HTTP_201_CREATED)
def create_session(payload: SessionCreate, db: Session = Depends(get_db)) -> ChatSession:
    data = payload.model_dump()
    data["workspace_id"] = data.get("workspace_id") or DEFAULT_WORKSPACE_ID
    data["agent_id"] = data.get("agent_id") or DEFAULT_AGENT_ID
    if db.get(Workspace, data["workspace_id"]) is None:
        raise HTTPException(status_code=409, detail="Selected workspace does not exist")
    if db.get(Agent, data["agent_id"]) is None:
        raise HTTPException(status_code=409, detail="Selected agent does not exist")
    _require_enabled_model_connection(db, data.get("model_connection_id"))
    item = ChatSession(**data)
    db.add(item)
    _commit(db)
    db.refresh(item)
    return item


@router.get("/sessions/{session_id}", response_model=SessionRead)
def get_session(session_id: str, db: Session = Depends(get_db)) -> ChatSession:
    return _require(db, ChatSession, session_id, "Session")


@router.patch("/sessions/{session_id}", response_model=SessionRead)
def update_session(
    session_id: str, payload: SessionUpdate, db: Session = Depends(get_db)
) -> ChatSession:
    item = _require(db, ChatSession, session_id, "Session")
    updates = payload.model_dump(exclude_unset=True)
    if "model_connection_id" in updates:
        _require_enabled_model_connection(db, updates["model_connection_id"])
    runtime_settings_changed = any(
        field in updates and getattr(item, field) != updates[field]
        for field in _SESSION_RUNTIME_SETTING_FIELDS
    )
    if runtime_settings_changed:
        active_run_id = db.scalar(
            select(Run.id)
            .where(Run.session_id == session_id, Run.status.in_(_ACTIVE_SESSION_RUN_STATUSES))
            .limit(1)
        )
        if active_run_id is not None:
            raise HTTPException(
                status_code=409,
                detail="Model and thinking settings cannot change while the session has an active or awaiting run",
            )
    _apply(item, payload)
    _commit(db)
    db.refresh(item)
    return item


@router.delete("/sessions/{session_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_session(session_id: str, db: Session = Depends(get_db)) -> Response:
    db.delete(_require(db, ChatSession, session_id, "Session"))
    _commit(db)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/sessions/{session_id}/messages", response_model=list[ChatMessageRead])
def list_messages(session_id: str, db: Session = Depends(get_db)) -> list[ChatMessage]:
    _require(db, ChatSession, session_id, "Session")
    return list(
        db.scalars(
            select(ChatMessage)
            .where(ChatMessage.session_id == session_id)
            .order_by(ChatMessage.created_at.asc())
        )
    )


@router.post(
    "/sessions/{session_id}/messages",
    response_model=ChatMessageRead,
    status_code=status.HTTP_201_CREATED,
)
def create_message(
    session_id: str, payload: ChatMessageCreate, db: Session = Depends(get_db)
) -> ChatMessage:
    session = _require(db, ChatSession, session_id, "Session")
    data = payload.model_dump(exclude={"metadata"})
    item = ChatMessage(session_id=session_id, extra=payload.metadata, **data)
    session.updated_at = datetime.now(timezone.utc)
    db.add(item)
    _commit(db)
    db.refresh(item)
    return item


# Runs and events -------------------------------------------------------------


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
    item = Run(**payload.model_dump())
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
def list_run_events(run_id: str, db: Session = Depends(get_db)) -> list[RunEvent]:
    _require(db, Run, run_id, "Run")
    return list(
        db.scalars(select(RunEvent).where(RunEvent.run_id == run_id).order_by(RunEvent.created_at.asc()))
    )


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


@router.get("/approvals", response_model=list[ApprovalRead])
def list_approvals(
    approval_status: str | None = Query(default=None, alias="status"),
    run_id: str | None = None,
    db: Session = Depends(get_db),
) -> list[Approval]:
    query = select(Approval)
    if approval_status:
        query = query.where(Approval.status == approval_status)
    if run_id:
        query = query.where(Approval.run_id == run_id)
    return list(db.scalars(query.order_by(Approval.created_at.desc())))


# Memories -------------------------------------------------------------------


@router.get("/memories", response_model=list[MemoryRead])
def list_memories(
    scope: str | None = None,
    scope_id: str | None = None,
    pinned: bool | None = None,
    db: Session = Depends(get_db),
) -> list[Memory]:
    query = select(Memory)
    if scope:
        query = query.where(Memory.scope == scope)
    if scope_id:
        query = query.where(Memory.scope_id == scope_id)
    if pinned is not None:
        query = query.where(Memory.pinned == pinned)
    return list(db.scalars(query.order_by(Memory.pinned.desc(), Memory.updated_at.desc())))


@router.post("/memories", response_model=MemoryRead, status_code=status.HTTP_201_CREATED)
def create_memory(payload: MemoryCreate, db: Session = Depends(get_db)) -> Memory:
    data = payload.model_dump(exclude={"metadata"})
    item = Memory(extra=payload.metadata, **data)
    db.add(item)
    _commit(db)
    db.refresh(item)
    return item


@router.patch("/memories/{memory_id}", response_model=MemoryRead)
def update_memory(memory_id: str, payload: MemoryUpdate, db: Session = Depends(get_db)) -> Memory:
    item = _require(db, Memory, memory_id, "Memory")
    _apply(item, payload, field_map={"metadata": "extra"})
    _commit(db)
    db.refresh(item)
    return item


@router.delete("/memories/{memory_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_memory(memory_id: str, db: Session = Depends(get_db)) -> Response:
    db.delete(_require(db, Memory, memory_id, "Memory"))
    _commit(db)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.delete("/memories", status_code=status.HTTP_204_NO_CONTENT)
def clear_memories(
    scope: str = Query(...), scope_id: str | None = None, db: Session = Depends(get_db)
) -> Response:
    query = delete(Memory).where(Memory.scope == scope)
    if scope_id is None:
        query = query.where(Memory.scope_id.is_(None))
    else:
        query = query.where(Memory.scope_id == scope_id)
    db.execute(query)
    _commit(db)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# Multi-agent task board and structured communication ------------------------


@router.get("/teams/tasks", response_model=list[TeamTaskRead])
def list_team_tasks(
    team_id: str | None = None,
    task_status: str | None = Query(default=None, alias="status"),
    assignee_agent_id: str | None = None,
    db: Session = Depends(get_db),
) -> list[TeamTask]:
    query = select(TeamTask)
    if team_id:
        query = query.where(TeamTask.team_id == team_id)
    if task_status:
        query = query.where(TeamTask.status == task_status)
    if assignee_agent_id:
        query = query.where(TeamTask.assignee_agent_id == assignee_agent_id)
    return list(db.scalars(query.order_by(TeamTask.updated_at.desc())))


@router.post("/teams/tasks", response_model=TeamTaskRead, status_code=status.HTTP_201_CREATED)
def create_team_task(payload: TeamTaskCreate, db: Session = Depends(get_db)) -> TeamTask:
    if payload.idempotency_key:
        existing = db.scalar(
            select(TeamTask).where(TeamTask.idempotency_key == payload.idempotency_key)
        )
        if existing is not None:
            return existing
    item = TeamTask(**payload.model_dump())
    db.add(item)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        if payload.idempotency_key:
            existing = db.scalar(
                select(TeamTask).where(TeamTask.idempotency_key == payload.idempotency_key)
            )
            if existing is not None:
                return existing
        raise HTTPException(status_code=409, detail="Team task could not be created") from exc
    db.refresh(item)
    return item


@router.get("/teams/tasks/{task_id}", response_model=TeamTaskRead)
def get_team_task(task_id: str, db: Session = Depends(get_db)) -> TeamTask:
    return _require(db, TeamTask, task_id, "Team task")


@router.patch("/teams/tasks/{task_id}", response_model=TeamTaskRead)
def update_team_task(
    task_id: str, payload: TeamTaskUpdate, db: Session = Depends(get_db)
) -> TeamTask:
    if payload.expected_version is not None:
        values = payload.model_dump(exclude_unset=True, exclude={"expected_version"})
        values.update(version=payload.expected_version + 1, updated_at=datetime.now(timezone.utc))
        result = db.execute(
            update(TeamTask)
            .where(TeamTask.id == task_id, TeamTask.version == payload.expected_version)
            .values(**values)
        )
        if result.rowcount != 1:
            db.rollback()
            if db.get(TeamTask, task_id) is None:
                raise HTTPException(status_code=404, detail="Team task not found")
            raise HTTPException(status_code=409, detail="Task version changed; reload before updating")
        db.commit()
        return _require(db, TeamTask, task_id, "Team task")

    item = _require(db, TeamTask, task_id, "Team task")
    _apply(item, payload)
    item.version += 1
    _commit(db)
    db.refresh(item)
    return item


@router.post("/teams/tasks/{task_id}/claim", response_model=TeamTaskRead)
def claim_team_task(
    task_id: str, payload: TeamTaskClaim, db: Session = Depends(get_db)
) -> TeamTask:
    now = datetime.now(timezone.utc)
    expires = now + timedelta(seconds=payload.lease_seconds)
    statement = (
        update(TeamTask)
        .where(
            TeamTask.id == task_id,
            TeamTask.version == payload.expected_version,
            TeamTask.status.in_(("todo", "blocked", "in_progress")),
            or_(
                TeamTask.lease_owner.is_(None),
                TeamTask.lease_owner == payload.lease_owner,
                TeamTask.lease_expires_at < now,
            ),
        )
        .values(
            assignee_agent_id=payload.agent_id,
            lease_owner=payload.lease_owner,
            lease_expires_at=expires,
            status="in_progress",
            version=payload.expected_version + 1,
            updated_at=now,
        )
    )
    result = db.execute(statement)
    if result.rowcount != 1:
        db.rollback()
        if db.get(TeamTask, task_id) is None:
            raise HTTPException(status_code=404, detail="Team task not found")
        raise HTTPException(status_code=409, detail="Task is already claimed or its version changed")
    db.commit()
    return _require(db, TeamTask, task_id, "Team task")


@router.delete("/teams/tasks/{task_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_team_task(task_id: str, db: Session = Depends(get_db)) -> Response:
    db.delete(_require(db, TeamTask, task_id, "Team task"))
    _commit(db)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/teams/messages", response_model=list[AgentMessageRead])
def list_agent_messages(
    team_id: str | None = None,
    task_id: str | None = None,
    recipient_agent_id: str | None = None,
    message_status: str | None = Query(default=None, alias="status"),
    include_expired: bool = False,
    db: Session = Depends(get_db),
) -> list[AgentMessage]:
    query = select(AgentMessage)
    if team_id:
        query = query.where(AgentMessage.team_id == team_id)
    if task_id:
        query = query.where(AgentMessage.task_id == task_id)
    if recipient_agent_id:
        query = query.where(AgentMessage.recipient_agent_id == recipient_agent_id)
    if message_status:
        query = query.where(AgentMessage.status == message_status)
    if not include_expired:
        now = datetime.now(timezone.utc)
        query = query.where(or_(AgentMessage.expires_at.is_(None), AgentMessage.expires_at > now))
    return list(db.scalars(query.order_by(AgentMessage.created_at.asc())))


@router.post("/teams/messages", response_model=AgentMessageRead, status_code=status.HTTP_201_CREATED)
def create_agent_message(
    payload: AgentMessageCreate, db: Session = Depends(get_db)
) -> AgentMessage:
    idempotency_key = payload.idempotency_key or f"message:{uuid4()}"
    existing = db.scalar(
        select(AgentMessage).where(AgentMessage.idempotency_key == idempotency_key)
    )
    if existing is not None:
        return existing
    data = payload.model_dump(exclude={"idempotency_key"})
    item = AgentMessage(idempotency_key=idempotency_key, **data)
    db.add(item)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        existing = db.scalar(
            select(AgentMessage).where(AgentMessage.idempotency_key == idempotency_key)
        )
        if existing is not None:
            return existing
        raise HTTPException(status_code=409, detail="Related task or agent does not exist") from exc
    db.refresh(item)
    return item


@router.post("/teams/messages/{message_id}/deliver", response_model=AgentMessageRead)
def deliver_agent_message(message_id: str, db: Session = Depends(get_db)) -> AgentMessage:
    item = _require(db, AgentMessage, message_id, "Agent message")
    now = datetime.now(timezone.utc)
    expires_at = item.expires_at
    if expires_at is not None:
        # SQLite can return a naive value even for timezone=True columns.
        comparable_expiry = expires_at.replace(tzinfo=timezone.utc) if expires_at.tzinfo is None else expires_at
        if comparable_expiry <= now:
            item.status = "expired"
            _commit(db)
            raise HTTPException(status_code=410, detail="Agent message has expired")
    if item.status == "pending":
        item.status = "delivered"
        _commit(db)
        db.refresh(item)
    return item


@router.post("/teams/messages/{message_id}/ack", response_model=AgentMessageRead)
def acknowledge_agent_message(message_id: str, db: Session = Depends(get_db)) -> AgentMessage:
    item = _require(db, AgentMessage, message_id, "Agent message")
    item.status = "acknowledged"
    item.acknowledged_at = datetime.now(timezone.utc)
    _commit(db)
    db.refresh(item)
    return item
