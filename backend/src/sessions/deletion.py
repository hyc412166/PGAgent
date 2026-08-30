"""Shared, complete deletion lifecycle for one or more conversations."""

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


class SessionDeletionConflict(RuntimeError):
    """The conversation cannot be deleted while owned work is active."""


@dataclass(frozen=True)
class SessionDeletionEffects:
    session_ids: tuple[str, ...]
    artifact_directories: tuple[Path, ...]


def stage_session_deletions(
    db: Session,
    conversations: list[ChatSession],
) -> SessionDeletionEffects:
    """Validate and stage complete conversation deletion without committing."""

    for conversation in conversations:
        _ensure_deletable(db, conversation.id)

    for conversation in conversations:
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

    artifact_root = (resources_api.settings.data_dir / "artifacts").resolve()
    artifact_directories: list[Path] = []
    session_ids: list[str] = []
    for conversation in conversations:
        session_id = conversation.id
        session_ids.append(session_id)
        artifact_directory = (artifact_root / session_id).resolve()
        if artifact_directory.parent == artifact_root:
            artifact_directories.append(artifact_directory)

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


def finalize_session_deletions(effects: SessionDeletionEffects) -> None:
    """Release runtime state and stored payloads after the database commit."""

    for session_id in effects.session_ids:
        coordinator.close_mcp_session(session_id)
    for artifact_directory in effects.artifact_directories:
        if artifact_directory.is_dir():
            shutil.rmtree(artifact_directory)
    refresh_memory_markdown_projection()


def _ensure_deletable(db: Session, session_id: str) -> None:
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
