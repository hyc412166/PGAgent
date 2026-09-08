"""Workspace lifecycle endpoints."""
# 文件职责：负责HTTP 接口、数据契约与依赖装配中的 workspaces 子模块。
# 逻辑关系：上层通过 api/routes/workspaces.py 使用本模块；本模块把处理结果交给同领域服务、持久化层或 API 响应层。

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
# 变量说明：router 表示当前步骤使用的 router 值。
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

# 函数职责：列出 workspaces 对应的数据或流程。
# 参数关系：db 表示当前数据库会话。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
@router.get("/workspaces", response_model=list[WorkspaceRead])
def list_workspaces(db: Session = Depends(get_db)) -> list[Workspace]:
    return list(db.scalars(select(Workspace).order_by(Workspace.updated_at.desc())))


# 函数职责：创建 workspace 对应的数据或流程。
# 参数关系：payload 表示跨层传递的数据载荷；response 表示下游返回的响应；db 表示当前数据库会话。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
@router.post("/workspaces", response_model=WorkspaceRead, status_code=status.HTTP_201_CREATED)
def create_workspace(
    payload: WorkspaceCreate,
    response: Response,
    db: Session = Depends(get_db),
) -> Workspace:
    # 变量说明：root_path 表示root_path 对应的文件系统位置。
    root_path = _normalized_workspace_root(payload.root_path)
    # Existing databases may contain relative roots. Compare canonical paths
    # so choosing the same folder is idempotent across old and new clients.
    for existing in db.scalars(select(Workspace).order_by(Workspace.created_at.asc())):
        if _normalized_workspace_root(existing.root_path) == root_path:
            if existing.root_path != root_path:
                # 变量说明：root_path 表示root_path 对应的文件系统位置。
                existing.root_path = root_path
                _commit(db)
                db.refresh(existing)
            # 变量说明：status_code 表示当前步骤使用的 status_code 值。
            response.status_code = status.HTTP_200_OK
            return existing

    # 变量说明：name 表示当前对象名称。
    name = (payload.name or "").strip() or _workspace_name_from_root(root_path)
    # 变量说明：item 表示当前步骤使用的 item 值。
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


# 函数职责：读取 workspace 对应的数据或流程。
# 参数关系：workspace_id 表示工作区标识；db 表示当前数据库会话。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
@router.get("/workspaces/{workspace_id}", response_model=WorkspaceRead)
def get_workspace(workspace_id: str, db: Session = Depends(get_db)) -> Workspace:
    return _require(db, Workspace, workspace_id, "Workspace")


# 函数职责：更新 workspace 对应的数据或流程。
# 参数关系：workspace_id 表示工作区标识；payload 表示跨层传递的数据载荷；db 表示当前数据库会话。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
@router.patch("/workspaces/{workspace_id}", response_model=WorkspaceRead)
def update_workspace(
    workspace_id: str, payload: WorkspaceUpdate, db: Session = Depends(get_db)
) -> Workspace:
    # 变量说明：item 表示当前步骤使用的 item 值。
    item = _require(db, Workspace, workspace_id, "Workspace")
    _apply(item, payload)
    if payload.root_path is not None:
        # 变量说明：root_path 表示root_path 对应的文件系统位置。
        item.root_path = _normalized_workspace_root(payload.root_path)
    _commit(db)
    db.refresh(item)
    refresh_memory_markdown_projection()
    return item


# 函数职责：删除 workspace 对应的数据或流程。
# 参数关系：workspace_id 表示工作区标识；db 表示当前数据库会话。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
@router.delete("/workspaces/{workspace_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_workspace(workspace_id: str, db: Session = Depends(get_db)) -> Response:
    # 变量说明：item 表示当前步骤使用的 item 值。
    item = _require(db, Workspace, workspace_id, "Workspace")
    if item.id == DEFAULT_WORKSPACE_ID:
        raise HTTPException(status_code=409, detail="The default workspace cannot be deleted")
    # 变量说明：conversations 表示当前流程使用的 conversations 集合。
    conversations = list(
        db.scalars(
            select(ChatSession)
            .where(ChatSession.workspace_id == workspace_id)
            .order_by(ChatSession.created_at.asc(), ChatSession.id.asc())
        )
    )
    try:
        # 变量说明：effects 表示当前流程使用的 effects 集合。
        effects = stage_session_deletions(db, conversations)
    except SessionDeletionConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    db.execute(delete(Memory).where(Memory.scope == "workspace", Memory.scope_id == workspace_id))
    db.delete(item)
    _commit(db)
    finalize_session_deletions(effects)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# Agents ---------------------------------------------------------------------
