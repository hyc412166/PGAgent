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
from sqlalchemy import delete, func, or_, select, text, update
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
    ManagedWorktreeBind,
    ManagedWorktreeCreate,
    ManagedWorktreeRead,
)
from src.coding.git import (
    GitOperationError,
    bind_worktree_owner,
    create_managed_worktree,
    default_worktree_base,
    release_worktree_owner,
    require_managed_worktree,
    git_state,
    list_managed_worktrees,
    remove_managed_worktree,
)
from src.memory.service import recall_memories, refresh_memory_markdown_projection, store_memory
from src.skills.registry import replace_agent_capabilities, replace_session_skills
from src.tasks.state import latest_resumable_task, task_payload
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
def list_workspaces(db: Session = Depends(get_db)) -> list[dict[str, Any]]:
    return [_workspace_payload(item) for item in db.scalars(select(Workspace).order_by(Workspace.updated_at.desc()))]


def _workspace_payload(item: Workspace) -> dict[str, Any]:
    payload = WorkspaceRead.model_validate(item).model_dump()
    state = git_state(item.root_path)
    payload.update(git_info=state["git_info"], git_error=state["git_error"])
    return payload


@router.get("/workspaces/{workspace_id}/git")
def get_workspace_git(workspace_id: str, db: Session = Depends(get_db)) -> dict[str, Any]:
    workspace = _require(db, Workspace, workspace_id, "Workspace")
    return git_state(workspace.root_path)


@router.get("/workspaces/{workspace_id}/worktrees", response_model=list[ManagedWorktreeRead])
def get_workspace_worktrees(workspace_id: str, db: Session = Depends(get_db)) -> list[dict[str, Any]]:
    workspace = _require(db, Workspace, workspace_id, "Workspace")
    try:
        return [item.__dict__ for item in list_managed_worktrees(workspace.root_path)]
    except (GitOperationError, OSError, ValueError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/workspaces/{workspace_id}/worktrees", response_model=ManagedWorktreeRead, status_code=status.HTTP_201_CREATED)
def create_workspace_worktree(
    workspace_id: str, payload: ManagedWorktreeCreate, db: Session = Depends(get_db)
) -> dict[str, Any]:
    workspace = _require(db, Workspace, workspace_id, "Workspace")
    try:
        if payload.base and payload.use_default_branch:
            raise GitOperationError("Choose an explicit revision or the default branch")
        base = default_worktree_base(workspace.root_path) if payload.use_default_branch else payload.base
        return create_managed_worktree(workspace.root_path, base).__dict__
    except (GitOperationError, OSError, ValueError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.delete("/workspaces/{workspace_id}/worktrees", status_code=204)
def delete_workspace_worktree(
    workspace_id: str, path: str = Query(..., min_length=1),
    session_id: str | None = None, db: Session = Depends(get_db),
) -> Response:
    # 删除与会话/运行创建共享 SQLite 写事务，避免检查后新运行在该目录启动。
    if db.get_bind().dialect.name == "sqlite":
        db.execute(text("BEGIN IMMEDIATE"))
    workspace = _require(db, Workspace, workspace_id, "Workspace")
    try:
        checkout = require_managed_worktree(workspace.root_path, path)
        target = Path(checkout.root)
        current = _require(db, ChatSession, session_id, "Session") if session_id else None
        if current and current.cwd and Path(current.cwd).resolve().is_relative_to(target):
            raise GitOperationError("请先切换到其他工作目录，再删除当前 worktree")
        sessions = list(db.scalars(select(ChatSession)))
        using_ids = [item.id for item in sessions if item.cwd and Path(item.cwd).resolve().is_relative_to(target)]
        if using_ids and (
            db.scalar(select(Run.id).where(Run.session_id.in_(using_ids), Run.status.in_(_ACTIVE_SESSION_RUN_STATUSES | {"awaiting_approval"})).limit(1))
            or db.scalar(select(BackgroundJob.id).where(BackgroundJob.session_id.in_(using_ids), BackgroundJob.status.in_({"queued", "running"})).limit(1))
        ):
            raise GitOperationError("该 worktree 中仍有运行或后台任务")
        if checkout.owner_thread_id:
            owner = db.get(ChatSession, checkout.owner_thread_id)
            if owner and (owner.workspace_id != workspace.id or not owner.cwd or not Path(owner.cwd).resolve().is_relative_to(target)):
                raise GitOperationError("worktree owner 与会话目录不匹配")
        remove_managed_worktree(workspace.root_path, path)
        # 这里没有数据库修改；释放用于排除并发启动的事务，不制造提交失败点。
        db.rollback()
    except (GitOperationError, OSError, ValueError) as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/workspaces/{workspace_id}/worktrees/bind", response_model=SessionRead)
def bind_workspace_worktree(
    workspace_id: str, payload: ManagedWorktreeBind, db: Session = Depends(get_db)
) -> ChatSession:
    if db.get_bind().dialect.name == "sqlite":
        db.execute(text("BEGIN IMMEDIATE"))
    workspace = _require(db, Workspace, workspace_id, "Workspace")
    session = _require(db, ChatSession, payload.session_id, "Session")
    if session.workspace_id != workspace.id:
        raise HTTPException(status_code=409, detail="Session belongs to another workspace")
    if db.scalar(select(ChatMessage.id).where(ChatMessage.session_id == session.id).limit(1)) or db.scalar(select(Run.id).where(Run.session_id == session.id).limit(1)):
        raise HTTPException(status_code=409, detail="仅空会话可绑定新的工作目录")
    newly_bound = False
    try:
        checkout = require_managed_worktree(workspace.root_path, payload.path)
        if session.cwd and Path(session.cwd).resolve() != Path(checkout.cwd):
            for existing in list_managed_worktrees(workspace.root_path):
                if existing.owner_thread_id == session.id:
                    raise GitOperationError("该会话已经绑定了其他 worktree")
        bind_worktree_owner(checkout.root, session.id)
        newly_bound = checkout.owner_thread_id is None
        session.cwd = checkout.cwd
        _commit(db)
    except Exception as exc:
        owner_id = payload.session_id
        db.rollback()
        if newly_bound:
            release_worktree_owner(checkout.root, owner_id)
        if isinstance(exc, (GitOperationError, OSError, ValueError)):
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        raise
    db.refresh(session)
    return session


# 函数职责：创建 workspace 对应的数据或流程。
# 参数关系：payload 表示跨层传递的数据载荷；response 表示下游返回的响应；db 表示当前数据库会话。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
@router.post("/workspaces", response_model=WorkspaceRead, status_code=status.HTTP_201_CREATED)
def create_workspace(
    payload: WorkspaceCreate,
    response: Response,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
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
            return _workspace_payload(existing)

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
    return _workspace_payload(item)


# 函数职责：读取 workspace 对应的数据或流程。
# 参数关系：workspace_id 表示工作区标识；db 表示当前数据库会话。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
@router.get("/workspaces/{workspace_id}", response_model=WorkspaceRead)
def get_workspace(workspace_id: str, db: Session = Depends(get_db)) -> dict[str, Any]:
    return _workspace_payload(_require(db, Workspace, workspace_id, "Workspace"))


# 函数职责：更新 workspace 对应的数据或流程。
# 参数关系：workspace_id 表示工作区标识；payload 表示跨层传递的数据载荷；db 表示当前数据库会话。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
@router.patch("/workspaces/{workspace_id}", response_model=WorkspaceRead)
def update_workspace(
    workspace_id: str, payload: WorkspaceUpdate, db: Session = Depends(get_db)
) -> dict[str, Any]:
    # 变量说明：item 表示当前步骤使用的 item 值。
    item = _require(db, Workspace, workspace_id, "Workspace")
    _apply(item, payload)
    if payload.root_path is not None:
        # 变量说明：root_path 表示root_path 对应的文件系统位置。
        item.root_path = _normalized_workspace_root(payload.root_path)
    _commit(db)
    db.refresh(item)
    refresh_memory_markdown_projection()
    return _workspace_payload(item)


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
