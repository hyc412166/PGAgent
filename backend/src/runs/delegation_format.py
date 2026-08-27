"""Delegation catalog and structured result formatting."""

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
    CompletionDecision,
    RunOutcome,
    RuntimeConfig,
    decide_deterministic_completion,
    normalize_usage,
)
from src.context import ContextManager, FilesystemArtifactStore
from src.context.window import message_tokens
from src.context.assembly import COMPACTION_SCHEMA, CONTINUATION_PREFIX
from src.tools import create_default_registry
from src.tools.registry import TOOL_SCHEMAS
from src.tools.types import ToolResult

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


_DELEGATE_OUTPUT_LIMIT = 16_000
_DELEGATE_MESSAGE_LIMIT = 20_000
_DELEGATE_AGENT_CATALOG_LIMIT = 40
def _single_line(value: object, *, limit: int) -> str:
    """Render user-configured metadata safely inside a system capability hint."""

    return " ".join(str(value or "").replace("\x00", "").split())[:limit]


def _active_child_agents(db: Any) -> list[Agent]:
    """Return only explicitly user-created, currently enabled delegate targets."""

    return list(db.scalars(
        select(Agent)
        .where(
            Agent.id != DEFAULT_AGENT_ID,
            Agent.is_default.is_(False),
            Agent.enabled.is_(True),
        )
        .order_by(Agent.updated_at.desc(), Agent.id.asc())
    ))


def _delegate_catalog_prompt(db: Any, run: Run) -> str:
    """Make the main runtime's valid delegate IDs visible to the model.

    The main coordinator stays system-owned and prompt-free as a profile. This
    is dynamic tool metadata, not a user-authored replacement system prompt.
    It deliberately exposes only names/descriptions/IDs—not child prompts,
    credentials, workspaces, or capability internals.
    """

    children = _active_child_agents(db)[:_DELEGATE_AGENT_CATALOG_LIMIT]
    if not children:
        return ""
    lines = [
        "可委派的子 Agent（仅在任务确实较复杂、专业，或用户明确要求时使用 task 工具）：",
        "- 单个子任务使用 task + agent_id；批量任务使用 tasks 数组中的稳定 id/depends_on 声明依赖，无依赖节点会并行启动。",
        "- 每项任务都必须使用下列精确 agent_id；子 Agent 的实际权限和工具会由系统再次校验。",
    ]
    for child in children:
        name = _single_line(child.name, limit=120) or "未命名子 Agent"
        description = _single_line(child.description, limit=300)
        suffix = f"：{description}" if description else ""
        lines.append(f"- {child.id} | {name}{suffix}")
    if len(children) >= _DELEGATE_AGENT_CATALOG_LIMIT:
        lines.append("- 列表已截断；如未找到匹配子 Agent，请直接完成可安全完成的部分或向用户说明。")
    team = db.scalar(select(CollaborationTeam).where(
        CollaborationTeam.parent_run_id == run.id
    ))
    if team is None and run.task_id:
        team = db.scalar(
            select(CollaborationTeam)
            .where(
                CollaborationTeam.task_id == run.task_id,
                CollaborationTeam.status == "active",
            )
            .order_by(CollaborationTeam.updated_at.desc(), CollaborationTeam.id.desc())
        )
        if team is not None:
            team.parent_run_id = run.id
    if team is not None:
        workers = list(db.scalars(
            select(TeammateWorker)
            .where(TeammateWorker.team_id == team.id)
            .order_by(TeammateWorker.created_at.asc(), TeammateWorker.id.asc())
        ))
        if workers:
            lines.append("持久化队友（task 的 agent_id 可直接使用 teammate id，系统会恢复其收件箱和历史）：")
            for worker in workers:
                lines.append(
                    f"- teammate={worker.id} | {worker.name} | role={worker.role} | "
                    f"status={worker.status} | workspace={worker.workspace_mode}"
                )
    return "\n".join(lines)


def _model_id_for_delegate(
    connection: ModelConnection,
    *,
    preferred: str | None,
    inherited_model_id: str | None,
    may_inherit_model: bool,
) -> str | None:
    """Resolve a child model without accidentally crossing connections."""

    selected = str(preferred or "").strip()
    if selected:
        return selected[:255]
    selected = str(connection.default_model or "").strip()
    if selected:
        return selected[:255]
    candidates = [*(connection.discovered_models or []), *(connection.manual_models or [])]
    for candidate in candidates:
        normalized = str(candidate or "").strip()
        if normalized:
            return normalized[:255]
    if may_inherit_model:
        selected = str(inherited_model_id or "").strip()
        if selected:
            return selected[:255]
    return None


def _delegate_result_content(payload: dict[str, Any]) -> str:
    """Bound the child response before it becomes a parent tool observation."""

    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str)
    if len(encoded) <= _DELEGATE_MESSAGE_LIMIT:
        return encoded
    # Keep the structured envelope valid and signal truncation explicitly.
    compact = dict(payload)
    output = str(compact.get("output") or "")
    compact["output"] = output[: max(0, _DELEGATE_OUTPUT_LIMIT // 2)]
    compact["output_truncated"] = True
    return json.dumps(compact, ensure_ascii=False, separators=(",", ":"), default=str)[:_DELEGATE_MESSAGE_LIMIT]
