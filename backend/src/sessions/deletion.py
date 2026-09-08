"""Shared, complete deletion lifecycle for one or more conversations."""
# 文件职责：负责会话交付与清理中的 deletion 子模块。
# 逻辑关系：上层通过 sessions/deletion.py 使用本模块；本模块把处理结果交给同领域服务、持久化层或 API 响应层。

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import shutil

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from src.agents.collaboration import cleanup_session_worktrees
from src.memory.service import refresh_memory_markdown_projection
from src.persistence.database import (
    BackgroundJob,
    DelegatedTask,
    DraftLaunch,
    Memory,
    Run,
    Session as ChatSession,
    UsageRecord,
    Workspace,
)
from src.runs.service import STOPPABLE_STATUSES, coordinator


# 类职责：定义 SessionDeletionConflict 在本领域中的数据与行为。
# 继承关系：复用基类提供的契约，并向调用方暴露本类声明的字段和方法。
class SessionDeletionConflict(RuntimeError):
    """The conversation cannot be deleted while owned work is active."""


# 类职责：定义 SessionDeletionEffects 在本领域中的数据与行为。
@dataclass(frozen=True)
class SessionDeletionEffects:
    # 变量说明：session_ids 表示session 对象标识集合。
    session_ids: tuple[str, ...]
    # 变量说明：artifact_directories 表示当前流程使用的 artifact_directories 集合。
    artifact_directories: tuple[Path, ...]


# 函数职责：完成 stage_session_deletions 对应的业务处理。
# 参数关系：db 表示当前数据库会话；conversations 表示当前流程使用的 conversations 集合。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def stage_session_deletions(
    db: Session,
    conversations: list[ChatSession],
) -> SessionDeletionEffects:
    """Validate and stage complete conversation deletion without committing."""

    for conversation in conversations:
        _ensure_deletable(db, conversation.id)

    for conversation in conversations:
        # 变量说明：workspace 表示当前步骤使用的 workspace 值。
        workspace = db.get(Workspace, conversation.workspace_id)
        if workspace is None:
            continue
        try:
            cleanup_session_worktrees(db, conversation.id, workspace.root_path)
        except RuntimeError as exc:
            raise SessionDeletionConflict(
                f"Cannot delete conversation worktrees: {exc}"
            ) from exc

    from src.api import routes as resources_api

    # 变量说明：artifact_root 表示当前步骤使用的 artifact_root 值。
    artifact_root = (resources_api.settings.data_dir / "artifacts").resolve()
    # 变量说明：artifact_directories 表示当前流程使用的 artifact_directories 集合。
    artifact_directories: list[Path] = []
    # 变量说明：session_ids 表示session 对象标识集合。
    session_ids: list[str] = []
    for conversation in conversations:
        # 变量说明：session_id 表示所属会话标识。
        session_id = conversation.id
        session_ids.append(session_id)
        # 变量说明：artifact_directory 表示当前步骤使用的 artifact_directory 值。
        artifact_directory = (artifact_root / session_id).resolve()
        if artifact_directory.parent == artifact_root:
            artifact_directories.append(artifact_directory)

        # 变量说明：run_ids 表示run 对象标识集合。
        run_ids = set(db.scalars(select(Run.id).where(Run.session_id == session_id)))
        run_ids.update(
            value
            for value in db.scalars(
                select(DelegatedTask.child_run_id).where(
                    DelegatedTask.parent_session_id == session_id
                )
            )
            if value
        )
        db.execute(
            delete(UsageRecord).where(
                (UsageRecord.session_id == session_id)
                | (UsageRecord.run_id.in_(run_ids) if run_ids else False)
            )
        )
        if run_ids:
            db.execute(delete(Run).where(Run.id.in_(run_ids)))
        db.execute(delete(DraftLaunch).where(DraftLaunch.session_id == session_id))
        db.execute(delete(Memory).where(Memory.scope == "session", Memory.scope_id == session_id))
        db.delete(conversation)

    return SessionDeletionEffects(tuple(session_ids), tuple(artifact_directories))


# 函数职责：完成 finalize_session_deletions 对应的业务处理。
# 参数关系：effects 表示当前流程使用的 effects 集合。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def finalize_session_deletions(effects: SessionDeletionEffects) -> None:
    """Release runtime state and stored payloads after the database commit."""

    for session_id in effects.session_ids:
        coordinator.close_mcp_session(session_id)
    for artifact_directory in effects.artifact_directories:
        if artifact_directory.is_dir():
            shutil.rmtree(artifact_directory)
    refresh_memory_markdown_projection()


# 函数职责：确保 deletable 对应的数据或流程。
# 参数关系：db 表示当前数据库会话；session_id 表示所属会话标识。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def _ensure_deletable(db: Session, session_id: str) -> None:
    # 变量说明：active_background_jobs 表示当前流程使用的 active_background_jobs 集合。
    active_background_jobs = db.scalar(
        select(func.count(BackgroundJob.id)).where(
            BackgroundJob.session_id == session_id,
            BackgroundJob.status.in_({"queued", "running"}),
        )
    )
    if int(active_background_jobs or 0) > 0:
        raise SessionDeletionConflict(
            "Cannot delete a conversation while it has active background jobs"
        )
    # 变量说明：active_runs 表示当前流程使用的 active_runs 集合。
    active_runs = db.scalar(
        select(func.count(Run.id)).where(
            Run.session_id == session_id,
            Run.status.in_(STOPPABLE_STATUSES),
        )
    )
    if int(active_runs or 0) > 0:
        raise SessionDeletionConflict(
            "Cannot delete a conversation while it has an active run"
        )
