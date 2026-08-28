"""Durable memory queries, citations, usage feedback, and index loading."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError

from src.config import settings
from src.persistence import database as database_module
from src.persistence.database import Memory, MemoryCitation, MemoryRollout, MemorySkill, Run


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def memory_directory() -> Path:
    database_path = database_module.engine.url.database
    if (
        database_module.engine.url.get_backend_name() == "sqlite"
        and database_path
        and database_path != ":memory:"
    ):
        return Path(database_path).resolve().parent / "memories"
    return settings.data_dir / "memories"


def load_memory_index(*, workspace_id: str | None, session_id: str | None = None) -> str:
    """Render every memory route visible to one frozen run."""

    clauses = [Memory.scope == "global"]
    if workspace_id:
        clauses.append((Memory.scope == "workspace") & (Memory.scope_id == workspace_id))
    if session_id:
        clauses.append((Memory.scope == "session") & (Memory.scope_id == session_id))
    with database_module.SessionLocal() as db:
        memories = list(db.scalars(select(Memory).where(
            Memory.status == "active", or_(*clauses)
        ).order_by(Memory.scope.asc(), Memory.name.asc(), Memory.id.asc())))
        skills = list(db.scalars(select(MemorySkill).where(
            MemorySkill.status == "active",
            MemorySkill.workspace_id == workspace_id,
        ).order_by(MemorySkill.name.asc(), MemorySkill.id.asc()))) if workspace_id else []
    if not memories and not skills:
        return "No active persistent memories are indexed."
    lines = ["# Visible persistent memory routes", ""]
    for memory in memories:
        description = " ".join((memory.description or memory.content).split())[:600]
        tags = ", ".join(str(value) for value in memory.tags or []) or "none"
        lines.extend([
            f"## {memory.name}",
            f"- id: `{memory.id}`",
            f"- scope: `{memory.scope}:{memory.scope_id or ''}`",
            f"- keywords: {tags}",
            f"- search: `MemorySearch {memory.name}`",
            f"- route: {description}",
            "",
        ])
    for skill in skills:
        keywords = ", ".join(str(value) for value in skill.keywords or []) or "none"
        lines.extend([
            f"## Skill: {skill.name}",
            f"- id: `{skill.id}`",
            f"- scope: `workspace:{skill.workspace_id}`",
            f"- keywords: {keywords}",
            f"- search: `MemoryRead skill_id={skill.id}`",
            f"- route: {' '.join(skill.description.split())[:600]}",
            "",
        ])
    return "\n".join(lines)


def rollout_payload(row: MemoryRollout) -> dict[str, Any]:
    return {
        "id": row.id,
        "source_session_id": row.source_session_id,
        "source_turn_id": row.source_turn_id,
        "workspace_id": row.workspace_id,
        "source_end_sequence": row.source_end_sequence,
        "cwd": row.cwd,
        "rollout_slug": row.rollout_slug,
        "rollout_summary": row.rollout_summary,
        "raw_memories": list(row.raw_memories or []),
        "keywords": list(row.keywords or []),
        "task_groups": list(row.task_groups or []),
        "outcome": row.outcome,
        "usage_count": int(row.usage_count or 0),
        "last_usage_at": row.last_usage_at.isoformat() if row.last_usage_at else None,
    }


def record_memory_citations(
    db: Any,
    *,
    run: Run,
    citation: Mapping[str, Any] | None,
) -> list[MemoryCitation]:
    """Persist validated citation targets once and apply their usage feedback."""

    if run.status != "completed" or not citation:
        return []
    note = str(citation.get("note") or "").strip()[:500]
    targets = [
        *(('memory', str(value).strip()) for value in citation.get("memory_ids", []) if str(value).strip()),
        *(('rollout', str(value).strip()) for value in citation.get("rollout_ids", []) if str(value).strip()),
        *(('skill', str(value).strip()) for value in citation.get("skill_ids", []) if str(value).strip()),
    ]
    stored: list[MemoryCitation] = []
    now = _utcnow()
    credited_rollouts: set[str] = set()
    for existing in db.scalars(select(MemoryCitation).where(MemoryCitation.run_id == run.id)):
        if existing.target_type == "rollout":
            credited_rollouts.add(existing.target_id)
        elif existing.target_type == "memory":
            existing_memory = db.get(Memory, existing.target_id)
            if existing_memory is not None and isinstance(existing_memory.extra, Mapping):
                credited_rollouts.update(str(value) for value in existing_memory.extra.get("source_rollout_ids", []))
    for target_type, target_id in dict.fromkeys(targets):
        model = {"memory": Memory, "rollout": MemoryRollout, "skill": MemorySkill}[target_type]
        target = db.get(model, target_id)
        if target is None:
            continue
        if target_type == "memory" and not (
            target.status == "active"
            and (
                target.scope == "global"
                or (target.scope == "workspace" and target.scope_id == run.workspace_id)
                or (target.scope == "session" and target.scope_id == run.session_id)
            )
        ):
            continue
        if target_type == "rollout" and (
            target.status not in {"active", "consolidating"}
            or target.workspace_id != run.workspace_id
        ):
            continue
        if target_type == "skill" and (
            target.status != "active" or target.workspace_id != run.workspace_id
        ):
            continue
        item = MemoryCitation(
            run_id=run.id,
            turn_id=run.turn_id,
            target_type=target_type,
            target_id=target_id,
            note=note,
        )
        try:
            with db.begin_nested():
                db.add(item)
                db.flush()
        except IntegrityError:
            continue
        if target_type != "rollout" or target_id not in credited_rollouts:
            target.usage_count = int(target.usage_count or 0) + 1
            target.last_usage_at = now
        stored.append(item)
        if target_type == "rollout":
            credited_rollouts.add(target_id)
        else:
            source_ids = (
                target.extra.get("source_rollout_ids", [])
                if target_type == "memory" and isinstance(target.extra, Mapping)
                else list(target.source_rollout_ids or [])
            )
            for rollout_id in source_ids:
                if str(rollout_id) in credited_rollouts:
                    continue
                rollout = db.get(MemoryRollout, str(rollout_id))
                if rollout is not None:
                    rollout.usage_count = int(rollout.usage_count or 0) + 1
                    rollout.last_usage_at = now
                    credited_rollouts.add(str(rollout_id))
    return stored


def visible_rollout(db: Any, rollout_id: str, *, workspace_id: str | None) -> MemoryRollout | None:
    query = select(MemoryRollout).where(MemoryRollout.id == rollout_id)
    if workspace_id:
        query = query.where(MemoryRollout.workspace_id == workspace_id)
    return db.scalar(query)
