"""Frozen model, tool, skill, and workspace bindings for a run."""

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


def _explicit_setting(*values: str | None) -> str | None:
    for value in values:
        normalized = (value or "").strip()
        if normalized and normalized != "auto":
            return normalized
    return None


def _fallback_connection(db: Any, *, provider: str | None = None) -> ModelConnection | None:
    query = select(ModelConnection).where(ModelConnection.enabled.is_(True))
    if provider:
        query = query.where(ModelConnection.provider == provider)
    query = query.order_by(
        case(
            (ModelConnection.status.in_(("connected", "healthy", "online")), 0),
            (ModelConnection.last_checked_at.is_not(None), 1),
            else_=2,
        ),
        ModelConnection.last_checked_at.desc(),
        ModelConnection.created_at.asc(),
    )
    return db.scalar(query)


def _effective_connection(db: Any, session: Session | None, agent: Agent) -> ModelConnection | None:
    candidate_ids = [session.model_connection_id if session else None, agent.model_connection_id]
    for connection_id in dict.fromkeys(item for item in candidate_ids if item):
        connection = db.get(ModelConnection, connection_id)
        if connection is not None and connection.enabled:
            return connection
    return _fallback_connection(db)


def _allowed_runtime_tool_names(tool_ids: list[str]) -> list[str]:
    """Map persisted catalog IDs to only tools this runtime actually implements."""

    selected: list[str] = []
    seen: set[str] = set()
    for raw_id in tool_ids:
        tool_name = str(raw_id or "").strip()
        if tool_name in TOOL_SCHEMAS and tool_name not in seen:
            selected.append(tool_name)
            seen.add(tool_name)
    return selected


def _read_selected_skill_instructions(db: Any, skill_ids: list[str]) -> list[dict[str, str]]:
    """Read only selected, PGAgent-managed ``SKILL.md`` text into a run binding.

    A Skill remains inert: this helper neither imports Python nor executes a
    script.  It skips a missing/tampered package instead of sending a path or
    filesystem exception to the model.
    """

    requested = [str(item).strip() for item in skill_ids if str(item).strip()]
    if not requested:
        return []
    rows = list(db.scalars(select(Skill).where(Skill.id.in_(requested), Skill.enabled.is_(True))))
    by_id = {item.id: item for item in rows}
    managed_root = (settings.data_dir / "skills").resolve()
    items: list[dict[str, str]] = []
    for skill_id in requested:
        skill = by_id.get(skill_id)
        if skill is None:
            continue
        try:
            root = Path(skill.root_path).resolve(strict=True)
            root.relative_to(managed_root)
            skill_file = (root / "SKILL.md").resolve(strict=True)
            skill_file.relative_to(root)
            if not skill_file.is_file() or skill_file.stat().st_size > 2_000_000:
                continue
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
                "content": content,
            }
        )
    return items
