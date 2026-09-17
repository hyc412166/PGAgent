"""Delegation catalog and structured result formatting."""
# 文件职责：负责运行创建、恢复、流式传输和生命周期中的 delegation_format 子模块。
# 逻辑关系：上层通过 runs/delegation_format.py 使用本模块；本模块把处理结果交给同领域服务、持久化层或 API 响应层。

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


# 变量说明：_DELEGATE_OUTPUT_LIMIT 表示当前步骤使用的 _DELEGATE_OUTPUT_LIMIT 值。
_DELEGATE_OUTPUT_LIMIT = 16_000
# 变量说明：_DELEGATE_MESSAGE_LIMIT 表示当前步骤使用的 _DELEGATE_MESSAGE_LIMIT 值。
_DELEGATE_MESSAGE_LIMIT = 20_000
# 变量说明：_DELEGATE_AGENT_CATALOG_LIMIT 表示当前步骤使用的 _DELEGATE_AGENT_CATALOG_LIMIT 值。
_DELEGATE_AGENT_CATALOG_LIMIT = 40
# 函数职责：完成 single_line 对应的业务处理。
# 参数关系：value 表示当前字段或计算值；limit 表示当前步骤使用的 limit 值。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def _single_line(value: object, *, limit: int) -> str:
    """Render user-configured metadata safely inside a system capability hint."""

    return " ".join(str(value or "").replace("\x00", "").split())[:limit]


# 函数职责：完成 active_child_agents 对应的业务处理。
# 参数关系：db 表示当前数据库会话。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
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


# 函数职责：完成 delegate_catalog_prompt 对应的业务处理。
# 参数关系：db 表示当前数据库会话；run 表示当前步骤使用的 run 值。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def _delegate_catalog_prompt(db: Any, run: Run) -> str:
    """Make the main runtime's valid delegate IDs visible to the model.

    The main coordinator stays system-owned and prompt-free as a profile. This
    is dynamic tool metadata, not a user-authored replacement system prompt.
    It deliberately exposes only names/descriptions/IDs—not child prompts,
    credentials, workspaces, or capability internals.
    """

    # 变量说明：children 表示当前步骤使用的 children 值。
    children = _active_child_agents(db)[:_DELEGATE_AGENT_CATALOG_LIMIT]
    if not children:
        return ""
    # 变量说明：lines 表示当前流程使用的 lines 集合。
    lines = [
        "可委派的子 Agent（仅在任务确实较复杂、专业，或用户明确要求时使用 task 工具）：",
        "- 单个子任务使用 task + agent_id；批量任务使用 tasks 数组中的稳定 id/depends_on 声明依赖，无依赖节点会并行启动。",
        "- 每项任务都必须使用下列精确 agent_id；子 Agent 的实际权限和工具会由系统再次校验。",
        "- 你可以按任务通过 model_id/thinking_level 分配模型与思考强度；没有确定理由时省略它们，子 Agent 会继承你本轮的实际设置。",
    ]
    for child in children:
        # 变量说明：name 表示当前对象名称。
        name = _single_line(child.name, limit=120) or "未命名子 Agent"
        # 变量说明：description 表示当前步骤使用的 description 值。
        description = _single_line(child.description, limit=300)
        # 变量说明：suffix 表示当前步骤使用的 suffix 值。
        suffix = f"：{description}" if description else ""
        lines.append(f"- {child.id} | {name}{suffix}")
    if len(children) >= _DELEGATE_AGENT_CATALOG_LIMIT:
        lines.append("- 列表已截断；如未找到匹配子 Agent，请直接完成可安全完成的部分或向用户说明。")
    # 变量说明：team 表示当前步骤使用的 team 值。
    team = db.scalar(select(CollaborationTeam).where(
        CollaborationTeam.parent_run_id == run.id
    ))
    if team is None and run.task_id:
        # 变量说明：team 表示当前步骤使用的 team 值。
        team = db.scalar(
            select(CollaborationTeam)
            .where(
                CollaborationTeam.task_id == run.task_id,
                CollaborationTeam.status == "active",
            )
            .order_by(CollaborationTeam.updated_at.desc(), CollaborationTeam.id.desc())
        )
        if team is not None:
            # 变量说明：parent_run_id 表示parent_run 对象的唯一标识。
            team.parent_run_id = run.id
    if team is not None:
        # 变量说明：workers 表示当前流程使用的 workers 集合。
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


# 函数职责：完成 delegate_result_content 对应的业务处理。
# 参数关系：payload 表示跨层传递的数据载荷。
# 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
def _delegate_result_content(payload: dict[str, Any]) -> str:
    """Bound the child response before it becomes a parent tool observation."""

    # 变量说明：encoded 表示当前步骤使用的 encoded 值。
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str)
    if len(encoded) <= _DELEGATE_MESSAGE_LIMIT:
        return encoded
    # Keep the structured envelope valid and signal truncation explicitly.
    # 变量说明：compact 表示当前步骤使用的 compact 值。
    compact = dict(payload)
    # 变量说明：output 表示当前步骤使用的 output 值。
    output = str(compact.get("output") or "")
    compact["output"] = output[: max(0, _DELEGATE_OUTPUT_LIMIT // 2)]
    compact["output_truncated"] = True
    return json.dumps(compact, ensure_ascii=False, separators=(",", ":"), default=str)[:_DELEGATE_MESSAGE_LIMIT]
