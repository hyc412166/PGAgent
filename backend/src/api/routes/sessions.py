"""Session, message, task, and collaboration endpoints."""
# 文件职责：负责HTTP 接口、数据契约与依赖装配中的 sessions 子模块。
# 逻辑关系：上层通过 api/routes/sessions.py 使用本模块；本模块把处理结果交给同领域服务、持久化层或 API 响应层。

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
    PermissionSettingsRead,
    PermissionSettingsUpdate,
    TeammateRead,
    WorkspaceCreate,
    WorkspaceRead,
    WorkspaceUpdate,
)
from src.memory.service import recall_memories, refresh_memory_markdown_projection, store_memory
from src.config import settings
from src.mcp.config import load_mcp_config_source, validate_mcp_server_names
from src.mcp.runtime import mcp_runtime_pool
from src.permissions.preferences import get_permission_mode, set_permission_mode
from src.skills.registry import replace_agent_capabilities, replace_session_skills
from src.tasks.state import cancel_durable_task, latest_resumable_task, task_payload
from src.agents.collaboration import cleanup_session_worktrees
from src.runs.service import coordinator
from src.sessions.deletion import (
    SessionDeletionConflict,
    finalize_session_deletions,
    stage_session_deletions,
)
# 变量说明：router 表示当前步骤使用的 router 值。
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


# 函数职责：读取用户最后一次选择的权限模式，作为后续新会话的默认值。
@router.get("/permissions/settings", response_model=PermissionSettingsRead)
def read_permission_settings(db: Session = Depends(get_db)) -> PermissionSettingsRead:
    return PermissionSettingsRead(permission_mode=get_permission_mode(db))


# 函数职责：保存用户最后一次选择的权限模式。
@router.put("/permissions/settings", response_model=PermissionSettingsRead)
def update_permission_settings(
    payload: PermissionSettingsUpdate,
    db: Session = Depends(get_db),
) -> PermissionSettingsRead:
    item = set_permission_mode(db, payload.permission_mode)
    _commit(db)
    return PermissionSettingsRead(permission_mode=item.permission_mode)

# 函数职责：列出 sessions 对应的数据或流程。
# 参数关系：workspace_id 表示工作区标识；agent_id 表示智能体标识；db 表示当前数据库会话。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
@router.get("/sessions", response_model=list[SessionRead])
def list_sessions(
    workspace_id: str | None = None,
    agent_id: str | None = None,
    db: Session = Depends(get_db),
) -> list[ChatSession]:
    # 变量说明：query 表示当前步骤使用的 query 值。
    query = select(ChatSession)
    if workspace_id:
        # 变量说明：query 表示当前步骤使用的 query 值。
        query = query.where(ChatSession.workspace_id == workspace_id)
    if agent_id:
        # 变量说明：query 表示当前步骤使用的 query 值。
        query = query.where(ChatSession.agent_id == agent_id)
    return list(db.scalars(query.order_by(ChatSession.updated_at.desc())))


# 函数职责：创建 session 对应的数据或流程。
# 参数关系：payload 表示跨层传递的数据载荷；db 表示当前数据库会话。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
@router.post("/sessions", response_model=SessionRead, status_code=status.HTTP_201_CREATED)
def create_session(payload: SessionCreate, db: Session = Depends(get_db)) -> ChatSession:
    # 变量说明：data 表示当前处理的数据。
    data = payload.model_dump()
    requested_permission_mode = data.pop("permission_mode", None)
    data["permission_mode"] = requested_permission_mode or get_permission_mode(db)
    # 变量说明：skill_ids 表示skill 对象标识集合。
    skill_ids = data.pop("skill_ids", [])
    try:
        data["mcp_server_names"] = validate_mcp_server_names(
            load_mcp_config_source(settings.mcp_config_file),
            data.get("mcp_server_names", []),
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    # 变量说明：data 的索引项 表示该语句创建或更新的目标数据。
    data["workspace_id"] = data.get("workspace_id") or DEFAULT_WORKSPACE_ID
    # Keep the field in the public schema for old clients, but all
    # conversations are coordinated by the built-in PGAgent master.
    # 变量说明：data 的索引项 表示该语句创建或更新的目标数据。
    data["agent_id"] = DEFAULT_AGENT_ID
    if db.get(Workspace, data["workspace_id"]) is None:
        raise HTTPException(status_code=409, detail="Selected workspace does not exist")
    if db.get(Agent, DEFAULT_AGENT_ID) is None:
        raise HTTPException(status_code=409, detail="PGAgent coordinator is unavailable")
    _require_enabled_model_connection(db, data.get("model_connection_id"))
    # 变量说明：item 表示当前步骤使用的 item 值。
    item = ChatSession(**data)
    db.add(item)
    if requested_permission_mode is not None:
        set_permission_mode(db, requested_permission_mode)
    db.flush()
    replace_session_skills(db, item, skill_ids)
    _commit(db)
    db.refresh(item)
    return item


# 函数职责：读取 session 对应的数据或流程。
# 参数关系：session_id 表示所属会话标识；db 表示当前数据库会话。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
@router.get("/sessions/{session_id}", response_model=SessionRead)
def get_session(session_id: str, db: Session = Depends(get_db)) -> ChatSession:
    return _require(db, ChatSession, session_id, "Session")


# 函数职责：列出 session_tasks 对应的数据或流程。
# 参数关系：session_id 表示所属会话标识；db 表示当前数据库会话。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
@router.get("/sessions/{session_id}/tasks", response_model=list[DurableTaskRead])
def list_session_tasks(session_id: str, db: Session = Depends(get_db)) -> list[dict[str, Any]]:
    _require(db, ChatSession, session_id, "Session")
    # 变量说明：tasks 表示当前流程使用的 tasks 集合。
    tasks = list(db.scalars(
        select(DurableTask)
        .where(DurableTask.session_id == session_id)
        .order_by(DurableTask.updated_at.desc(), DurableTask.created_at.desc(), DurableTask.id.desc())
    ))
    return [task_payload(db, item) for item in tasks]


# 函数职责：读取 session_active_task 对应的数据或流程。
# 参数关系：session_id 表示所属会话标识；db 表示当前数据库会话。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
@router.get("/sessions/{session_id}/active-task", response_model=DurableTaskRead | None)
def get_session_active_task(session_id: str, db: Session = Depends(get_db)) -> dict[str, Any] | None:
    _require(db, ChatSession, session_id, "Session")
    # 变量说明：task 表示当前步骤使用的 task 值。
    task = latest_resumable_task(db, session_id)
    return task_payload(db, task) if task is not None else None


# 函数职责：异步完成 cancel_session_task 对应的业务处理。
# 参数关系：session_id 表示所属会话标识；task_id 表示任务标识；db 表示当前数据库会话。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
@router.post("/sessions/{session_id}/tasks/{task_id}/cancel", response_model=DurableTaskRead)
async def cancel_session_task(session_id: str, task_id: str, db: Session = Depends(get_db)) -> dict[str, Any]:
    _require(db, ChatSession, session_id, "Session")
    # 变量说明：task 表示当前步骤使用的 task 值。
    task = db.get(DurableTask, task_id)
    if task is None or task.session_id != session_id:
        raise HTTPException(status_code=404, detail="Task not found")
    if task.status not in {"planning", "running", "waiting", "paused", "needs_recovery", "blocked"}:
        raise HTTPException(status_code=404, detail="当前会话没有可取消的任务")

    # 变量说明：linked_run_ids 表示linked_run 对象标识集合。
    linked_run_ids = list(db.scalars(
        select(Run.id).where(Run.task_id == task.id).order_by(Run.started_at.desc())
    ))
    for run_id in linked_run_ids:
        coordinator.stop(run_id, db=db, reason="user_interrupted")
    cancel_durable_task(db, task)
    db.commit()
    db.refresh(task)
    return task_payload(db, task)


# 函数职责：列出 session_background_jobs 对应的数据或流程。
# 参数关系：session_id 表示所属会话标识；db 表示当前数据库会话。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
@router.get("/sessions/{session_id}/background-jobs", response_model=list[BackgroundJobRead])
def list_session_background_jobs(
    session_id: str,
    db: Session = Depends(get_db),
) -> list[BackgroundJobRead]:
    _require(db, ChatSession, session_id, "Session")
    # 变量说明：jobs 表示当前流程使用的 jobs 集合。
    jobs = list(db.scalars(
        select(BackgroundJob)
        .where(BackgroundJob.session_id == session_id)
        .order_by(BackgroundJob.created_at.desc(), BackgroundJob.id.desc())
    ))
    return [BackgroundJobRead.model_validate(job) for job in jobs]


# 函数职责：列出 session_delegations 对应的数据或流程。
# 参数关系：session_id 表示所属会话标识；db 表示当前数据库会话。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
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
    # 变量说明：persisted 表示当前步骤使用的 persisted 值。
    persisted = list(db.scalars(
        select(DelegatedTask)
        .where(DelegatedTask.parent_session_id == session_id)
        .order_by(DelegatedTask.updated_at.desc(), DelegatedTask.created_at.desc())
    ))
    # 变量说明：items 表示待处理的元素集合。
    items = [DelegatedTaskRead.model_validate(item) for item in persisted]
    # 变量说明：known_task_ids 表示known_task 对象标识集合。
    known_task_ids = {item.id for item in items}
    # 变量说明：known_child_run_ids 表示known_child_run 对象标识集合。
    known_child_run_ids = {item.child_run_id for item in items if item.child_run_id}

    # 变量说明：legacy_rows 表示当前流程使用的 legacy_rows 集合。
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
        # 变量说明：link 表示当前步骤使用的 link 值。
        link = link_event.payload if isinstance(link_event.payload, dict) else {}
        # 变量说明：task_id 表示任务标识。
        task_id = str(link.get("delegation_id") or link.get("team_task_id") or link_event.id)
        if task_id in known_task_ids or child_run.id in known_child_run_ids:
            continue

        # 变量说明：snapshot 表示当前步骤使用的 snapshot 值。
        snapshot = db.scalar(
            select(RunEvent)
            .where(RunEvent.run_id == child_run.id, RunEvent.event_type == "runtime_snapshot")
            .order_by(RunEvent.created_at.desc())
        )
        # 变量说明：snapshot_payload 表示当前步骤使用的 snapshot_payload 值。
        snapshot_payload = snapshot.payload if snapshot and isinstance(snapshot.payload, dict) else {}
        # 变量说明：messages 表示发送给模型或客户端的消息序列。
        messages = snapshot_payload.get("messages")
        # 变量说明：description 表示当前步骤使用的 description 值。
        description = ""
        if isinstance(messages, list):
            for message in messages:
                if not isinstance(message, dict) or message.get("role") != "user":
                    continue
                # 变量说明：content 表示待处理或返回的正文内容。
                content = message.get("content")
                if isinstance(content, str) and content.strip():
                    # 变量说明：description 表示当前步骤使用的 description 值。
                    description = content.strip()
                    break
        # 变量说明：title 表示当前步骤使用的 title 值。
        title = description.replace("\n", " ").strip()[:80] or "历史子 Agent 任务"
        # 变量说明：output 表示当前步骤使用的 output 值。
        output = snapshot_payload.get("output")
        # 变量说明：result 表示本步骤产生的结果。
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
            # 变量说明：result 的索引项 表示该语句创建或更新的目标数据。
            result["output"] = output
        if child_run.error_message:
            # 变量说明：result 的索引项 表示该语句创建或更新的目标数据。
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


# 函数职责：列出 session_teammates 对应的数据或流程。
# 参数关系：session_id 表示所属会话标识；db 表示当前数据库会话。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
@router.get("/sessions/{session_id}/teammates", response_model=list[TeammateRead])
def list_session_teammates(session_id: str, db: Session = Depends(get_db)) -> list[TeammateWorker]:
    _require(db, ChatSession, session_id, "Session")
    # 变量说明：team_ids 表示team 对象标识集合。
    team_ids = select(CollaborationTeam.id).where(CollaborationTeam.session_id == session_id)
    return list(db.scalars(
        select(TeammateWorker)
        .where(TeammateWorker.team_id.in_(team_ids))
        .order_by(TeammateWorker.created_at.asc(), TeammateWorker.id.asc())
    ))


# 函数职责：列出 session_collaboration_messages 对应的数据或流程。
# 参数关系：session_id 表示所属会话标识；limit 表示当前步骤使用的 limit 值；db 表示当前数据库会话。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
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
    # 变量说明：team_ids 表示team 对象标识集合。
    team_ids = select(CollaborationTeam.id).where(CollaborationTeam.session_id == session_id)
    return list(db.scalars(
        select(CollaborationMessage)
        .where(CollaborationMessage.team_id.in_(team_ids))
        .order_by(CollaborationMessage.created_at.desc(), CollaborationMessage.id.desc())
        .limit(limit)
    ))


# 函数职责：列出 session_collaboration_events 对应的数据或流程。
# 参数关系：session_id 表示所属会话标识；limit 表示当前步骤使用的 limit 值；db 表示当前数据库会话。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
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
    # 变量说明：task_ids 表示task 对象标识集合。
    task_ids = select(DurableTask.id).where(DurableTask.session_id == session_id)
    # 变量说明：run_ids 表示run 对象标识集合。
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


# 函数职责：异步更新 session 对应的数据或流程。
# 参数关系：session_id 表示所属会话标识；payload 表示跨层传递的数据载荷；db 表示当前数据库会话。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
@router.patch("/sessions/{session_id}", response_model=SessionRead)
async def update_session(
    session_id: str, payload: SessionUpdate, db: Session = Depends(get_db)
) -> ChatSession:
    # 变量说明：item 表示当前步骤使用的 item 值。
    item = _require(db, ChatSession, session_id, "Session")
    # 变量说明：updates 表示当前流程使用的 updates 集合。
    updates = payload.model_dump(exclude_unset=True)
    # 变量说明：mcp_selection_changed 表示当前步骤使用的 mcp_selection_changed 值。
    mcp_selection_changed = (
        "mcp_server_names" in updates
        and list(item.mcp_server_names or []) != updates["mcp_server_names"]
    )
    # 变量说明：skill_ids_supplied 表示当前步骤使用的 skill_ids_supplied 值。
    skill_ids_supplied = "skill_ids" in updates
    # 变量说明：skill_ids 表示skill 对象标识集合。
    skill_ids = updates.pop("skill_ids", None)
    if "mcp_server_names" in updates:
        try:
            # 变量说明：updates 的索引项 表示该语句创建或更新的目标数据。
            updates["mcp_server_names"] = validate_mcp_server_names(
                load_mcp_config_source(settings.mcp_config_file),
                updates["mcp_server_names"],
            )
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
    if "permission_mode" in updates:
        set_permission_mode(db, updates["permission_mode"])
    if "model_connection_id" in updates:
        _require_enabled_model_connection(db, updates["model_connection_id"])
    # 变量说明：runtime_settings_changed 表示当前步骤使用的 runtime_settings_changed 值。
    runtime_settings_changed = any(
        field in updates and getattr(item, field) != updates[field]
        for field in _SESSION_RUNTIME_SETTING_FIELDS
    )
    if runtime_settings_changed:
        # 变量说明：active_run_id 表示active_run 对象的唯一标识。
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
    # 变量说明：agent_id 表示智能体标识。
    item.agent_id = DEFAULT_AGENT_ID
    _commit(db)
    db.refresh(item)
    if mcp_selection_changed:
        await mcp_runtime_pool.close_session(session_id)
    refresh_memory_markdown_projection()
    return item


# 函数职责：删除 session 对应的数据或流程。
# 参数关系：session_id 表示所属会话标识；db 表示当前数据库会话。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
@router.delete("/sessions/{session_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_session(session_id: str, db: Session = Depends(get_db)) -> Response:
    # 变量说明：item 表示当前步骤使用的 item 值。
    item = _require(db, ChatSession, session_id, "Session")
    try:
        # 变量说明：effects 表示当前流程使用的 effects 集合。
        effects = stage_session_deletions(db, [item])
    except SessionDeletionConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    _commit(db)
    finalize_session_deletions(effects)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# 函数职责：列出 messages 对应的数据或流程。
# 参数关系：session_id 表示所属会话标识；db 表示当前数据库会话。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
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


# 函数职责：创建 message 对应的数据或流程。
# 参数关系：session_id 表示所属会话标识；payload 表示跨层传递的数据载荷；db 表示当前数据库会话。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
@router.post(
    "/sessions/{session_id}/messages",
    response_model=ChatMessageRead,
    status_code=status.HTTP_201_CREATED,
)
def create_message(
    session_id: str, payload: ChatMessageCreate, db: Session = Depends(get_db)
) -> ChatMessage:
    # 变量说明：session 表示当前步骤使用的 session 值。
    session = _require(db, ChatSession, session_id, "Session")
    # 变量说明：data 表示当前处理的数据。
    data = payload.model_dump(exclude={"metadata"})
    # 变量说明：item 表示当前步骤使用的 item 值。
    item = ChatMessage(
        session_id=session_id,
        sequence=next_chat_message_sequence(db, session_id),
        extra=payload.metadata,
        **data,
    )
    # 变量说明：updated_at 表示最近更新时间。
    session.updated_at = datetime.now(timezone.utc)
    db.add(item)
    _commit(db)
    db.refresh(item)
    return item


# Runs and events -------------------------------------------------------------
