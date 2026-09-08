"""Durable memory queries, citations, usage feedback, and index loading."""
# 文件职责：负责长期记忆提取、合并、检索与偏好中的 repository 子模块。
# 逻辑关系：上层通过 memory/repository.py 使用本模块；本模块把处理结果交给同领域服务、持久化层或 API 响应层。

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError

from src.config import settings
from src.persistence import database as database_module
from src.persistence.database import Memory, MemoryCitation, MemoryRollout, MemorySkill, Run


# 函数职责：完成 utcnow 对应的业务处理。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# 函数职责：完成 memory_directory 对应的业务处理。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def memory_directory() -> Path:
    # 变量说明：database_path 表示database_path 对应的文件系统位置。
    database_path = database_module.engine.url.database
    if (
        database_module.engine.url.get_backend_name() == "sqlite"
        and database_path
        and database_path != ":memory:"
    ):
        return Path(database_path).resolve().parent / "memories"
    return settings.data_dir / "memories"


# 函数职责：加载 memory_index 对应的数据或流程。
# 参数关系：workspace_id 表示工作区标识；session_id 表示所属会话标识。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def load_memory_index(*, workspace_id: str | None, session_id: str | None = None) -> str:
    """Render every memory route visible to one frozen run."""

    # 变量说明：clauses 表示当前流程使用的 clauses 集合。
    clauses = [Memory.scope == "global"]
    if workspace_id:
        clauses.append((Memory.scope == "workspace") & (Memory.scope_id == workspace_id))
    if session_id:
        clauses.append((Memory.scope == "session") & (Memory.scope_id == session_id))
    with database_module.SessionLocal() as db:
        # 变量说明：memories 表示当前流程使用的 memories 集合。
        memories = list(db.scalars(select(Memory).where(
            Memory.status == "active", or_(*clauses)
        ).order_by(Memory.scope.asc(), Memory.name.asc(), Memory.id.asc())))
        # 变量说明：skills 表示当前流程使用的 skills 集合。
        skills = list(db.scalars(select(MemorySkill).where(
            MemorySkill.status == "active",
            MemorySkill.workspace_id == workspace_id,
        ).order_by(MemorySkill.name.asc(), MemorySkill.id.asc()))) if workspace_id else []
    if not memories and not skills:
        return "No active persistent memories are indexed."
    # 变量说明：lines 表示当前流程使用的 lines 集合。
    lines = ["# Visible persistent memory routes", ""]
    for memory in memories:
        # 变量说明：description 表示当前步骤使用的 description 值。
        description = " ".join((memory.description or memory.content).split())[:600]
        # 变量说明：tags 表示当前流程使用的 tags 集合。
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
        # 变量说明：keywords 表示当前流程使用的 keywords 集合。
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


# 函数职责：完成 rollout_payload 对应的业务处理。
# 参数关系：row 表示当前步骤使用的 row 值。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
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


# 函数职责：记录 memory_citations 对应的数据或流程。
# 参数关系：db 表示当前数据库会话；run 表示当前步骤使用的 run 值；citation 表示当前步骤使用的 citation 值。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def record_memory_citations(
    db: Any,
    *,
    run: Run,
    citation: Mapping[str, Any] | None,
) -> list[MemoryCitation]:
    """Persist validated citation targets once and apply their usage feedback."""

    if run.status != "completed" or not citation:
        return []
    # 变量说明：note 表示当前步骤使用的 note 值。
    note = str(citation.get("note") or "").strip()[:500]
    # 变量说明：targets 表示当前流程使用的 targets 集合。
    targets = [
        *(('memory', str(value).strip()) for value in citation.get("memory_ids", []) if str(value).strip()),
        *(('rollout', str(value).strip()) for value in citation.get("rollout_ids", []) if str(value).strip()),
        *(('skill', str(value).strip()) for value in citation.get("skill_ids", []) if str(value).strip()),
    ]
    # 变量说明：stored 表示当前步骤使用的 stored 值。
    stored: list[MemoryCitation] = []
    # 变量说明：now 表示当前时间。
    now = _utcnow()
    # 变量说明：credited_rollouts 表示当前流程使用的 credited_rollouts 集合。
    credited_rollouts: set[str] = set()
    for existing in db.scalars(select(MemoryCitation).where(MemoryCitation.run_id == run.id)):
        if existing.target_type == "rollout":
            credited_rollouts.add(existing.target_id)
        elif existing.target_type == "memory":
            # 变量说明：existing_memory 表示当前步骤使用的 existing_memory 值。
            existing_memory = db.get(Memory, existing.target_id)
            if existing_memory is not None and isinstance(existing_memory.extra, Mapping):
                credited_rollouts.update(str(value) for value in existing_memory.extra.get("source_rollout_ids", []))
    for target_type, target_id in dict.fromkeys(targets):
        # 变量说明：model 表示当前选择的模型。
        model = {"memory": Memory, "rollout": MemoryRollout, "skill": MemorySkill}[target_type]
        # 变量说明：target 表示当前步骤使用的 target 值。
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
        # 变量说明：item 表示当前步骤使用的 item 值。
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
            # 变量说明：usage_count 表示usage 的数量。
            target.usage_count = int(target.usage_count or 0) + 1
            # 变量说明：last_usage_at 表示last_usage_at 对应的时间信息。
            target.last_usage_at = now
        stored.append(item)
        if target_type == "rollout":
            credited_rollouts.add(target_id)
        else:
            # 变量说明：source_ids 表示source 对象标识集合。
            source_ids = (
                target.extra.get("source_rollout_ids", [])
                if target_type == "memory" and isinstance(target.extra, Mapping)
                else list(target.source_rollout_ids or [])
            )
            for rollout_id in source_ids:
                if str(rollout_id) in credited_rollouts:
                    continue
                # 变量说明：rollout 表示当前步骤使用的 rollout 值。
                rollout = db.get(MemoryRollout, str(rollout_id))
                if rollout is not None:
                    # 变量说明：usage_count 表示usage 的数量。
                    rollout.usage_count = int(rollout.usage_count or 0) + 1
                    # 变量说明：last_usage_at 表示last_usage_at 对应的时间信息。
                    rollout.last_usage_at = now
                    credited_rollouts.add(str(rollout_id))
    return stored


# 函数职责：完成 visible_rollout 对应的业务处理。
# 参数关系：db 表示当前数据库会话；rollout_id 表示rollout 对象的唯一标识；workspace_id 表示工作区标识。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def visible_rollout(db: Any, rollout_id: str, *, workspace_id: str | None) -> MemoryRollout | None:
    # 变量说明：query 表示当前步骤使用的 query 值。
    query = select(MemoryRollout).where(MemoryRollout.id == rollout_id)
    if workspace_id:
        # 变量说明：query 表示当前步骤使用的 query 值。
        query = query.where(MemoryRollout.workspace_id == workspace_id)
    return db.scalar(query)
