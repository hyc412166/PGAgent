"""Session, message, task, and collaboration endpoints."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TypeVar
from urllib.parse import urlsplit, urlunsplit

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

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
from src.config import settings
from src.mcp.config import load_mcp_config_source, validate_mcp_server_names
from src.mcp.runtime import mcp_runtime_pool
from src.skills.registry import replace_agent_capabilities, replace_session_skills
from src.tasks.state import cancel_durable_task, latest_resumable_task, task_payload
from src.agents.collaboration import cleanup_session_worktrees
from src.runs.service import coordinator
from src.sessions.deletion import (
    SessionDeletionConflict,
    finalize_session_deletions,
    stage_session_deletions,
)
router = APIRouter(prefix="/api", tags=["sessions"])

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
    try:
        data["mcp_server_names"] = validate_mcp_server_names(
            load_mcp_config_source(settings.mcp_config_file),
            data.get("mcp_server_names", []),
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
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


@router.get("/sessions/{session_id}/tasks", response_model=list[DurableTaskRead])
def list_session_tasks(session_id: str, db: Session = Depends(get_db)) -> list[dict[str, Any]]:
    _require(db, ChatSession, session_id, "Session")
    tasks = list(db.scalars(
        select(DurableTask)
        .where(DurableTask.session_id == session_id)
        .order_by(DurableTask.updated_at.desc(), DurableTask.created_at.desc(), DurableTask.id.desc())
    ))
    return [task_payload(db, item) for item in tasks]


@router.get("/sessions/{session_id}/active-task", response_model=DurableTaskRead | None)
def get_session_active_task(session_id: str, db: Session = Depends(get_db)) -> dict[str, Any] | None:
    _require(db, ChatSession, session_id, "Session")
    task = latest_resumable_task(db, session_id)
    return task_payload(db, task) if task is not None else None


@router.post("/sessions/{session_id}/tasks/{task_id}/cancel", response_model=DurableTaskRead)
async def cancel_session_task(session_id: str, task_id: str, db: Session = Depends(get_db)) -> dict[str, Any]:
    _require(db, ChatSession, session_id, "Session")
    task = db.get(DurableTask, task_id)
    if task is None or task.session_id != session_id:
        raise HTTPException(status_code=404, detail="Task not found")
    if task.status not in {"planning", "running", "waiting", "paused", "needs_recovery", "blocked"}:
        raise HTTPException(status_code=404, detail="当前会话没有可取消的任务")

    linked_run_ids = list(db.scalars(
        select(Run.id).where(Run.task_id == task.id).order_by(Run.started_at.desc())
    ))
    for run_id in linked_run_ids:
        coordinator.stop(run_id, db=db, reason="user_interrupted")
    cancel_durable_task(db, task)
    db.commit()
    db.refresh(task)
    return task_payload(db, task)


@router.get("/sessions/{session_id}/background-jobs", response_model=list[BackgroundJobRead])
def list_session_background_jobs(
    session_id: str,
    db: Session = Depends(get_db),
) -> list[BackgroundJobRead]:
    _require(db, ChatSession, session_id, "Session")
    jobs = list(db.scalars(
        select(BackgroundJob)
        .where(BackgroundJob.session_id == session_id)
        .order_by(BackgroundJob.created_at.desc(), BackgroundJob.id.desc())
    ))
    return [BackgroundJobRead.model_validate(job) for job in jobs]


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
            plan_step_id=None,
            teammate_id=None,
            title=title,
            description=description,
            status=child_run.status,
            result=result,
            created_at=child_run.started_at,
            updated_at=child_run.finished_at or link_event.created_at,
        ))

    return sorted(items, key=lambda item: item.updated_at, reverse=True)


@router.get("/sessions/{session_id}/teammates", response_model=list[TeammateRead])
def list_session_teammates(session_id: str, db: Session = Depends(get_db)) -> list[TeammateWorker]:
    _require(db, ChatSession, session_id, "Session")
    team_ids = select(CollaborationTeam.id).where(CollaborationTeam.session_id == session_id)
    return list(db.scalars(
        select(TeammateWorker)
        .where(TeammateWorker.team_id.in_(team_ids))
        .order_by(TeammateWorker.created_at.asc(), TeammateWorker.id.asc())
    ))


@router.get(
    "/sessions/{session_id}/collaboration-messages",
    response_model=list[CollaborationMessageRead],
)
def list_session_collaboration_messages(
    session_id: str,
    limit: int = Query(default=200, ge=1, le=500),
    db: Session = Depends(get_db),
) -> list[CollaborationMessage]:
    _require(db, ChatSession, session_id, "Session")
    team_ids = select(CollaborationTeam.id).where(CollaborationTeam.session_id == session_id)
    return list(db.scalars(
        select(CollaborationMessage)
        .where(CollaborationMessage.team_id.in_(team_ids))
        .order_by(CollaborationMessage.created_at.desc(), CollaborationMessage.id.desc())
        .limit(limit)
    ))


@router.get(
    "/sessions/{session_id}/collaboration-events",
    response_model=list[CollaborationEventRead],
)
def list_session_collaboration_events(
    session_id: str,
    limit: int = Query(default=200, ge=1, le=500),
    db: Session = Depends(get_db),
) -> list[CollaborationEvent]:
    _require(db, ChatSession, session_id, "Session")
    task_ids = select(DurableTask.id).where(DurableTask.session_id == session_id)
    run_ids = select(Run.id).where(Run.session_id == session_id)
    return list(db.scalars(
        select(CollaborationEvent)
        .where(or_(
            CollaborationEvent.task_id.in_(task_ids),
            CollaborationEvent.run_id.in_(run_ids),
        ))
        .order_by(CollaborationEvent.created_at.desc(), CollaborationEvent.id.desc())
        .limit(limit)
    ))


@router.patch("/sessions/{session_id}", response_model=SessionRead)
async def update_session(
    session_id: str, payload: SessionUpdate, db: Session = Depends(get_db)
) -> ChatSession:
    item = _require(db, ChatSession, session_id, "Session")
    updates = payload.model_dump(exclude_unset=True)
    mcp_selection_changed = (
        "mcp_server_names" in updates
        and list(item.mcp_server_names or []) != updates["mcp_server_names"]
    )
    skill_ids_supplied = "skill_ids" in updates
    skill_ids = updates.pop("skill_ids", None)
    if "mcp_server_names" in updates:
        try:
            updates["mcp_server_names"] = validate_mcp_server_names(
                load_mcp_config_source(settings.mcp_config_file),
                updates["mcp_server_names"],
            )
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
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
    if mcp_selection_changed:
        await mcp_runtime_pool.close_session(session_id)
    refresh_memory_markdown_projection()
    return item


@router.delete("/sessions/{session_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_session(session_id: str, db: Session = Depends(get_db)) -> Response:
    item = _require(db, ChatSession, session_id, "Session")
    try:
        effects = stage_session_deletions(db, [item])
    except SessionDeletionConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    _commit(db)
    finalize_session_deletions(effects)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/sessions/{session_id}/messages", response_model=list[ChatMessageRead])
def list_messages(session_id: str, db: Session = Depends(get_db)) -> list[ChatMessage]:
    _require(db, ChatSession, session_id, "Session")
    return list(
        db.scalars(
            select(ChatMessage)
            .where(ChatMessage.session_id == session_id)
            .order_by(ChatMessage.sequence.asc(), ChatMessage.created_at.asc(), ChatMessage.id.asc())
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
