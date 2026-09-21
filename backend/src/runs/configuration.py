"""Frozen model, tool, skill, and workspace bindings for a run."""
# 文件职责：负责运行创建、恢复、流式传输和生命周期中的 configuration 子模块。
# 逻辑关系：上层通过 runs/configuration.py 使用本模块；本模块把处理结果交给同领域服务、持久化层或 API 响应层。

from __future__ import annotations

import asyncio
import hashlib
import json
import threading
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from sqlalchemy import case, func, or_, select, text, update
from sqlalchemy.exc import IntegrityError

from src.persistence import database as database_module
from src.config import settings
from src.persistence.database import (
    Agent,
    Approval,
    BackgroundJob,
    ChatMessage,
    CollaborationEvent,
    CollaborationMessage,
    CollaborationTeam,
    ConversationTurn,
    ConversationCompaction,
    Artifact,
    DelegatedTask,
    DurableTask,
    DEFAULT_AGENT_ID,
    DEFAULT_WORKSPACE_ID,
    ModelConnection,
    Memory,
    MemoryJob,
    PlanStep,
    Run,
    RunEvent,
    Session,
    Skill,
    TeammateWorker,
    UsageRecord,
    Workspace,
)
from src.agent import (
    AgentRuntime,
    RunOutcome,
    RuntimeConfig,
    normalize_usage,
)
from src.tools.registry import TOOL_SCHEMAS

from src.tasks import background as background_job_service
from src.model.gateway import ModelConfigurationError, ProviderConfig, build_model_call
from src.artifacts.storage import ArtifactToolStore
from src.tasks.background import BackgroundJobToolStore
from src.memory.service import (
    MemoryToolStore,
    ensure_user_memory_snapshot,
    extraction_prompt,
    memory_payload,
    parse_extraction_response,
    recall_memories,
    refresh_memory_markdown_projection,
    render_memory_snapshot,
    store_memory,
    visible_memory_query,
)
from src.context.instructions import load_instruction_chain, render_workspace_rules
from src.runs.stream import run_stream_broker
from src.tasks.state import (
    recovery_prompt,
    sync_todos_for_run,
    task_checkpoint_for_run,
    todo_state_for_run,
    transition_run_task,
)
from src.tasks.graph import TaskGraphToolStore, ready_steps, refresh_task_state, settle_step, upsert_delegated_graph
from src.agents.collaboration import TeamToolStore, teammate_context
from src.sessions.delivery import (
    classify_error_details,
    classify_exception,
    ensure_run_turn,
    is_terminal_delivery,
    persist_terminal_response,
    public_error_message,
    sync_turn_progress,
    terminal_error_code,
)


# 函数职责：完成 explicit_setting 对应的业务处理。
# 参数关系：values 表示当前流程使用的 values 集合。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def _explicit_setting(*values: str | None) -> str | None:
    for value in values:
        # 变量说明：normalized 表示当前步骤使用的 normalized 值。
        normalized = (value or "").strip()
        if normalized and normalized != "auto":
            return normalized
    return None


def _enabled_connection_models(connection: ModelConnection) -> list[str]:
    """按连接目录顺序返回可用于新 Run 的模型。"""

    disabled = set(connection.disabled_models or [])
    catalog = dict.fromkeys([
        *([connection.default_model] if connection.default_model else []),
        *(connection.discovered_models or []),
        *(connection.manual_models or []),
    ])
    return [model for model in catalog if model not in disabled]


# 函数职责：完成 fallback_connection 对应的业务处理。
# 参数关系：db 表示当前数据库会话；provider 表示模型供应商。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def _fallback_connection(db: Any, *, provider: str | None = None) -> ModelConnection | None:
    # 变量说明：query 表示当前步骤使用的 query 值。
    query = select(ModelConnection).where(ModelConnection.enabled.is_(True))
    if provider:
        # 变量说明：query 表示当前步骤使用的 query 值。
        query = query.where(ModelConnection.provider == provider)
    # 变量说明：query 表示当前步骤使用的 query 值。
    query = query.order_by(
        case(
            (ModelConnection.status.in_(("connected", "healthy", "online")), 0),
            (ModelConnection.last_checked_at.is_not(None), 1),
            else_=2,
        ),
        ModelConnection.last_checked_at.desc(),
        ModelConnection.created_at.asc(),
    )
    # 连接开启不等于存在可用模型；继续寻找下一条可运行连接。
    return next((connection for connection in db.scalars(query) if _enabled_connection_models(connection)), None)


# 函数职责：完成 effective_connection 对应的业务处理。
# 参数关系：db 表示当前数据库会话；session 表示当前步骤使用的 session 值；agent 表示当前步骤使用的 agent 值。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def _effective_connection(db: Any, session: Session | None, agent: Agent) -> ModelConnection | None:
    # 变量说明：candidate_ids 表示candidate 对象标识集合。
    candidate_ids = [session.model_connection_id if session else None, agent.model_connection_id]
    for connection_id in dict.fromkeys(item for item in candidate_ids if item):
        # 变量说明：connection 表示当前步骤使用的 connection 值。
        connection = db.get(ModelConnection, connection_id)
        if connection is not None and connection.enabled and _enabled_connection_models(connection):
            return connection
    return _fallback_connection(db)


# 函数职责：完成 allowed_runtime_tool_names 对应的业务处理。
# 参数关系：tool_ids 表示tool 对象标识集合。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def _allowed_runtime_tool_names(tool_ids: list[str]) -> list[str]:
    """Map persisted catalog IDs to only tools this runtime actually implements."""

    # 变量说明：selected 表示当前步骤使用的 selected 值。
    selected: list[str] = []
    # 变量说明：seen 表示当前步骤使用的 seen 值。
    seen: set[str] = set()
    for raw_id in tool_ids:
        # 变量说明：tool_name 表示当前步骤使用的 tool_name 值。
        tool_name = str(raw_id or "").strip()
        if tool_name in TOOL_SCHEMAS and tool_name not in seen:
            selected.append(tool_name)
            seen.add(tool_name)
    return selected


# 函数职责：完成 read_selected_skill_instructions 对应的业务处理。
# 参数关系：db 表示当前数据库会话；skill_ids 表示skill 对象标识集合。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def _read_selected_skill_instructions(db: Any, skill_ids: list[str]) -> list[dict[str, str]]:
    """Read only selected, PGAgent-managed ``SKILL.md`` text into a run binding.

    A Skill remains inert: this helper neither imports Python nor executes a
    script.  It skips a missing/tampered package instead of sending a path or
    filesystem exception to the model.
    """

    # 变量说明：requested 表示当前步骤使用的 requested 值。
    requested = [str(item).strip() for item in skill_ids if str(item).strip()]
    if not requested:
        return []
    # 变量说明：rows 表示当前流程使用的 rows 集合。
    rows = list(db.scalars(select(Skill).where(Skill.id.in_(requested), Skill.enabled.is_(True))))
    # 变量说明：by_id 表示by 对象的唯一标识。
    by_id = {item.id: item for item in rows}
    # 变量说明：managed_root 表示当前步骤使用的 managed_root 值。
    managed_root = (settings.data_dir / "skills").resolve()
    # 变量说明：items 表示待处理的元素集合。
    items: list[dict[str, str]] = []
    for skill_id in requested:
        # 变量说明：skill 表示当前步骤使用的 skill 值。
        skill = by_id.get(skill_id)
        if skill is None:
            continue
        try:
            # 变量说明：root 表示处理范围的根目录。
            root = Path(skill.root_path).resolve(strict=True)
            root.relative_to(managed_root)
            # 变量说明：skill_file 表示当前步骤使用的 skill_file 值。
            skill_file = (root / "SKILL.md").resolve(strict=True)
            skill_file.relative_to(root)
            if not skill_file.is_file() or skill_file.stat().st_size > 2_000_000:
                continue
            # 变量说明：content 表示待处理或返回的正文内容。
            content = skill_file.read_text(encoding="utf-8")[:40_000]
        except (OSError, UnicodeError, ValueError):
            continue
        if not content.strip():
            continue
        items.append(
            {
                "id": skill.id,
                "slug": skill.slug,
                "name": skill.name,
                "description": skill.description[:1_000],
                "path": str(skill_file),
                "resource_root": str(root),
                "content": content,
            }
        )
    return items
