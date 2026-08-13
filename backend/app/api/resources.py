"""Core PGAgent resource CRUD endpoints."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TypeVar
from urllib.parse import urlsplit, urlunsplit

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.database import (
    Agent,
    Approval,
    ChatMessage,
    DelegatedTask,
    Memory,
    ModelConnection,
    Run,
    RunEvent,
    Session as ChatSession,
    Workspace,
    DEFAULT_AGENT_ID,
    DEFAULT_WORKSPACE_ID,
    get_db,
    next_chat_message_sequence,
)
from app.schemas import (
    AgentCreate,
    AgentRead,
    AgentUpdate,
    ApprovalRead,
    ChatMessageCreate,
    ChatMessageRead,
    DashboardRead,
    DelegatedTaskRead,
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
    WorkspaceCreate,
    WorkspaceRead,
    WorkspaceUpdate,
)
from app.services.skill_service import replace_agent_capabilities, replace_session_skills


router = APIRouter(prefix="/api", tags=["resources"])
T = TypeVar("T")
_ACTIVE_SESSION_RUN_STATUSES = frozenset(
    {"received", "preparing_context", "planning", "acting", "observing", "running", "awaiting_approval"}
)
_SESSION_RUNTIME_SETTING_FIELDS = frozenset({"model_connection_id", "model_id", "thinking_level"})
_PUBLIC_RUN_EVENT_TYPES = frozenset({
    "approval_rejected",
    "approval_requested",
    "completed",
    "context_prepared",
    "context_compacted",
    "context_resumed",
    "delegated_child_started",
    "delegated_child_awaiting_approval",
    "delegated_child_completed",
    "delegated_child_stopped",
    "delegated_child_failed",
    "delegated_child_continuation_started",
    "failed",
    "integration_failed",
    "model_failed",
    "model_step_started",
    "run_completed",
    "run_interrupted",
    "run_stopped",
    "stopped",
    "tool_call",
    "tool_finished",
    "tool_result",
    "tool_started",
    "user_question_requested",
})
_TOOL_START_EVENT_TYPES = frozenset({"tool_call", "tool_started"})
_TOOL_FINISH_EVENT_TYPES = frozenset({"tool_finished", "tool_result"})
_TERMINAL_EVENT_TYPES = frozenset({
    "completed", "failed", "integration_failed", "model_failed", "run_completed", "run_stopped", "stopped"
})
_PUBLIC_EVENT_NUMBER_FIELDS = frozenset({
    "duration_ms",
    "elapsed_ms",
    "estimated_tokens",
    "omitted_messages",
    "output_chars",
    "question_chars",
    "remaining_call_count",
    "step",
    "thought_duration_ms",
})
_PUBLIC_EVENT_BOOLEAN_FIELDS = frozenset({
    "changed", "has_output", "ok", "pending_approval", "requires_next_message", "task_anchor_preserved", "terminal",
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


def _public_tool_argument_summary(value: Any) -> dict[str, str]:
    """Expose only a display target from a tool's already-scrubbed summary.

    Runtime events are normally written with ``safe_tool_argument_summary``.
    The HTTP read boundary still cannot trust every historical or manually
    created row, so it deliberately accepts no arbitrary argument keys.
    """

    if not isinstance(value, dict):
        return {}
    public: dict[str, str] = {}
    for key in ("path", "file_path", "target"):
        text = _public_event_text(value.get(key), limit=300)
        if text is not None:
            public[key] = text
    url = _public_event_url(value.get("url"))
    if url is not None:
        public["url"] = url
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

    if event_type.startswith("delegated_child_"):
        for key in ("task_id", "delegation_id", "child_run_id", "child_agent_id", "child_agent_name", "task_title", "status"):
            text = _public_event_text(source.get(key), limit=200)
            if text is not None:
                public[key] = text

    # Controlled status labels are useful for diagnostics, while free-form
    # error/reason/output text is intentionally kept out of this endpoint.
    if event_type in _TERMINAL_EVENT_TYPES | {"run_interrupted", "approval_rejected"}:
        for key in ("code", "error_type"):
            text = _public_event_text(source.get(key), limit=160)
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
    data = payload.model_dump()
    tool_ids = data.pop("tool_ids", [])
    skill_ids = data.pop("skill_ids", [])
    _require_enabled_model_connection(db, data.get("model_connection_id"))
    item = Agent(**data)
    db.add(item)
    try:
        db.flush()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail="Related resource does not exist") from exc
    replace_agent_capabilities(
        db,
        item,
        tool_ids=tool_ids,
        skill_ids=skill_ids,
        replace_tools=True,
        replace_skills=True,
    )
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
    if is_default and updates:
        raise HTTPException(
            status_code=409,
            detail="The PGAgent coordinator is managed by the system and cannot be modified",
        )
    tool_ids = updates.pop("tool_ids", None)
    skill_ids = updates.pop("skill_ids", None)
    if "model_connection_id" in updates:
        _require_enabled_model_connection(db, updates["model_connection_id"])
    for key, value in updates.items():
        setattr(item, key, value)
    if "tool_ids" in payload.model_fields_set:
        replace_agent_capabilities(db, item, tool_ids=tool_ids, replace_tools=True)
    if "skill_ids" in payload.model_fields_set:
        replace_agent_capabilities(db, item, skill_ids=skill_ids, replace_skills=True)
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
    skill_ids = data.pop("skill_ids", [])
    data["workspace_id"] = data.get("workspace_id") or DEFAULT_WORKSPACE_ID
    # Keep the field in the public schema for old clients, but all
    # conversations are coordinated by the built-in PGAgent master.
    data["agent_id"] = DEFAULT_AGENT_ID
    if db.get(Workspace, data["workspace_id"]) is None:
        raise HTTPException(status_code=409, detail="Selected workspace does not exist")
    if db.get(Agent, DEFAULT_AGENT_ID) is None:
        raise HTTPException(status_code=409, detail="PGAgent coordinator is unavailable")
    _require_enabled_model_connection(db, data.get("model_connection_id"))
    item = ChatSession(**data)
    db.add(item)
    db.flush()
    replace_session_skills(db, item, skill_ids)
    _commit(db)
    db.refresh(item)
    return item


@router.get("/sessions/{session_id}", response_model=SessionRead)
def get_session(session_id: str, db: Session = Depends(get_db)) -> ChatSession:
    return _require(db, ChatSession, session_id, "Session")


@router.get("/sessions/{session_id}/delegations", response_model=list[DelegatedTaskRead])
def list_session_delegations(
    session_id: str,
    db: Session = Depends(get_db),
) -> list[DelegatedTaskRead]:
    """Return child-Agent work, including records created before the dedicated table.

    Older PGAgent builds persisted a child run and its ``delegation_link``
    event but did not create a ``delegated_tasks`` row.  Those runs are still
    valid history, so reconstruct a read-only view instead of making the UI
    silently lose them.  Team-board rows remain deliberately excluded.
    """

    _require(db, ChatSession, session_id, "Session")
    persisted = list(db.scalars(
        select(DelegatedTask)
        .where(DelegatedTask.parent_session_id == session_id)
        .order_by(DelegatedTask.updated_at.desc(), DelegatedTask.created_at.desc())
    ))
    items = [DelegatedTaskRead.model_validate(item) for item in persisted]
    known_task_ids = {item.id for item in items}
    known_child_run_ids = {item.child_run_id for item in items if item.child_run_id}

    legacy_rows = db.execute(
        select(RunEvent, Run, Agent)
        .join(Run, Run.id == RunEvent.run_id)
        .outerjoin(Agent, Agent.id == Run.agent_id)
        .where(
            Run.session_id == session_id,
            RunEvent.event_type == "delegation_link",
        )
        .order_by(RunEvent.created_at.desc())
    ).all()
    for link_event, child_run, child_agent in legacy_rows:
        link = link_event.payload if isinstance(link_event.payload, dict) else {}
        task_id = str(link.get("delegation_id") or link.get("team_task_id") or link_event.id)
        if task_id in known_task_ids or child_run.id in known_child_run_ids:
            continue

        snapshot = db.scalar(
            select(RunEvent)
            .where(RunEvent.run_id == child_run.id, RunEvent.event_type == "runtime_snapshot")
            .order_by(RunEvent.created_at.desc())
        )
        snapshot_payload = snapshot.payload if snapshot and isinstance(snapshot.payload, dict) else {}
        messages = snapshot_payload.get("messages")
        description = ""
        if isinstance(messages, list):
            for message in messages:
                if not isinstance(message, dict) or message.get("role") != "user":
                    continue
                content = message.get("content")
                if isinstance(content, str) and content.strip():
                    description = content.strip()
                    break
        title = description.replace("\n", " ").strip()[:80] or "历史子 Agent 任务"
        output = snapshot_payload.get("output")
        result: dict[str, Any] = {
            "legacy": True,
            "child_run_id": child_run.id,
            "steps": child_run.current_step,
            "tool_calls": child_run.tool_calls,
            "agent": {
                "id": child_run.agent_id or "",
                "name": child_agent.name if child_agent is not None else "子 Agent",
            },
        }
        if isinstance(output, str) and output.strip():
            result["output"] = output
        if child_run.error_message:
            result["error"] = child_run.error_message

        items.append(DelegatedTaskRead(
            id=task_id,
            parent_run_id=str(link.get("parent_run_id") or child_run.id),
            parent_session_id=session_id,
            child_run_id=child_run.id,
            child_agent_id=child_run.agent_id,
            title=title,
            description=description,
            status=child_run.status,
            result=result,
            created_at=child_run.started_at,
            updated_at=child_run.finished_at or link_event.created_at,
        ))

    return sorted(items, key=lambda item: item.updated_at, reverse=True)


@router.patch("/sessions/{session_id}", response_model=SessionRead)
def update_session(
    session_id: str, payload: SessionUpdate, db: Session = Depends(get_db)
) -> ChatSession:
    item = _require(db, ChatSession, session_id, "Session")
    updates = payload.model_dump(exclude_unset=True)
    skill_ids_supplied = "skill_ids" in updates
    skill_ids = updates.pop("skill_ids", None)
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
    for key, value in updates.items():
        setattr(item, key, value)
    if skill_ids_supplied:
        replace_session_skills(db, item, skill_ids)
    # Accept stale clients that still send agent_id, without letting a child
    # Agent replace the session's fixed coordinator.
    item.agent_id = DEFAULT_AGENT_ID
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
    item = ChatMessage(
        session_id=session_id,
        sequence=next_chat_message_sequence(db, session_id),
        extra=payload.metadata,
        **data,
    )
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
