"""Agent profile and configuration endpoints."""

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
router = APIRouter(prefix="/api", tags=["agents"])

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
