"""Agent profile and configuration endpoints."""
# 文件职责：负责HTTP 接口、数据契约与依赖装配中的 agents 子模块。
# 逻辑关系：上层通过 api/routes/agents.py 使用本模块；本模块把处理结果交给同领域服务、持久化层或 API 响应层。

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
# 变量说明：router 表示当前步骤使用的 router 值。
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

# 函数职责：列出 agents 对应的数据或流程。
# 参数关系：db 表示当前数据库会话。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
@router.get("/agents", response_model=list[AgentRead])
def list_agents(db: Session = Depends(get_db)) -> list[Agent]:
    return list(db.scalars(select(Agent).order_by(Agent.updated_at.desc())))


# 函数职责：创建 agent 对应的数据或流程。
# 参数关系：payload 表示跨层传递的数据载荷；db 表示当前数据库会话。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
@router.post("/agents", response_model=AgentRead, status_code=status.HTTP_201_CREATED)
def create_agent(payload: AgentCreate, db: Session = Depends(get_db)) -> Agent:
    # 变量说明：data 表示当前处理的数据。
    data = payload.model_dump()
    # 变量说明：tool_ids 表示tool 对象标识集合。
    tool_ids = data.pop("tool_ids", [])
    # 变量说明：skill_ids 表示skill 对象标识集合。
    skill_ids = data.pop("skill_ids", [])
    _require_enabled_model_connection(db, data.get("model_connection_id"))
    # 变量说明：item 表示当前步骤使用的 item 值。
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


# 函数职责：读取 agent 对应的数据或流程。
# 参数关系：agent_id 表示智能体标识；db 表示当前数据库会话。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
@router.get("/agents/{agent_id}", response_model=AgentRead)
def get_agent(agent_id: str, db: Session = Depends(get_db)) -> Agent:
    return _require(db, Agent, agent_id, "Agent")


# 函数职责：更新 agent 对应的数据或流程。
# 参数关系：agent_id 表示智能体标识；payload 表示跨层传递的数据载荷；db 表示当前数据库会话。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
@router.patch("/agents/{agent_id}", response_model=AgentRead)
def update_agent(agent_id: str, payload: AgentUpdate, db: Session = Depends(get_db)) -> Agent:
    # 变量说明：item 表示当前步骤使用的 item 值。
    item = _require(db, Agent, agent_id, "Agent")
    # 变量说明：updates 表示当前流程使用的 updates 集合。
    updates = payload.model_dump(exclude_unset=True)
    # 变量说明：is_default 表示表示是否满足 default 条件的布尔标记。
    is_default = item.id == DEFAULT_AGENT_ID or item.is_default
    if is_default and updates:
        raise HTTPException(
            status_code=409,
            detail="The PGAgent coordinator is managed by the system and cannot be modified",
        )
    # 变量说明：tool_ids 表示tool 对象标识集合。
    tool_ids = updates.pop("tool_ids", None)
    # 变量说明：skill_ids 表示skill 对象标识集合。
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


# 函数职责：删除 agent 对应的数据或流程。
# 参数关系：agent_id 表示智能体标识；db 表示当前数据库会话。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
@router.delete("/agents/{agent_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_agent(agent_id: str, db: Session = Depends(get_db)) -> Response:
    # 变量说明：item 表示当前步骤使用的 item 值。
    item = _require(db, Agent, agent_id, "Agent")
    if item.id == DEFAULT_AGENT_ID or item.is_default:
        raise HTTPException(status_code=409, detail="The default agent cannot be deleted")
    db.delete(item)
    _commit(db)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# Sessions and messages -------------------------------------------------------
