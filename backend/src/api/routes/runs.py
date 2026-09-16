"""Run records and run-event endpoints."""
# 文件职责：负责HTTP 接口、数据契约与依赖装配中的 runs 子模块。
# 逻辑关系：上层通过 api/routes/runs.py 使用本模块；本模块把处理结果交给同领域服务、持久化层或 API 响应层。

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
import json
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
    RunEventPage,
    RunEventRead,
    RunRead,
    RunUpdate,
    SessionCreate,
    SessionRead,
    SessionUpdate,
    TeammateRead,
    ToolResultPageRead,
    WorkspaceCreate,
    WorkspaceRead,
    WorkspaceUpdate,
)
from src.memory.service import recall_memories, refresh_memory_markdown_projection, store_memory
from src.skills.registry import replace_agent_capabilities, replace_session_skills
from src.tasks.state import latest_resumable_task, task_payload
from src.agents.collaboration import cleanup_session_worktrees
# 变量说明：router 表示当前步骤使用的 router 值。
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
from src.persistence.run_events import append_run_event


# 函数职责：执行 read 对应的数据或流程。
# 参数关系：run 表示当前步骤使用的 run 值；session_titles 表示当前流程使用的 session_titles 集合；agent_names 表示当前流程使用的 agent_names 集合；db 表示当前数据库会话。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def _run_read(
    run: Run,
    *,
    session_titles: dict[str, str] | None = None,
    agent_names: dict[str, str] | None = None,
    db: Session | None = None,
) -> RunRead:
    if session_titles is None:
        # 变量说明：session 表示当前步骤使用的 session 值。
        session = db.get(ChatSession, run.session_id) if db is not None and run.session_id else None
        # 变量说明：session_title 表示当前步骤使用的 session_title 值。
        session_title = session.title if session is not None else None
    else:
        # 变量说明：session_title 表示当前步骤使用的 session_title 值。
        session_title = session_titles.get(run.session_id or "")
    if agent_names is None:
        # 变量说明：agent 表示当前步骤使用的 agent 值。
        agent = db.get(Agent, run.agent_id) if db is not None and run.agent_id else None
        # 变量说明：agent_name 表示当前步骤使用的 agent_name 值。
        agent_name = agent.name if agent is not None else None
    else:
        # 变量说明：agent_name 表示当前步骤使用的 agent_name 值。
        agent_name = agent_names.get(run.agent_id or "")
    return RunRead.model_validate(run).model_copy(update={
        "session_title": session_title,
        "agent_name": agent_name,
    })


# 函数职责：完成 message_excerpt 对应的业务处理。
# 参数关系：content 表示待处理或返回的正文内容；limit 表示当前步骤使用的 limit 值。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def _message_excerpt(content: str, limit: int = 180) -> str:
    # 变量说明：normalized 表示当前步骤使用的 normalized 值。
    normalized = " ".join(content.replace("\x00", "").split())
    if len(normalized) <= limit:
        return normalized
    return normalized[: limit - 1].rstrip() + "…"


_TOOL_RESULT_METADATA_FIELDS = (
    "artifact_id", "offset", "next_offset", "total_chars", "eof",
    "command", "exit_code", "truncated", "output_truncated",
    "attachment_id", "extracted_chars",
)
_COMMAND_TOOL_NAMES = frozenset({"shell", "bash", "run_command"})
_BACKGROUND_PAYLOAD_MARKERS = frozenset({
    "id", "session_id", "run_id", "log_path", "pid", "observed_by_run_id",
    "waiting_run_id", "created_at", "started_at", "finished_at", "cwd",
})
_BACKGROUND_PAYLOAD_FIELDS = (
    "command", "shell", "exit_code", "output", "error",
    "output_truncated", "truncated",
)


def _json_object(value: str) -> Mapping[str, Any] | None:
    try:
        decoded = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return None
    return decoded if isinstance(decoded, Mapping) else None


def _project_tool_result(payload: Mapping[str, Any], *, fallback_tool_name: str) -> dict[str, Any]:
    """将持久化 ToolResult 投影为浏览器可读取的最小安全详情。"""

    tool_name = str(payload.get("tool_name") or fallback_tool_name or "unknown")
    raw_content = payload.get("content")
    content = raw_content if isinstance(raw_content, str) else str(raw_content or "")

    if tool_name in _COMMAND_TOOL_NAMES:
        background = _json_object(content)
        if (
            background is not None
            and ("command" in background or "shell" in background)
            and _BACKGROUND_PAYLOAD_MARKERS.intersection(background)
        ):
            # 后台任务正文混有数据库归属与主机路径；详情只保留命令执行结果。
            content = json.dumps(
                {key: background[key] for key in _BACKGROUND_PAYLOAD_FIELDS if key in background},
                ensure_ascii=False,
            )
    elif tool_name == "read_artifact":
        nested = _json_object(content)
        if (
            nested is not None
            and isinstance(nested.get("tool_name"), str)
            and "ok" in nested
            and "content" in nested
        ):
            # Artifact 的完整页可能本身是一层 ToolResult；分页碎片解析失败时仍作为正文保留。
            content = json.dumps(
                _project_tool_result(nested, fallback_tool_name=str(nested["tool_name"])),
                ensure_ascii=False,
            )

    projected: dict[str, Any] = {
        "tool_name": tool_name,
        "ok": payload.get("ok") is True,
        "content": content,
    }
    if isinstance(payload.get("changed"), bool):
        projected["changed"] = payload["changed"]
    if isinstance(payload.get("error_code"), str) and payload["error_code"]:
        projected["error_code"] = payload["error_code"]
    metadata = payload.get("metadata")
    if isinstance(metadata, Mapping):
        safe_metadata = {
            key: metadata[key]
            for key in _TOOL_RESULT_METADATA_FIELDS
            if key in metadata
        }
        if safe_metadata:
            projected["metadata"] = safe_metadata
    return projected

# 函数职责：列出 runs 对应的数据或流程。
# 参数关系：session_id 表示所属会话标识；run_status 表示当前流程使用的 run_status 集合；limit 表示当前步骤使用的 limit 值；offset 表示当前步骤使用的 offset 值；before_started_at 表示before_started_at 对应的时间信息；before_id 表示before 对象的唯一标识；db 表示当前数据库会话。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
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
    # 变量说明：query 表示当前步骤使用的 query 值。
    query = select(Run)
    if session_id:
        # 变量说明：query 表示当前步骤使用的 query 值。
        query = query.where(Run.session_id == session_id)
    if run_status:
        # 变量说明：query 表示当前步骤使用的 query 值。
        query = query.where(Run.status == run_status)
    if (before_started_at is None) != (before_id is None):
        raise HTTPException(status_code=422, detail="before_started_at and before_id must be provided together")
    if before_started_at is not None and before_id is not None:
        # 变量说明：query 表示当前步骤使用的 query 值。
        query = query.where(or_(
            Run.started_at < before_started_at,
            and_(Run.started_at == before_started_at, Run.id < before_id),
        ))
    # 变量说明：runs 表示当前流程使用的 runs 集合。
    runs = list(db.scalars(
        query.order_by(Run.started_at.desc(), Run.id.desc()).offset(offset).limit(limit)
    ))
    # 变量说明：session_ids 表示session 对象标识集合。
    session_ids = {run.session_id for run in runs if run.session_id}
    # 变量说明：agent_ids 表示agent 对象标识集合。
    agent_ids = {run.agent_id for run in runs if run.agent_id}
    # 变量说明：session_titles 表示当前流程使用的 session_titles 集合。
    session_titles = dict(db.execute(
        select(ChatSession.id, ChatSession.title).where(ChatSession.id.in_(session_ids))
    ).all()) if session_ids else {}
    # 变量说明：agent_names 表示当前流程使用的 agent_names 集合。
    agent_names = dict(db.execute(
        select(Agent.id, Agent.name).where(Agent.id.in_(agent_ids))
    ).all()) if agent_ids else {}
    return [
        _run_read(run, session_titles=session_titles, agent_names=agent_names)
        for run in runs
    ]


# 函数职责：创建 run 对应的数据或流程。
# 参数关系：payload 表示跨层传递的数据载荷；db 表示当前数据库会话。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
@router.post("/runs", response_model=RunRead, status_code=status.HTTP_201_CREATED)
def create_run(payload: RunCreate, db: Session = Depends(get_db)) -> RunRead:
    # 变量说明：data 表示当前处理的数据。
    data = payload.model_dump()
    if data.get("session_id"):
        # 变量说明：chat_session 表示当前步骤使用的 chat_session 值。
        chat_session = _require(db, ChatSession, data["session_id"], "Session")
        data["agent_id"] = DEFAULT_AGENT_ID
        data["workspace_id"] = data.get("workspace_id") or chat_session.workspace_id or DEFAULT_WORKSPACE_ID
    # 变量说明：item 表示当前步骤使用的 item 值。
    item = Run(**data)
    db.add(item)
    _commit(db)
    db.refresh(item)
    return _run_read(item, db=db)


# 函数职责：读取 run 对应的数据或流程。
# 参数关系：run_id 表示当前运行标识；db 表示当前数据库会话。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
@router.get("/runs/{run_id}", response_model=RunRead)
def get_run(run_id: str, db: Session = Depends(get_db)) -> RunRead:
    return _run_read(_require(db, Run, run_id, "Run"), db=db)


# 函数职责：按需分页读取某次运行持久化的完整工具结果，同时严格限制消息归属。
@router.get("/runs/{run_id}/tool-results/{tool_call_id}", response_model=ToolResultPageRead)
def get_tool_result(
    run_id: str,
    tool_call_id: str,
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=24_000, ge=1, le=24_000),
    db: Session = Depends(get_db),
) -> ToolResultPageRead:
    run = _require(db, Run, run_id, "Run")
    if not run.session_id:
        raise HTTPException(status_code=404, detail="Tool result not found")

    # 同一会话的不同运行可能复用 provider 生成的 call id；runtime_run_id 是持久化链路中的运行归属。
    candidates = db.scalars(
        select(ChatMessage)
        .where(
            ChatMessage.session_id == run.session_id,
            ChatMessage.role == "tool",
            ChatMessage.tool_call_id == tool_call_id,
        )
        .order_by(ChatMessage.sequence.desc(), ChatMessage.created_at.desc(), ChatMessage.id.desc())
    )
    message = next(
        (
            candidate
            for candidate in candidates
            if isinstance(candidate.extra, dict)
            and candidate.extra.get("runtime_run_id") == run.id
        ),
        None,
    )
    if message is None:
        raise HTTPException(status_code=404, detail="Tool result not found")

    persisted = message.content
    decoded = _json_object(persisted)
    if decoded is None:
        safe_content = persisted
        projected: Mapping[str, Any] = {}
    else:
        projected = _project_tool_result(decoded, fallback_tool_name=message.tool_name or "unknown")
        safe_content = json.dumps(projected, ensure_ascii=False)
    total_chars = len(safe_content)
    end = min(total_chars, offset + limit)
    return ToolResultPageRead(
        tool_call_id=tool_call_id,
        tool_name=message.tool_name or str(projected.get("tool_name") or "unknown"),
        ok=projected.get("ok") is True,
        content=safe_content[offset:end],
        offset=offset,
        next_offset=end,
        total_chars=total_chars,
        eof=end >= total_chars,
    )


# 函数职责：更新 run 对应的数据或流程。
# 参数关系：run_id 表示当前运行标识；payload 表示跨层传递的数据载荷；db 表示当前数据库会话。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
@router.patch("/runs/{run_id}", response_model=RunRead)
def update_run(run_id: str, payload: RunUpdate, db: Session = Depends(get_db)) -> RunRead:
    # 变量说明：item 表示当前步骤使用的 item 值。
    item = _require(db, Run, run_id, "Run")
    _apply(item, payload)
    _commit(db)
    db.refresh(item)
    return _run_read(item, db=db)


# 函数职责：列出 run_events 对应的数据或流程。
# 参数关系：run_id 表示当前运行标识；db 表示当前数据库会话。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
@router.get("/runs/{run_id}/events", response_model=RunEventPage)
def list_run_events(
    run_id: str,
    event_type: list[str] | None = Query(default=None),
    step: int | None = Query(default=None, ge=0),
    errors_only: bool = Query(default=False),
    before: int | None = Query(default=None, ge=1),
    limit: int = Query(default=100, ge=1, le=200),
    db: Session = Depends(get_db),
) -> RunEventPage:
    # 变量说明：run 表示当前步骤使用的 run 值。
    run = _require(db, Run, run_id, "Run")
    # 变量说明：user_message 表示当前步骤使用的 user_message 值。
    user_message = db.scalar(
        select(ChatMessage.content)
        .where(ChatMessage.turn_id == run.turn_id, ChatMessage.role == "user")
        .order_by(ChatMessage.sequence.asc(), ChatMessage.created_at.asc())
        .limit(1)
    ) if run.turn_id else None
    # 变量说明：message_excerpt 表示当前步骤使用的 message_excerpt 值。
    message_excerpt = _message_excerpt(user_message) if user_message else None
    # 变量说明：events 表示运行事件集合。
    events = list(db.scalars(select(RunEvent).where(RunEvent.run_id == run_id).order_by(RunEvent.sequence.desc(), RunEvent.id.desc())))
    # 变量说明：public_events 表示当前流程使用的 public_events 集合。
    public_events: list[RunEventRead] = []
    for event in events:
        # 变量说明：public 表示当前步骤使用的 public 值。
        public = _public_run_event(event)
        if public is None or (before is not None and (event.sequence or 0) >= before):
            continue
        if event_type and event.event_type not in set(event_type):
            continue
        if step is not None and event.step != step:
            continue
        if errors_only and not (
            "failed" in event.event_type or "error" in event.event_type or "retry" in event.event_type
            or (isinstance(event.payload, dict) and (event.payload.get("error_code") or event.payload.get("ok") is False))
        ):
            continue
        if message_excerpt and public.event_type in {"context_prepared", "context_resumed"}:
            # 变量说明：public 表示当前步骤使用的 public 值。
            public = public.model_copy(update={
                "payload": {**public.payload, "message_excerpt": message_excerpt},
            })
        public_events.append(public)
    # SQL 采用倒序抓取最近事件；返回前翻回时间正序，便于 UI 连续追加历史页。
    page_items = list(reversed(public_events[:limit]))
    next_before = public_events[limit - 1].sequence if len(public_events) > limit else None
    return RunEventPage(items=page_items, next_before=next_before)


# 函数职责：创建 run_event 对应的数据或流程。
# 参数关系：run_id 表示当前运行标识；payload 表示跨层传递的数据载荷；db 表示当前数据库会话。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
@router.post("/runs/{run_id}/events", response_model=RunEventRead, status_code=status.HTTP_201_CREATED)
def create_run_event(
    run_id: str, payload: RunEventCreate, db: Session = Depends(get_db)
) -> RunEventRead:
    _require(db, Run, run_id, "Run")
    if "checkpoint" in payload.event_type.casefold() or payload.event_type.casefold().endswith("_snapshot"):
        raise HTTPException(status_code=422, detail="该事件类型仅供运行恢复使用")
    item = append_run_event(db, run_id=run_id, **payload.model_dump())
    _commit(db)
    db.refresh(item)
    public = _public_run_event(item)
    if public is None:
        public = RunEventRead(
            id=item.id,
            run_id=item.run_id,
            event_type=item.event_type,
            trace_id=item.trace_id,
            sequence=item.sequence,
            step=item.step,
            payload={},
            created_at=item.created_at,
        )
    return public


# Approvals ------------------------------------------------------------------
