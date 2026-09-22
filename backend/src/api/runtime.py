"""Endpoints that launch and resume real agent executions."""
# 文件职责：负责HTTP 接口、数据契约与依赖装配中的 runtime 子模块。
# 逻辑关系：上层通过 api/runtime.py 使用本模块；本模块把处理结果交给同领域服务、持久化层或 API 响应层。

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, Sequence

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, Field, ValidationError
from starlette.datastructures import UploadFile
from sse_starlette.sse import EventSourceResponse
from sqlalchemy import select, text, update
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session as OrmSession

from src.persistence import database as database_module
from src.config import settings
from src.persistence.database import (
    DEFAULT_AGENT_ID,
    DEFAULT_WORKSPACE_ID,
    Agent,
    Approval,
    ConversationCompaction,
    DraftLaunch,
    ModelConnection,
    Run,
    RunEvent,
    Session,
    Workspace,
    get_db,
)
from src.persistence.run_events import append_run_event
from src.api.schemas import (
    ApprovalRead,
    DraftLaunchRead,
    DraftLaunchRequest,
    RunRead,
    SessionRead,
    WorkspaceRead,
)
from src.runs.service import (
    ACTIVE_STATUSES,
    _prepare_session_history,
    approval_matches_pending,
    coordinator,
)
from src.runs.stream import TERMINAL_EVENT_TYPES, run_stream_broker
from src.mcp.config import load_mcp_config_source, validate_mcp_server_names
from src.permissions.preferences import get_permission_mode, set_permission_mode
from src.skills.registry import replace_session_skills, validate_skill_ids
from src.tasks.state import (
    bind_recovery_task,
    cancel_durable_task,
    is_task_cancellation_request,
    latest_resumable_task,
)
from src.sessions.delivery import (
    find_turn_by_client_message,
    is_terminal_delivery,
    persist_terminal_response,
    run_for_turn,
    stage_user_turn,
)
from src.attachments.storage import (
    UploadedAttachment,
    persist_uploaded_attachments,
    read_uploaded_attachments,
)
from src.context.assembly import FilesystemArtifactStore
from src.api.routes.shared import _public_run_event_payload


# 变量说明：router 表示当前步骤使用的 router 值。
router = APIRouter(prefix="/api", tags=["runtime"])


# 函数职责：流式传输 event_is_terminal 对应的数据或流程。
# 参数关系：event 表示当前运行事件。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def _stream_event_is_terminal(event: dict) -> bool:
    # 变量说明：event_type 表示当前步骤使用的 event_type 值。
    event_type = str(event.get("type") or "")
    if event_type not in TERMINAL_EVENT_TYPES:
        return False
    if event_type in {"run_stopped", "stopped"}:
        return is_terminal_delivery(
            "stopped",
            str(event.get("stop_reason") or event.get("reason") or "") or None,
        )
    return True


def _public_stream_event(event: dict) -> dict:
    """对 SSE 事件应用与持久事件接口相同的公开字段投影。"""

    event_type = str(event.get("type") or "message")
    public: dict = {"type": event_type}
    if event.get("event_id") is not None:
        public["event_id"] = event["event_id"]
    if event_type == "run_state":
        for key in (
            "run_id", "status", "current_step", "tool_calls", "stop_reason",
            "error_code", "error", "terminal",
        ):
            if key in event:
                public[key] = event[key]
        return public
    if event_type in {"assistant_delta", "thought_delta"}:
        if isinstance(event.get("delta"), str):
            public["delta"] = event["delta"]
        if isinstance(event.get("step"), int):
            public["step"] = event["step"]
        return public
    public.update(_public_run_event_payload(event_type, event))
    return public


# 类职责：定义 SessionRunRequest 的跨层数据契约。
# 继承关系：复用基类提供的契约，并向调用方暴露本类声明的字段和方法。
class SessionRunRequest(BaseModel):
    # 变量说明：content 表示待处理或返回的正文内容。
    content: str = Field(default="", max_length=100_000)
    # 变量说明：idempotency_key 表示当前步骤使用的 idempotency_key 值。
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=255)


# 类职责：定义 SessionContextRead 在本领域中的数据与行为。
# 继承关系：复用基类提供的契约，并向调用方暴露本类声明的字段和方法。
class SessionContextRead(BaseModel):
    # Codex 风格字段：active_context_tokens 表示当前窗口实际占用量；其余字段描述自动压缩范围和剩余预算。
    active_context_tokens: int
    auto_compact_scope_tokens: int
    auto_compact_scope_limit: int
    full_context_window_limit: int
    base_window_tokens_remaining: int
    token_limit_reached: bool
    # 变量说明：used_tokens 表示当前流程使用的 used_tokens 集合。
    used_tokens: int
    # 变量说明：limit_tokens 表示当前流程使用的 limit_tokens 集合。
    limit_tokens: int
    # 变量说明：compact_threshold_tokens 表示当前流程使用的 compact_threshold_tokens 集合。
    compact_threshold_tokens: int
    # 变量说明：percent 表示当前步骤使用的 percent 值。
    percent: float
    # 变量说明：last_compaction_at 表示last_compaction_at 对应的时间信息。
    last_compaction_at: datetime | None


# 类职责：定义 ApprovalRuntimeDecision 在本领域中的数据与行为。
# 继承关系：复用基类提供的契约，并向调用方暴露本类声明的字段和方法。
class ApprovalRuntimeDecision(BaseModel):
    # 变量说明：decision 表示当前步骤使用的 decision 值。
    decision: Literal["approve", "reject"]
    # 变量说明：reason 表示当前步骤使用的 reason 值。
    reason: str | None = None


# 类职责：定义 StopRunRequest 的跨层数据契约。
# 继承关系：复用基类提供的契约，并向调用方暴露本类声明的字段和方法。
class StopRunRequest(BaseModel):
    # Keep the public API intentionally narrow.  Runtime safety/guard stops
    # are produced by the engine itself; clients may only request a user stop.
    # 变量说明：reason 表示当前步骤使用的 reason 值。
    reason: Literal["user_interrupted"] = "user_interrupted"


# 函数职责：完成 normalized_workspace_root 对应的业务处理。
# 参数关系：root_path 表示root_path 对应的文件系统位置。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def _normalized_workspace_root(root_path: str) -> str:
    """Canonicalize a selected project directory before comparing it."""

    return str(Path(root_path).expanduser().resolve())


# 函数职责：完成 workspace_name_from_root 对应的业务处理。
# 参数关系：root_path 表示root_path 对应的文件系统位置。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def _workspace_name_from_root(root_path: str) -> str:
    # 变量说明：name 表示当前对象名称。
    name = Path(root_path).name.strip()
    return name or "项目"


# 函数职责：完成 begin_draft_transaction 对应的业务处理。
# 参数关系：db 表示当前数据库会话。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def _begin_draft_transaction(db: OrmSession) -> None:
    """Serialize local SQLite draft launches before checking project roots.

    ``root_path`` has no legacy database uniqueness constraint.  Taking an
    immediate write lock before the lookup makes two distinct first-send
    requests for the same normalized path observe one project row instead of
    racing to insert two.  Other database engines retain their normal
    transaction semantics and the draft idempotency unique key still protects
    retry requests.
    """

    if db.get_bind().dialect.name != "sqlite":
        return
    try:
        db.execute(text("BEGIN IMMEDIATE"))
    except OperationalError as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Draft launch is busy; retry with the same idempotency key",
        ) from exc


# 函数职责：完成 draft_request_fingerprint 对应的业务处理。
# 参数关系：payload 表示跨层传递的数据载荷；normalized_root 表示当前步骤使用的 normalized_root 值；uploads 表示当前流程使用的 uploads 集合。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def _draft_request_fingerprint(
    payload: DraftLaunchRequest,
    normalized_root: str | None,
    uploads: Sequence[UploadedAttachment] = (),
) -> str:
    """Bind an idempotency key to its exact materialisation request."""

    # 变量说明：canonical 表示当前步骤使用的 canonical 值。
    canonical = {
        "title": payload.title,
        "content": payload.content,
        "root_path": normalized_root,
        "model_connection_id": payload.model_connection_id,
        "model_id": payload.model_id,
        "thinking_level": payload.thinking_level,
        "permission_mode": payload.permission_mode,
        "use_memories": payload.use_memories,
        "skill_ids": sorted({skill_id.strip() for skill_id in payload.skill_ids}),
        "mcp_server_names": sorted({name.strip() for name in payload.mcp_server_names}),
    }
    # 变量说明：encoded 表示当前步骤使用的 encoded 值。
    encoded = json.dumps(canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    # 变量说明：digest 表示当前步骤使用的 digest 值。
    digest = hashlib.sha256(encoded.encode("utf-8"))
    for upload in uploads:
        digest.update(b"\0attachment\0")
        digest.update(upload.name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(upload.mime_type.encode("ascii", errors="replace"))
        digest.update(b"\0")
        digest.update(upload.data)
    return digest.hexdigest()


# 函数职责：完成 turn_request_fingerprint 对应的业务处理。
# 参数关系：content 表示待处理或返回的正文内容；uploads 表示当前流程使用的 uploads 集合。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def _turn_request_fingerprint(content: str, uploads: Sequence[UploadedAttachment]) -> str:
    # 变量说明：digest 表示当前步骤使用的 digest 值。
    digest = hashlib.sha256(content.strip().encode("utf-8"))
    for upload in uploads:
        digest.update(b"\0attachment\0")
        digest.update(upload.name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(upload.mime_type.encode("ascii", errors="replace"))
        digest.update(b"\0")
        digest.update(upload.data)
    return digest.hexdigest()


# 函数职责：异步完成 multipart_payload 对应的业务处理。
# 参数关系：request 表示调用方传入的请求数据；model_type 表示当前步骤使用的 model_type 值。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
async def _multipart_payload(
    request: Request,
    model_type: type[DraftLaunchRequest] | type[SessionRunRequest],
) -> tuple[DraftLaunchRequest | SessionRunRequest, list[UploadedAttachment]]:
    # 变量说明：form 表示当前步骤使用的 form 值。
    form = await request.form(max_files=10, max_fields=10, max_part_size=25 * 1024 * 1024)
    # 变量说明：raw_payload 表示当前步骤使用的 raw_payload 值。
    raw_payload = form.get("payload")
    try:
        # 变量说明：payload 表示跨层传递的数据载荷。
        payload = model_type.model_validate_json(str(raw_payload or ""))
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=exc.errors()) from exc
    # 变量说明：files 表示当前流程使用的 files 集合。
    files = [item for item in form.getlist("files") if isinstance(item, UploadFile)]
    # 变量说明：uploads 表示当前流程使用的 uploads 集合。
    uploads = await read_uploaded_attachments(files)
    if not payload.content and not uploads:
        raise HTTPException(status_code=422, detail="消息内容和附件不能同时为空")
    return payload, uploads


# 函数职责：完成 workspace_for_root 对应的业务处理。
# 参数关系：db 表示当前数据库会话；normalized_root 表示当前步骤使用的 normalized_root 值。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def _workspace_for_root(db: OrmSession, normalized_root: str) -> Workspace | None:
    """Find an existing project while repairing legacy non-canonical paths."""

    for workspace in db.scalars(select(Workspace).order_by(Workspace.created_at.asc(), Workspace.id.asc())):
        if _normalized_workspace_root(workspace.root_path) == normalized_root:
            # 变量说明：root_path 表示root_path 对应的文件系统位置。
            workspace.root_path = normalized_root
            return workspace
    return None


# 函数职责：完成 draft_launch_response 对应的业务处理。
# 参数关系：record 表示当前步骤使用的 record 值；expected_fingerprint 表示当前步骤使用的 expected_fingerprint 值；db 表示当前数据库会话；reused 表示当前步骤使用的 reused 值。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def _draft_launch_response(
    record: DraftLaunch,
    *,
    expected_fingerprint: str,
    db: OrmSession,
    reused: bool,
) -> DraftLaunchRead:
    if record.request_fingerprint != expected_fingerprint:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Idempotency key was already used with a different draft payload",
        )
    # 变量说明：chat_session 表示当前步骤使用的 chat_session 值。
    chat_session = db.get(Session, record.session_id) if record.session_id else None
    # 变量说明：run 表示当前步骤使用的 run 值。
    run = db.get(Run, record.run_id) if record.run_id else None
    if chat_session is None or run is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="The resources for this draft launch are no longer available",
        )
    # 变量说明：workspace_id 表示工作区标识。
    workspace_id = record.workspace_id or chat_session.workspace_id
    # 变量说明：workspace 表示当前步骤使用的 workspace 值。
    workspace = db.get(Workspace, workspace_id) if workspace_id else None
    return DraftLaunchRead(
        workspace=WorkspaceRead.model_validate(workspace) if workspace is not None else None,
        session=SessionRead.model_validate(chat_session),
        run=RunRead.model_validate(run),
        reused=reused,
    )


# 函数职责：完成 mark_unscheduled_run 对应的业务处理。
# 参数关系：db 表示当前数据库会话；run 表示当前步骤使用的 run 值；error 表示当前捕获或准备上报的错误。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def _mark_unscheduled_run(
    db: OrmSession,
    run: Run,
    error: BaseException | None = None,
) -> None:
    """Persist a terminal outcome if the post-commit coordinator cannot start."""

    # 变量说明：status 表示当前对象或运行的状态。
    run.status = "failed"
    # 变量说明：error_code 表示当前步骤使用的 error_code 值。
    run.error_code = "launch_unavailable"
    # 变量说明：error_message 表示当前步骤使用的 error_message 值。
    run.error_message = "任务已经保存，但本地执行器未能启动。"
    # 变量说明：finished_at 表示finished_at 对应的时间信息。
    run.finished_at = datetime.now(timezone.utc)
    # 变量说明：event_payload 表示当前步骤使用的 event_payload 值。
    event_payload = {"error_type": "launch_unavailable", "phase": "launch"}
    if error is not None:
        event_payload.update({
            "exception_type": type(error).__name__,
            # Private diagnostic evidence. Public event serializers omit this
            # field, and the user-facing reply remains deterministic.
            "internal_error": (str(error) or type(error).__name__)[:20_000],
        })
    append_run_event(
        db,
        run_id=run.id,
        event_type="integration_failed",
        payload=event_payload,
    )
    persist_terminal_response(
        db,
        run,
        error_code=run.error_code,
        error_message=run.error_message,
    )
    db.commit()


# 函数职责：异步完成 launch_draft_core 对应的业务处理。
# 参数关系：payload 表示跨层传递的数据载荷；response 表示下游返回的响应；db 表示当前数据库会话；uploads 表示当前流程使用的 uploads 集合。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
async def _launch_draft_core(
    payload: DraftLaunchRequest,
    response: Response,
    db: OrmSession = Depends(get_db),
    uploads: Sequence[UploadedAttachment] = (),
) -> DraftLaunchRead:
    """Atomically turn a client-only draft into its first persisted run.

    The browser may retry a request after a navigation or transport failure.
    All rows are created in one transaction and the coordinator is scheduled
    only after that transaction commits, so reusing the same key is a pure
    read of the original launch rather than another side effect.
    """

    # 变量说明：normalized_root 表示当前步骤使用的 normalized_root 值。
    normalized_root = _normalized_workspace_root(payload.root_path) if payload.root_path else None
    if not payload.content and not uploads:
        raise HTTPException(status_code=422, detail="消息内容和附件不能同时为空")
    # 变量说明：fingerprint 表示当前步骤使用的 fingerprint 值。
    fingerprint = _draft_request_fingerprint(payload, normalized_root, uploads)
    try:
        _begin_draft_transaction(db)
        # 变量说明：existing 表示当前步骤使用的 existing 值。
        existing = db.scalar(
            select(DraftLaunch).where(DraftLaunch.idempotency_key == payload.idempotency_key)
        )
        if existing is not None:
            # 变量说明：result 表示本步骤产生的结果。
            result = _draft_launch_response(
                existing,
                expected_fingerprint=fingerprint,
                db=db,
                reused=True,
            )
            db.commit()
            # 变量说明：status_code 表示当前步骤使用的 status_code 值。
            response.status_code = status.HTTP_200_OK
            return result

        # 变量说明：agent 表示当前步骤使用的 agent 值。
        agent = db.get(Agent, DEFAULT_AGENT_ID)
        if agent is None or not agent.enabled:
            raise HTTPException(status_code=409, detail="PGAgent main coordinator is unavailable")
        if payload.model_connection_id is not None:
            # 变量说明：connection 表示当前步骤使用的 connection 值。
            connection = db.get(ModelConnection, payload.model_connection_id)
            if connection is None:
                raise HTTPException(status_code=409, detail="Selected model connection does not exist")
            if not connection.enabled:
                raise HTTPException(status_code=409, detail="Selected model connection is disabled")
        # Validate before any workspace/session/message/run row is staged so
        # an invalid or disabled Skill leaves an unsent draft fully ephemeral.
        # 变量说明：skill_ids 表示skill 对象标识集合。
        skill_ids = validate_skill_ids(db, payload.skill_ids)
        try:
            # 变量说明：mcp_server_names 表示当前流程使用的 mcp_server_names 集合。
            mcp_server_names = validate_mcp_server_names(
                load_mcp_config_source(settings.mcp_config_file),
                payload.mcp_server_names,
            )
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

        if normalized_root is None:
            # 变量说明：workspace 表示当前步骤使用的 workspace 值。
            workspace = db.get(Workspace, DEFAULT_WORKSPACE_ID)
            if workspace is None or not workspace.enabled:
                raise HTTPException(status_code=409, detail="The default task workspace is unavailable")
        else:
            # 变量说明：workspace 表示当前步骤使用的 workspace 值。
            workspace = _workspace_for_root(db, normalized_root)
            if workspace is None:
                # 变量说明：workspace 表示当前步骤使用的 workspace 值。
                workspace = Workspace(
                    name=_workspace_name_from_root(normalized_root),
                    description="",
                    root_path=normalized_root,
                    enabled=True,
                )
                db.add(workspace)
                db.flush()

        # 变量说明：chat_session 表示当前步骤使用的 chat_session 值。
        permission_mode = payload.permission_mode or get_permission_mode(db)
        if payload.permission_mode is not None:
            set_permission_mode(db, payload.permission_mode)
        chat_session = Session(
            title=payload.title,
            workspace_id=workspace.id,
            agent_id=DEFAULT_AGENT_ID,
            model_connection_id=payload.model_connection_id,
            model_id=payload.model_id,
            thinking_level=payload.thinking_level,
            permission_mode=permission_mode,
            use_memories=payload.use_memories,
            mcp_server_names=mcp_server_names,
            status="active",
        )
        db.add(chat_session)
        db.flush()
        replace_session_skills(db, chat_session, skill_ids)
        # 变量说明：attachments 表示当前流程使用的 attachments 集合。
        attachments = persist_uploaded_attachments(
            db,
            session_id=chat_session.id,
            artifact_store=FilesystemArtifactStore(
                settings.data_dir / "artifacts" / chat_session.id
            ),
            uploads=uploads,
        )
        # 变量说明：_turn 表示当前步骤使用的 _turn 值；message 表示当前消息；run 表示当前步骤使用的 run 值。
        _turn, message, run = stage_user_turn(
            db,
            session_id=chat_session.id,
            workspace_id=workspace.id,
            agent_id=DEFAULT_AGENT_ID,
            content=payload.content,
            mode="auto",
            client_message_id=payload.idempotency_key,
            fingerprint=fingerprint,
            message_extra={
                "mode": "auto",
                "source": "draft_launch",
                "attachments": attachments,
            },
        )
        # 变量说明：provider_payload 表示当前步骤使用的 provider_payload 值。
        message.provider_payload = {"attachment_refs": attachments} if attachments else {}
        # 变量说明：record 表示当前步骤使用的 record 值。
        record = DraftLaunch(
            idempotency_key=payload.idempotency_key,
            request_fingerprint=fingerprint,
            workspace_id=workspace.id,
            session_id=chat_session.id,
            run_id=run.id,
        )
        db.add(record)
        db.commit()
    except HTTPException:
        db.rollback()
        raise
    except IntegrityError as exc:
        # A non-SQLite deployment can still race the idempotency unique key.
        # Its losing transaction is wholly rolled back before returning the
        # winning launch, so no partial workspace/session/message/run remains.
        db.rollback()
        # 变量说明：existing 表示当前步骤使用的 existing 值。
        existing = db.scalar(
            select(DraftLaunch).where(DraftLaunch.idempotency_key == payload.idempotency_key)
        )
        if existing is not None:
            # 变量说明：result 表示本步骤产生的结果。
            result = _draft_launch_response(
                existing,
                expected_fingerprint=fingerprint,
                db=db,
                reused=True,
            )
            db.commit()
            # 变量说明：status_code 表示当前步骤使用的 status_code 值。
            response.status_code = status.HTTP_200_OK
            return result
        raise HTTPException(status_code=409, detail="Draft launch could not be persisted") from exc
    except Exception:
        db.rollback()
        raise

    # 变量说明：result 表示本步骤产生的结果。
    result = _draft_launch_response(record, expected_fingerprint=fingerprint, db=db, reused=False)
    try:
        # 变量说明：scheduled 表示当前步骤使用的 scheduled 值。
        scheduled = coordinator.launch(run.id)
    except Exception as exc:
        _mark_unscheduled_run(db, run, exc)
        return _draft_launch_response(record, expected_fingerprint=fingerprint, db=db, reused=False)
    if not scheduled:
        _mark_unscheduled_run(db, run)
        return _draft_launch_response(record, expected_fingerprint=fingerprint, db=db, reused=False)
    return result


# 函数职责：异步完成 launch_draft 对应的业务处理。
# 参数关系：payload 表示跨层传递的数据载荷；response 表示下游返回的响应；db 表示当前数据库会话。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
@router.post("/drafts/launch", response_model=DraftLaunchRead, status_code=status.HTTP_202_ACCEPTED)
async def launch_draft(
    payload: DraftLaunchRequest,
    response: Response,
    db: OrmSession = Depends(get_db),
) -> DraftLaunchRead:
    return await _launch_draft_core(payload, response, db)


# 函数职责：异步完成 launch_draft_input 对应的业务处理。
# 参数关系：request 表示调用方传入的请求数据；response 表示下游返回的响应；db 表示当前数据库会话。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
@router.post("/drafts/launch-input", response_model=DraftLaunchRead, status_code=status.HTTP_202_ACCEPTED)
async def launch_draft_input(
    request: Request,
    response: Response,
    db: OrmSession = Depends(get_db),
) -> DraftLaunchRead:
    # 变量说明：payload 表示跨层传递的数据载荷；uploads 表示当前流程使用的 uploads 集合。
    payload, uploads = await _multipart_payload(request, DraftLaunchRequest)
    return await _launch_draft_core(payload, response, db, uploads)


# 函数职责：异步完成 launch_session_run_core 对应的业务处理。
# 参数关系：session_id 表示所属会话标识；payload 表示跨层传递的数据载荷；response 表示下游返回的响应；db 表示当前数据库会话；uploads 表示当前流程使用的 uploads 集合。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
async def _launch_session_run_core(
    session_id: str,
    payload: SessionRunRequest,
    response: Response,
    db: OrmSession = Depends(get_db),
    uploads: Sequence[UploadedAttachment] = (),
) -> Run:
    # 变量说明：chat_session 表示当前步骤使用的 chat_session 值。
    chat_session = db.get(Session, session_id)
    if chat_session is None:
        raise HTTPException(status_code=404, detail="Session not found")
    # 变量说明：content 表示待处理或返回的正文内容。
    content = payload.content.strip()
    if not content and not uploads:
        raise HTTPException(status_code=422, detail="消息内容和附件不能同时为空")
    # 变量说明：client_message_id 表示client_message 对象的唯一标识。
    client_message_id = payload.idempotency_key.strip() if payload.idempotency_key else None
    # 变量说明：fingerprint 表示当前步骤使用的 fingerprint 值。
    fingerprint = _turn_request_fingerprint(content, uploads)
    # 变量说明：existing_turn 表示当前步骤使用的 existing_turn 值。
    existing_turn = find_turn_by_client_message(
        db,
        session_id=session_id,
        client_message_id=client_message_id,
    )
    if existing_turn is not None:
        if existing_turn.request_fingerprint != fingerprint:
            raise HTTPException(status_code=409, detail="该消息标识已用于不同内容")
        # 变量说明：existing_run 表示当前步骤使用的 existing_run 值。
        existing_run = run_for_turn(db, existing_turn.id)
        if existing_run is None:
            raise HTTPException(status_code=409, detail="已接收消息对应的运行记录不存在")
        # 变量说明：status_code 表示当前步骤使用的 status_code 值。
        response.status_code = status.HTTP_200_OK
        return existing_run
    # A session always runs through the fixed PGAgent coordinator. This also
    # repairs a legacy session lazily if it predates the coordinator migration.
    # 变量说明：agent 表示当前步骤使用的 agent 值。
    agent = db.get(Agent, DEFAULT_AGENT_ID)
    if agent is None:
        raise HTTPException(status_code=409, detail="PGAgent 主控不可用")
    # 变量说明：agent_id 表示智能体标识。
    chat_session.agent_id = DEFAULT_AGENT_ID
    # 变量说明：workspace_id 表示工作区标识。
    workspace_id = chat_session.workspace_id or agent.workspace_id or DEFAULT_WORKSPACE_ID
    if not workspace_id or db.get(Workspace, workspace_id) is None:
        raise HTTPException(status_code=409, detail="请先为会话或 Agent 选择有效工作区")
    # 变量说明：cancellation_request 表示当前步骤使用的 cancellation_request 值。
    cancellation_request = is_task_cancellation_request(content)
    if cancellation_request:
        # 变量说明：task 表示当前步骤使用的 task 值。
        task = latest_resumable_task(db, session_id)
        if task is not None:
            # 变量说明：linked_run_ids 表示linked_run 对象标识集合。
            linked_run_ids = list(db.scalars(
                select(Run.id).where(Run.task_id == task.id).order_by(Run.started_at.desc())
            ))
            for linked_run_id in linked_run_ids:
                coordinator.stop(linked_run_id, db=db, reason="user_interrupted")
            cancel_durable_task(db, task)
            db.commit()

    # 变量说明：active 表示当前步骤使用的 active 值。
    active = db.scalar(select(Run).where(Run.session_id == session_id, Run.status.in_(ACTIVE_STATUSES | {"awaiting_approval"})))
    if active is not None:
        raise HTTPException(status_code=409, detail="当前会话已有运行或待审批工具，请先处理后再发送")

    # 变量说明：mode 表示当前步骤使用的 mode 值。
    mode = "auto"
    # 变量说明：attachments 表示当前流程使用的 attachments 集合。
    attachments = persist_uploaded_attachments(
        db,
        session_id=session_id,
        artifact_store=FilesystemArtifactStore(
            settings.data_dir / "artifacts" / session_id
        ),
        uploads=uploads,
    )
    # 变量说明：_turn 表示当前步骤使用的 _turn 值；message 表示当前消息；run 表示当前步骤使用的 run 值。
    _turn, message, run = stage_user_turn(
        db,
        session_id=session_id,
        workspace_id=workspace_id,
        agent_id=agent.id,
        content=content,
        mode=mode,
        client_message_id=client_message_id,
        fingerprint=fingerprint,
        message_extra={"mode": mode, "attachments": attachments},
    )
    # 变量说明：provider_payload 表示当前步骤使用的 provider_payload 值。
    message.provider_payload = {"attachment_refs": attachments} if attachments else {}
    if not cancellation_request:
        # 变量说明：task_prompt 表示当前步骤使用的 task_prompt 值。
        task_prompt = content or f"分析附件：{', '.join(item['name'] for item in attachments)}"
        bind_recovery_task(db, run, task_prompt)
    # 变量说明：updated_at 表示最近更新时间。
    chat_session.updated_at = datetime.now(timezone.utc)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        # 变量说明：existing_turn 表示当前步骤使用的 existing_turn 值。
        existing_turn = find_turn_by_client_message(
            db,
            session_id=session_id,
            client_message_id=client_message_id,
        )
        if existing_turn is None or existing_turn.request_fingerprint != fingerprint:
            raise HTTPException(status_code=409, detail="消息接收发生冲突") from exc
        # 变量说明：existing_run 表示当前步骤使用的 existing_run 值。
        existing_run = run_for_turn(db, existing_turn.id)
        if existing_run is None:
            raise HTTPException(status_code=409, detail="已接收消息对应的运行记录不存在") from exc
        # 变量说明：status_code 表示当前步骤使用的 status_code 值。
        response.status_code = status.HTTP_200_OK
        return existing_run
    db.refresh(run)
    try:
        # 变量说明：scheduled 表示当前步骤使用的 scheduled 值。
        scheduled = coordinator.launch(run.id)
    except Exception as exc:
        _mark_unscheduled_run(db, run, exc)
        db.refresh(run)
        return run
    if not scheduled:
        _mark_unscheduled_run(db, run)
        db.refresh(run)
    return run


# 函数职责：异步完成 launch_session_run 对应的业务处理。
# 参数关系：session_id 表示所属会话标识；payload 表示跨层传递的数据载荷；response 表示下游返回的响应；db 表示当前数据库会话。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
@router.post("/sessions/{session_id}/run", response_model=RunRead, status_code=status.HTTP_202_ACCEPTED)
async def launch_session_run(
    session_id: str,
    payload: SessionRunRequest,
    response: Response,
    db: OrmSession = Depends(get_db),
) -> Run:
    return await _launch_session_run_core(session_id, payload, response, db)


# 函数职责：异步完成 launch_session_turn 对应的业务处理。
# 参数关系：session_id 表示所属会话标识；request 表示调用方传入的请求数据；response 表示下游返回的响应；db 表示当前数据库会话。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
@router.post("/sessions/{session_id}/turns", response_model=RunRead, status_code=status.HTTP_202_ACCEPTED)
async def launch_session_turn(
    session_id: str,
    request: Request,
    response: Response,
    db: OrmSession = Depends(get_db),
) -> Run:
    # 变量说明：payload 表示跨层传递的数据载荷；uploads 表示当前流程使用的 uploads 集合。
    payload, uploads = await _multipart_payload(request, SessionRunRequest)
    return await _launch_session_run_core(session_id, payload, response, db, uploads)


# 函数职责：异步流式传输 run 对应的数据或流程。
# 参数关系：run_id 表示当前运行标识；request 表示调用方传入的请求数据。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
@router.get("/runs/{run_id}/stream")
async def stream_run(run_id: str, request: Request) -> EventSourceResponse:
    """Stream transient JSON events for one local run using SSE."""

    with database_module.SessionLocal() as db:
        # 变量说明：run 表示当前步骤使用的 run 值。
        run = db.get(Run, run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="Run not found")
        # 变量说明：initial_state 表示当前步骤使用的 initial_state 值。
        initial_state = {
            "type": "run_state",
            "run_id": run.id,
            "status": run.status,
            "current_step": run.current_step,
            "tool_calls": run.tool_calls,
            "stop_reason": run.stop_reason,
            "error_code": run.error_code,
            "error": run.error_message,
            "terminal": is_terminal_delivery(run.status, run.stop_reason),
        }

    # 变量说明：replay 表示当前步骤使用的 replay 值；subscription 表示当前步骤使用的 subscription 值。
    replay, subscription = run_stream_broker.subscribe(run_id)
    # 变量说明：last_event_id 表示last_event 对象的唯一标识。
    last_event_id = request.headers.get("last-event-id")
    if last_event_id:
        for index, event in enumerate(replay):
            if str(event.get("event_id") or "") == last_event_id:
                # 变量说明：replay 表示当前步骤使用的 replay 值。
                replay = replay[index + 1 :]
                break

    # 函数职责：完成 encode 对应的业务处理。
    # 参数关系：event 表示当前运行事件。
    # 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
    def encode(event: dict) -> dict[str, str]:
        event = _public_stream_event(event)
        # 变量说明：encoded 表示当前步骤使用的 encoded 值。
        encoded: dict[str, str] = {
            "event": str(event.get("type") or "message"),
            "data": json.dumps(event, ensure_ascii=False, separators=(",", ":")),
        }
        # 变量说明：event_id 表示event 对象的唯一标识。
        event_id = event.get("event_id")
        if event_id is not None:
            encoded["id"] = str(event_id)
        return encoded

    # 函数职责：异步完成 events 对应的业务处理。
    # 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
    async def events():  # type: ignore[no-untyped-def]
        try:
            yield encode(initial_state)
            for event in replay:
                yield encode(event)
                if _stream_event_is_terminal(event):
                    return
            if initial_state["terminal"]:
                return
            while not await request.is_disconnected():
                # 变量说明：event 表示当前运行事件。
                event = await subscription.queue.get()
                yield encode(event)
                if _stream_event_is_terminal(event):
                    return
        finally:
            run_stream_broker.unsubscribe(subscription)

    return EventSourceResponse(
        events(),
        ping=15,
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# 函数职责：完成 session_context 对应的业务处理。
# 参数关系：session_id 表示所属会话标识；db 表示当前数据库会话。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
@router.get("/sessions/{session_id}/context", response_model=SessionContextRead)
def session_context(session_id: str, db: OrmSession = Depends(get_db)) -> SessionContextRead:
    # 变量说明：chat_session 表示当前步骤使用的 chat_session 值。
    chat_session = db.get(Session, session_id)
    if chat_session is None:
        raise HTTPException(status_code=404, detail="Session not found")
    _prepare_session_history(db, chat_session)
    db.commit()
    # 变量说明：used_tokens 表示当前流程使用的 used_tokens 集合。
    used_tokens = max(0, min(int(chat_session.context_tokens or 0), settings.context_limit_tokens))
    # 变量说明：latest_compaction 表示当前步骤使用的 latest_compaction 值。
    latest_compaction = db.scalar(
        select(ConversationCompaction)
        .where(ConversationCompaction.session_id == session_id)
        .order_by(ConversationCompaction.source_sequence.desc(), ConversationCompaction.created_at.desc())
    )
    return SessionContextRead(
        active_context_tokens=used_tokens,
        # Total scope 与 Codex 的 Total 模式一致：压缩判断使用当前活动上下文本身，
        # 不是“距离阈值还剩多少”这种反向数值。
        auto_compact_scope_tokens=used_tokens,
        auto_compact_scope_limit=settings.compact_threshold_tokens,
        full_context_window_limit=settings.context_limit_tokens,
        base_window_tokens_remaining=max(0, settings.context_limit_tokens - used_tokens),
        token_limit_reached=used_tokens >= settings.compact_threshold_tokens,
        used_tokens=used_tokens,
        limit_tokens=settings.context_limit_tokens,
        compact_threshold_tokens=settings.compact_threshold_tokens,
        percent=round((used_tokens / settings.context_limit_tokens) * 100, 2),
        last_compaction_at=latest_compaction.created_at if latest_compaction is not None else None,
    )


# 函数职责：异步完成 stop_run 对应的业务处理。
# 参数关系：run_id 表示当前运行标识；payload 表示跨层传递的数据载荷；db 表示当前数据库会话。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
@router.post("/runs/{run_id}/stop", response_model=RunRead)
async def stop_run(
    run_id: str,
    payload: StopRunRequest | None = None,
    db: OrmSession = Depends(get_db),
) -> Run:
    """Interrupt a running model/tool loop at the user's request.

    The coordinator commits the terminal stop marker before cancelling its
    asyncio task.  Repeating the request is safe: an already terminal run is
    returned unchanged and a completed/failed run is never rewritten as an
    interruption.
    """

    # 变量说明：run 表示当前步骤使用的 run 值。
    run = coordinator.stop(run_id, db=db, reason=(payload.reason if payload else "user_interrupted"))
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found")
    return run


# 函数职责：异步完成 decide_and_resume 对应的业务处理。
# 参数关系：approval_id 表示approval 对象的唯一标识；payload 表示跨层传递的数据载荷；db 表示当前数据库会话。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
@router.post("/approvals/{approval_id}/decide", response_model=ApprovalRead)
async def decide_and_resume(
    approval_id: str, payload: ApprovalRuntimeDecision, db: OrmSession = Depends(get_db)
) -> Approval:
    # 变量说明：approval 表示当前步骤使用的 approval 值。
    approval = db.get(Approval, approval_id)
    if approval is None:
        raise HTTPException(status_code=404, detail="Approval not found")
    if approval.status != "pending":
        raise HTTPException(status_code=409, detail="该审批已经处理")
    # 变量说明：run 表示当前步骤使用的 run 值。
    run = db.get(Run, approval.run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found")
    if run.status != "awaiting_approval":
        raise HTTPException(status_code=409, detail="该运行当前不在等待审批状态")
    # 变量说明：snapshot 表示当前步骤使用的 snapshot 值。
    snapshot = db.scalar(
        select(RunEvent)
        .where(RunEvent.run_id == run.id, RunEvent.event_type == "runtime_snapshot")
        .order_by(RunEvent.sequence.desc(), RunEvent.id.desc())
    )
    # 变量说明：snapshot_payload 表示当前步骤使用的 snapshot_payload 值。
    snapshot_payload = snapshot.payload if snapshot is not None and isinstance(snapshot.payload, dict) else {}
    # 变量说明：pending 表示当前步骤使用的 pending 值。
    pending = snapshot_payload.get("pending_approval")
    if snapshot_payload.get("status") != "awaiting_approval" or not approval_matches_pending(approval, pending):
        raise HTTPException(status_code=409, detail="审批记录与最新运行快照不匹配，已拒绝继续执行")

    # 变量说明：original_approval_reason 表示当前步骤使用的 original_approval_reason 值。
    original_approval_reason = approval.reason
    # 变量说明：decided_at 表示decided_at 对应的时间信息。
    decided_at = datetime.now(timezone.utc)
    # 变量说明：approval_status 表示当前流程使用的 approval_status 集合。
    approval_status = "approved" if payload.decision == "approve" else "rejected"
    # 变量说明：parent_bridge_event 表示当前步骤使用的 parent_bridge_event 值。
    parent_bridge_event: dict[str, object] | None = None
    # 变量说明：approval_update 表示当前步骤使用的 approval_update 值。
    approval_update = db.execute(
        update(Approval)
        .where(Approval.id == approval.id, Approval.status == "pending")
        .values(status=approval_status, reason=payload.reason, decided_at=decided_at)
    )
    if approval_update.rowcount != 1:
        db.rollback()
        raise HTTPException(status_code=409, detail="该审批已被其他请求处理")
    if payload.decision == "reject":
        # 变量说明：run_values 表示当前流程使用的 run_values 集合。
        run_values = {
            "status": "stopped",
            "stop_reason": "approval_rejected",
            "error_code": "approval_rejected",
            "error_message": "所需操作没有获得批准，任务已停止。",
            "finished_at": decided_at,
        }
        append_run_event(
            db,
            run_id=run.id,
            event_type="approval_rejected",
            payload={"approval_id": approval.id},
        )
    else:
        # 变量说明：run_values 表示当前流程使用的 run_values 集合。
        run_values = {"status": "received", "finished_at": None}
    # 变量说明：run_update 表示当前步骤使用的 run_update 值。
    run_update = db.execute(
        update(Run)
        .where(Run.id == run.id, Run.status == "awaiting_approval")
        .values(**run_values)
    )
    if run_update.rowcount != 1:
        db.rollback()
        raise HTTPException(status_code=409, detail="运行状态已变更，未执行审批决定")
    if payload.decision == "reject":
        # A delegated child is linked to the parent session for approval
        # visibility.  Rejection must settle the child task in this same
        # transaction; otherwise the delegation record would remain falsely active.
        db.refresh(run)
        # 变量说明：parent_bridge_event 表示当前步骤使用的 parent_bridge_event 值。
        parent_bridge_event = coordinator.reconcile_delegated_child_terminal(
            db,
            run,
            status="stopped",
            stop_reason="approval_rejected",
            error=payload.reason or "Child approval was rejected",
            error_code="approval_rejected",
        )
        persist_terminal_response(
            db,
            run,
            error_code="approval_rejected",
            error_message=run.error_message,
        )
    db.commit()
    # 变量说明：approval 表示当前步骤使用的 approval 值。
    approval = db.get(Approval, approval_id)
    if approval is None:
        raise HTTPException(status_code=404, detail="Approval not found")
    if payload.decision == "approve":
        if not coordinator.launch(run.id, resume=True):
            db.execute(
                update(Approval)
                .where(Approval.id == approval.id, Approval.status == "approved")
                .values(status="pending", reason=original_approval_reason, decided_at=None)
            )
            db.execute(
                update(Run)
                .where(Run.id == run.id, Run.status == "received")
                .values(status="awaiting_approval")
            )
            db.commit()
            raise HTTPException(status_code=503, detail="续跑任务暂时无法启动，请重试审批")
    else:
        run_stream_broker.publish(run.id, {
            "type": "run_stopped",
            "code": "approval_rejected",
            "reason": payload.reason or "approval rejected",
        })
        if parent_bridge_event is not None and parent_bridge_event.get("parent_run_id"):
            run_stream_broker.publish(str(parent_bridge_event["parent_run_id"]), parent_bridge_event)
        coordinator.launch_parent_continuation_if_queued(parent_bridge_event)
    return approval
