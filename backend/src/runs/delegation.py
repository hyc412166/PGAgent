"""Delegated child-run execution and result persistence."""

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
from src.mcp import attach_mcp_tools

from src.tasks import background as background_job_service
from src.model.gateway import ModelConfigurationError, ProviderConfig
from src.artifacts.storage import ArtifactToolStore
from src.tasks.background import BackgroundJobToolStore
from src.memory.service import MemoryToolStore
from src.memory.repository import load_memory_index
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
from src.context.compaction import _configuration_digest, _utcnow
from .configuration import (
    _allowed_runtime_tool_names,
    _explicit_setting,
    _read_selected_skill_instructions,
)
from .delegation_format import (
    _delegate_result_content,
    _model_id_for_delegate,
    _single_line,
)
from .dependencies import build_model_call

_CHILD_FORBIDDEN_ORCHESTRATION_TOOLS = frozenset({
    "task",
    "Agent",
    "spawn_teammate",
    "TeamCreate",
    "TeamDelete",
    "WorkerCreate",
    "WorkerGet",
    "WorkerObserve",
    "WorkerResolveTrust",
    "WorkerAwaitReady",
    "WorkerSendPrompt",
    "WorkerRestart",
    "WorkerTerminate",
    "WorkerObserveCompletion",
    "task_create",
    "task_get",
    "task_update",
    "task_list",
    "TaskCreate",
    "RunTaskPacket",
    "TaskGet",
    "TaskList",
    "TaskStop",
    "TaskUpdate",
    "TaskOutput",
    "claim_task",
    "shutdown_request",
    "plan_approval",
    "integrate_teammate",
})
_DELEGATE_CHILD_SYSTEM_SUFFIX = (
    "\n\n你正在作为受限的子 Agent 执行一项已分配任务。"
    "只完成下方任务并返回可验证的结构化结论；不要再次委派任务，"
    "不要假称调用未启用的工具，也不要把问题直接抛给用户。"
)
class _SubagentTaskDelegate:
    """One parent runtime's bounded bridge to a user-created child Agent.

    The delegate is intentionally not a queue that can spawn arbitrary work:
    one ``task`` call produces at most one persisted ``DelegatedTask`` and
    one child Run.  The child uses the parent run's already-frozen workspace and
    permission mode, an intersected tool allowlist, and never receives the
    ``task`` tool.  It therefore cannot recursively create an unbounded agent
    tree or escape the user's active conversation policy.
    """

    def __init__(
        self,
        *,
        coordinator: Any | None = None,
        parent_run_id: str,
        parent_agent_id: str,
        parent_binding: dict[str, Any],
        parent_allowed_tool_names: list[str],
        permission_mode: str,
    ) -> None:
        if coordinator is None:
            from .lifecycle import coordinator as default_coordinator

            coordinator = default_coordinator
        self.coordinator = coordinator
        self.parent_run_id = parent_run_id
        self.parent_agent_id = parent_agent_id
        self.parent_binding = dict(parent_binding)
        self.parent_allowed_tool_names = tuple(
            str(name).strip() for name in parent_allowed_tool_names if str(name).strip()
        )
        self.permission_mode = str(permission_mode or "smart")
        self._plan_step_ids: dict[str, dict[str, str]] = {}
        self._worker_by_external_id: dict[str, dict[str, str]] = {}

    def prepare_graph(self, specs: list[dict[str, Any]], *, call_id: str | None = None) -> None:
        """Persist the complete DAG before any child in its first wave starts."""

        resolved_specs: list[dict[str, Any]] = []
        graph_key = str(call_id or "")
        worker_map: dict[str, str] = {}
        with database_module.SessionLocal() as db:
            for raw in specs:
                spec = dict(raw)
                worker = db.get(TeammateWorker, str(spec.get("agent_id") or ""))
                if worker is not None:
                    team = db.get(CollaborationTeam, worker.team_id)
                    if team is None or team.parent_run_id != self.parent_run_id:
                        raise ValueError("teammate does not belong to this parent run")
                    worker_map[str(spec["id"])] = worker.id
                    spec["agent_id"] = worker.agent_id
                    spec["workspace_mode"] = worker.workspace_mode
                resolved_specs.append(spec)
        self._plan_step_ids[graph_key] = upsert_delegated_graph(
            self.parent_run_id,
            resolved_specs,
            graph_call_id=call_id,
        )
        self._worker_by_external_id[graph_key] = worker_map

    def block_step(
        self,
        external_id: str,
        reason: str,
        *,
        graph_call_id: str | None = None,
    ) -> None:
        step_id = self._plan_step_ids.get(str(graph_call_id or ""), {}).get(str(external_id))
        if step_id:
            settle_step(step_id, status="failed", error=reason)

    @staticmethod
    def _configuration_matches_parent(connection: ModelConnection, binding: dict[str, Any]) -> bool:
        return (
            connection.id == str(binding.get("model_connection_id") or "")
            and connection.enabled
            and connection.provider == str(binding.get("provider") or "")
            and connection.base_url == str(binding.get("base_url") or "")
            and connection.secret_ref == str(binding.get("secret_ref") or "")
            and _configuration_digest(connection.custom_headers or {})
            == str(binding.get("custom_headers_digest") or "")
        )

    def _task_idempotency_key(self, *, call_id: str | None, task: str, agent_id: str) -> str:
        if call_id:
            call_digest = hashlib.sha256(str(call_id).encode("utf-8")).hexdigest()[:32]
            return f"delegate:{self.parent_run_id}:call:{call_digest}"
        digest = hashlib.sha256(f"{agent_id}\0{task}".encode("utf-8")).hexdigest()[:32]
        return f"delegate:{self.parent_run_id}:{digest}"

    @staticmethod
    def _task_title(task: str) -> str:
        compact = _single_line(task, limit=200)
        return compact or "子 Agent 任务"

    @staticmethod
    def _safe_pending_summary(pending: dict[str, Any] | None) -> dict[str, Any] | None:
        if not pending:
            return None
        return {
            "tool_name": _single_line(pending.get("tool_name"), limit=100) or "unknown",
            "reason": _single_line(pending.get("reason"), limit=500),
            "requires_user_approval": True,
        }

    @staticmethod
    def _changed_workspace(outcome: RunOutcome) -> bool:
        return any(
            event.get("type") == "tool_finished" and bool(event.get("changed"))
            for event in outcome.events
            if isinstance(event, dict)
        )

    @staticmethod
    def _error_code_for_status(status: str) -> str | None:
        return {
            "awaiting_approval": "delegate_child_awaiting_approval",
            "waiting_background": "delegate_child_waiting_event",
            "stopped": "delegate_child_stopped",
            "failed": "delegate_child_failed",
        }.get(status, "delegate_child_incomplete") if status != "completed" else None

    def _tool_result_from_payload(self, payload: dict[str, Any]) -> ToolResult:
        status = str(payload.get("status") or "failed")
        waiting_event = status == "waiting_background"
        delegation_id = str(payload.get("delegation_id") or payload.get("task_id") or "")
        return ToolResult(
            "task",
            status == "completed",
            _delegate_result_content(payload),
            changed=bool(payload.get("workspace_changed")),
            error_code=self._error_code_for_status(status),
            metadata={
                "task_id": delegation_id,
                "delegation_id": delegation_id,
                "child_run_id": str(payload.get("child_run_id") or ""),
                "child_agent_id": str(payload.get("agent", {}).get("id") or "")
                if isinstance(payload.get("agent"), dict)
                else "",
                "plan_step_id": str(payload.get("plan_step_id") or ""),
                "plan_step_external_id": str(payload.get("plan_step_external_id") or ""),
                "status": status,
                # A child approval is not an ordinary failed observation.  The
                # parent runtime uses this explicit marker to stop without
                # inventing a final answer while the child remains resumable.
                "delegated_child_awaiting_approval": status in {"awaiting_approval", "waiting_background"},
                "delegated_child_waiting_event": waiting_event,
            },
        )

    def _blocked_result(
        self,
        *,
        task_id: str | None,
        child_run_id: str | None,
        agent: Agent | None,
        code: str,
        message: str,
    ) -> ToolResult:
        payload = {
            "task_id": task_id,
            "delegation_id": task_id,
            "child_run_id": child_run_id,
            "agent": {
                "id": agent.id if agent is not None else "",
                "name": _single_line(agent.name, limit=120) if agent is not None else "",
            },
            "status": "blocked",
            "error_code": code,
            "error": _single_line(message, limit=1_000),
        }
        return ToolResult(
            "task",
            False,
            _delegate_result_content(payload),
            error_code=code,
            metadata={
                "task_id": task_id or "",
                "delegation_id": task_id or "",
                "child_run_id": child_run_id or "",
                "status": "blocked",
            },
        )

    def _remaining_parent_run_seconds(self, db: Any) -> float | None:
        """Return the child budget left in its parent run, if bounded.

        A delegated child executes inside the parent's active tool call, so
        wall-clock time since the parent run began is deliberately a
        conservative upper bound.  We never grant a second arbitrary timeout
        to a child: an unbounded parent stays unbounded and a bounded parent
        gives the child only its remaining allowance.
        """

        configured = settings.max_run_seconds
        if not configured:
            return None
        limit = max(0.0, float(configured))
        parent = db.get(Run, self.parent_run_id)
        if parent is None:
            raise RuntimeError("Parent run no longer exists")
        started_at = parent.started_at
        if started_at.tzinfo is None:
            started_at = started_at.replace(tzinfo=timezone.utc)
        elapsed = max(0.0, (_utcnow() - started_at).total_seconds())
        return max(0.0, limit - elapsed)

    def _freeze_child_binding(
        self,
        db: Any,
        child: Agent,
        *,
        workspace_root_override: str | None = None,
    ) -> tuple[ProviderConfig, dict[str, Any], list[dict[str, str]]]:
        """Snapshot all execution-relevant child settings before model I/O."""

        parent_connection_id = str(self.parent_binding.get("model_connection_id") or "")
        requested_connection_id = str(child.model_connection_id or "").strip() or parent_connection_id
        connection = db.get(ModelConnection, requested_connection_id)
        if connection is None or not connection.enabled:
            raise ModelConfigurationError("子 Agent 的模型连接不存在或已禁用")
        inherits_parent_connection = connection.id == parent_connection_id
        if inherits_parent_connection and not self._configuration_matches_parent(connection, self.parent_binding):
            raise RuntimeError("主会话冻结的模型连接已变化，拒绝在变化配置中启动子 Agent")

        model_id = _model_id_for_delegate(
            connection,
            preferred=child.model_id,
            inherited_model_id=str(self.parent_binding.get("model_id") or ""),
            may_inherit_model=inherits_parent_connection,
        )
        if not model_id:
            raise ModelConfigurationError("子 Agent 没有可用模型，请为其配置模型或主会话模型")
        thinking_level = _explicit_setting(
            child.thinking_level,
            connection.thinking_level,
            str(self.parent_binding.get("thinking_level") or ""),
        ) or "auto"

        # A child can only receive the capability intersection. The primary
        # coordinator normally has the full catalog, but this remains safe for
        # standalone/future restricted parent runtimes as well.
        parent_allowed = set(self.parent_allowed_tool_names)
        child_tools = [
            tool_name
            for tool_name in _allowed_runtime_tool_names(list(getattr(child, "tool_ids", []) or []))
            if tool_name in parent_allowed
            and tool_name not in _CHILD_FORBIDDEN_ORCHESTRATION_TOOLS
            and tool_name != "MemoryWrite"
        ]
        if "read_artifact" in parent_allowed and "read_artifact" not in child_tools:
            child_tools.append("read_artifact")
        child_skill_ids = list(getattr(child, "skill_ids", []) or [])
        skill_instructions = _read_selected_skill_instructions(db, child_skill_ids)
        workspace_root = str(
            workspace_root_override or self.parent_binding.get("workspace_root") or ""
        ).strip()
        if not workspace_root:
            raise RuntimeError("主会话缺少冻结的工作区")

        agents_instructions, agents_instruction_sources = load_instruction_chain(workspace_root)
        provider_config = ProviderConfig(
            provider=connection.provider,
            base_url=connection.base_url,
            secret_ref=connection.secret_ref,
            model_id=model_id,
            model_connection_id=connection.id,
            thinking_level=thinking_level,
            custom_headers=dict(connection.custom_headers or {}),
        )
        binding = {
            "delegation_version": 1,
            "parent_run_id": self.parent_run_id,
            "agent_id": child.id,
            "agent_system_prompt": (str(child.system_prompt or "") + _DELEGATE_CHILD_SYSTEM_SUFFIX).strip(),
            "child_agent_id": child.id,
            "workspace_root": workspace_root,
            "agents_instructions": agents_instructions,
            "agents_instruction_sources": agents_instruction_sources,
            "model_connection_id": connection.id,
            "provider": connection.provider,
            "base_url": connection.base_url,
            "secret_ref": connection.secret_ref,
            "model_id": model_id,
            "thinking_level": thinking_level,
            "permission_mode": self.permission_mode,
            "tool_ids": list(getattr(child, "tool_ids", []) or []),
            "allowed_tool_names": child_tools,
            "skill_ids": child_skill_ids,
            "skill_instructions": skill_instructions,
            "todo_state": [],
            "custom_headers_digest": _configuration_digest(connection.custom_headers or {}),
        }
        return provider_config, binding, skill_instructions

    @staticmethod
    def _public_binding(binding: dict[str, Any]) -> dict[str, Any]:
        """Persist displayable evidence without leaking routes or credentials."""

        return {
            "model_connection_id": str(binding.get("model_connection_id") or ""),
            "provider": str(binding.get("provider") or ""),
            "model_id": str(binding.get("model_id") or ""),
            "thinking_level": str(binding.get("thinking_level") or "auto"),
            "permission_mode": str(binding.get("permission_mode") or "smart"),
            "allowed_tool_names": list(binding.get("allowed_tool_names") or []),
            "skill_ids": list(binding.get("skill_ids") or []),
            "workspace_mode": str(binding.get("workspace_mode") or "shared"),
            "workspace_inherited": str(binding.get("workspace_mode") or "shared") == "shared",
            "recursive_task_enabled": False,
        }

    async def __call__(
        self,
        task: str,
        *,
        agent_id: str = "",
        call_id: str | None = None,
        plan_step_external_id: str | None = None,
        graph_call_id: str | None = None,
    ) -> ToolResult:
        """Persist, execute and summarize exactly one bounded child task."""

        requested_agent_id = str(agent_id or "").strip()
        idempotency_key = self._task_idempotency_key(call_id=call_id, task=task, agent_id=requested_agent_id)
        child: Agent | None = None
        delegation_id: str | None = None
        child_run_id: str | None = None
        provider_config: ProviderConfig | None = None
        child_binding: dict[str, Any] | None = None
        child_skill_instructions: list[dict[str, str]] = []
        child_max_run_seconds: float | None = None
        global_memories_enabled = bool(self.parent_binding.get("memories_enabled", True))
        use_memories = bool(self.parent_binding.get("use_memories", True))
        child_memory_use_enabled = global_memories_enabled and use_memories
        graph_key = str(graph_call_id or "")
        plan_step_id = self._plan_step_ids.get(graph_key, {}).get(str(plan_step_external_id or ""))
        teammate_id = self._worker_by_external_id.get(graph_key, {}).get(str(plan_step_external_id or ""))
        with database_module.SessionLocal() as db:
            if plan_step_id and db.get_bind().dialect.name == "sqlite":
                db.execute(text("BEGIN IMMEDIATE"))
            existing = db.scalar(
                select(DelegatedTask).where(DelegatedTask.idempotency_key == idempotency_key)
            )
            if existing is not None:
                result = dict(existing.result or {})
                if result:
                    return self._tool_result_from_payload(result)
                return self._blocked_result(
                    task_id=existing.id,
                    child_run_id=None,
                    agent=None,
                    code="delegate_task_in_progress",
                    message="相同的子 Agent 任务已经在执行中",
                )

            teammate = db.get(TeammateWorker, teammate_id or requested_agent_id)
            if teammate is not None:
                team = db.get(CollaborationTeam, teammate.team_id)
                if team is None or team.parent_run_id != self.parent_run_id:
                    teammate = None
                elif teammate.status in {"provisioning", "stopped", "failed", "stopping"}:
                    return self._blocked_result(
                        task_id=plan_step_id,
                        child_run_id=None,
                        agent=None,
                        code="teammate_not_available",
                        message="The persistent teammate is stopped or unavailable.",
                    )
            teammate_id = teammate.id if teammate is not None else None
            child = db.get(Agent, teammate.agent_id if teammate is not None else requested_agent_id)
            if child is None or child.id == DEFAULT_AGENT_ID or child.is_default:
                return self._blocked_result(
                    task_id=None,
                    child_run_id=None,
                    agent=None,
                    code="delegate_agent_not_found",
                    message="请求的子 Agent 不存在或不是可委派的用户 Agent",
                )
            if not child.enabled:
                return self._blocked_result(
                    task_id=None,
                    child_run_id=None,
                    agent=child,
                    code="delegate_agent_disabled",
                    message="请求的子 Agent 已被禁用",
                )
            try:
                provider_config, child_binding, child_skill_instructions = self._freeze_child_binding(
                    db,
                    child,
                    workspace_root_override=(
                        teammate.worktree_path if teammate is not None and teammate.workspace_mode == "worktree" else None
                    ),
                )
                if teammate is not None:
                    child_binding["workspace_mode"] = teammate.workspace_mode
                    child_binding["agent_system_prompt"] = (
                        str(child_binding["agent_system_prompt"])
                        + f"\n\nPersistent teammate identity: {teammate.name}; role: {teammate.role}.\n"
                        + teammate.prompt
                    ).strip()
                child_max_run_seconds = self._remaining_parent_run_seconds(db)
            except (ModelConfigurationError, RuntimeError) as exc:
                return self._blocked_result(
                    task_id=None,
                    child_run_id=None,
                    agent=child,
                    code="delegate_configuration_invalid",
                    message=str(exc),
                )

            if child_max_run_seconds is not None and child_max_run_seconds <= 0:
                return self._blocked_result(
                    task_id=None,
                    child_run_id=None,
                    agent=child,
                    code="delegate_parent_time_budget_exhausted",
                    message="The parent run has no remaining execution time for a delegated child.",
                )

            if plan_step_id:
                step = db.get(PlanStep, plan_step_id)
                if step is not None and step.status == "completed":
                    return self._tool_result_from_payload({
                        "task_id": step.id,
                        "delegation_id": step.id,
                        "plan_step_id": step.id,
                        "plan_step_external_id": step.external_id,
                        "agent": {"id": child.id, "name": _single_line(child.name, limit=120)},
                        "status": "completed",
                        "output": step.result,
                        "workspace_changed": False,
                    })
                ready_ids = {item.id for item in ready_steps(db, step.task_id)} if step is not None else set()
                if step is None or step.id not in ready_ids:
                    return self._blocked_result(
                        task_id=plan_step_id,
                        child_run_id=None,
                        agent=child,
                        code="delegate_step_not_ready",
                        message="The delegated plan step is not ready or is already claimed.",
                    )
                step.status = "in_progress"
                step.started_at = step.started_at or _utcnow()
                step.assigned_agent_id = child.id
                step.workspace_mode = teammate.workspace_mode if teammate is not None else "shared"
                step.worktree_path = teammate.worktree_path if teammate is not None else None
                step.attempt = int(step.attempt or 0) + 1
                step.error = None

            delegation = DelegatedTask(
                parent_run_id=self.parent_run_id,
                parent_session_id=db.scalar(select(Run.session_id).where(Run.id == self.parent_run_id)),
                title=self._task_title(task),
                description=task,
                status="in_progress",
                child_agent_id=child.id,
                plan_step_id=plan_step_id,
                teammate_id=teammate_id,
                idempotency_key=idempotency_key,
                result={
                    "status": "in_progress",
                    "agent": {"id": child.id, "name": _single_line(child.name, limit=120)},
                    "binding": self._public_binding(child_binding),
                },
            )
            child_run = Run(
                # A child is attached to the parent session solely so the
                # current conversation can discover its approval card and
                # terminal result.  Its frozen delegation binding still
                # selects the child Agent during resume.
                session_id=db.scalar(select(Run.session_id).where(Run.id == self.parent_run_id)),
                workspace_id=None,
                agent_id=child.id,
                mode="auto",
                status="received",
            )
            db.add_all([delegation, child_run])
            db.flush()
            child_run.workspace_id = db.scalar(
                select(Run.workspace_id).where(Run.id == self.parent_run_id)
            )
            rendered_child_task = task
            if teammate is not None:
                rendered_child_task = teammate_context(db, teammate) + "\n\n" + rendered_child_task
            child_memory_index = (
                load_memory_index(
                    workspace_id=child_run.workspace_id,
                    session_id=None,
                )
                if child_memory_use_enabled else ""
            )
            child_binding = {
                **child_binding,
                "delegation_id": delegation.id,
                "parent_agent_id": self.parent_agent_id,
                "parent_session_id": child_run.session_id,
                "max_run_seconds": child_max_run_seconds,
                "memories_enabled": global_memories_enabled,
                "use_memories": use_memories,
                "memory_index": child_memory_index,
                "rendered_task": rendered_child_task,
                "plan_step_id": plan_step_id,
                "plan_step_external_id": plan_step_external_id,
                "teammate_id": teammate_id,
            }
            delegation.child_run_id = child_run.id
            delegation.result = {
                **dict(delegation.result or {}),
                "task_id": delegation.id,
                "delegation_id": delegation.id,
                "child_run_id": child_run.id,
                "parent_run_id": self.parent_run_id,
                "parent_session_id": child_run.session_id,
                "plan_step_id": plan_step_id,
                "plan_step_external_id": plan_step_external_id,
                "teammate_id": teammate_id,
            }
            if plan_step_id:
                plan_step = db.get(PlanStep, plan_step_id)
                if plan_step is not None:
                    plan_step.assigned_run_id = child_run.id
            if teammate is not None:
                teammate.status = "working"
                teammate.current_plan_step_id = plan_step_id
                teammate.last_run_id = child_run.id
            # Persist the complete frozen child configuration in the same
            # transaction that creates the child Run. If the process exits
            # before the first provider response, recovery still has the
            # exact execution binding rather than only its public summary.
            db.add(RunEvent(
                run_id=child_run.id,
                event_type="runtime_snapshot",
                payload=type(self.coordinator)._runtime_snapshot(RunOutcome(
                    status="received",
                    output=None,
                    messages=[],
                    events=[],
                    steps=0,
                    tool_calls=0,
                    mode="auto",
                    runtime_binding=child_binding,
                )),
            ))
            # This link exists before model I/O. It lets terminal failure
            # handling reconcile the delegation even if no runtime snapshot was
            # ever produced.
            db.add(RunEvent(
                run_id=child_run.id,
                event_type="delegation_link",
                payload={
                    "delegation_id": delegation.id,
                    "parent_run_id": self.parent_run_id,
                    "parent_agent_id": self.parent_agent_id,
                    "parent_session_id": child_run.session_id,
                },
            ))
            # This lightweight parent event reaches the active conversation
            # before the synchronous child model work begins.  The browser can
            # therefore reveal the child side panel immediately instead of
            # looking frozen until the child has a terminal result.
            db.add(RunEvent(
                run_id=self.parent_run_id,
                event_type="delegated_child_started",
                payload={
                    "task_id": delegation.id,
                    "child_run_id": child_run.id,
                    "child_agent_id": child.id,
                    "child_agent_name": _single_line(child.name, limit=120),
                    "task_title": self._task_title(task),
                },
            ))
            db.commit()
            delegation_id = delegation.id
            child_run_id = child_run.id

        assert child is not None and provider_config is not None and child_binding is not None
        assert delegation_id is not None and child_run_id is not None
        run_stream_broker.publish(self.parent_run_id, {
            "type": "delegated_child_started",
            "task_id": delegation_id,
            "child_run_id": child_run_id,
            "child_agent_id": child.id,
            "child_agent_name": _single_line(child.name, limit=120),
            "task_title": self._task_title(task),
        })
        child_background_store = BackgroundJobToolStore(
            run_id=child_run_id,
            workspace_id=child_run.workspace_id,
            session_id=child_run.session_id,
            workspace_root=str(child_binding["workspace_root"]),
        )
        child_team_store = TeamToolStore(
            run_id=self.parent_run_id,
            session_id=child_run.session_id,
            workspace_root=str(child_binding["workspace_root"]),
            actor_worker_id=teammate_id,
        ) if teammate_id else None
        child_artifact_store = FilesystemArtifactStore(
            settings.data_dir / "artifacts" / str(child_run.session_id or child_run.id)
        )
        child_registry = create_default_registry(
            str(child_binding["workspace_root"]),
            allowed_tool_names=child_binding["allowed_tool_names"],
            permission_mode=child_binding["permission_mode"],
            skill_instructions=child_skill_instructions,
            todo_state=[],
            memory_store=(
                MemoryToolStore(
                    workspace_id=child_run.workspace_id,
                    session_id=None,
                )
                if child_memory_use_enabled else None
            ),
            artifact_store=ArtifactToolStore(child_artifact_store),
            background_store=child_background_store,
            team_store=child_team_store,
            # Deliberately omit task_delegate: task was removed from the
            # allowlist and a child never obtains a recursive dispatch hook.
        )
        # A delegated child runs inside the parent's asyncio task, but its
        # tools may own subprocesses.  Register its cancellation hook so a
        # user stop can terminate those side effects as well as the parent
        # coroutine awaiting the child.
        self.coordinator.register_tool_canceller(child_run_id, child_registry.cancel_active)
        await attach_mcp_tools(
            child_registry,
            session_key=str(child_run.session_id or child_run_id),
            workspace_root=str(child_binding["workspace_root"]),
            frozen_tools=child_binding.get("mcp_tools"),
        )
        child_runtime = AgentRuntime(
            model_call=build_model_call(provider_config),
            tool_registry=child_registry,
            context_manager=ContextManager(max_tokens=settings.context_limit_tokens),
            artifact_store=child_artifact_store,
            event_sink=type(self.coordinator)._event_sink(child_run_id),
            config=RuntimeConfig(
                max_steps=settings.max_steps,
                max_tool_calls=settings.max_tool_calls,
                identical_call_limit=settings.max_identical_calls,
                no_progress_limit=settings.no_progress_limit,
                context_compaction_threshold_tokens=settings.compact_threshold_tokens,
                model_timeout_seconds=min(
                    float(settings.model_timeout_seconds),
                    child_max_run_seconds,
                ) if child_max_run_seconds is not None else settings.model_timeout_seconds,
                max_run_seconds=child_max_run_seconds,
            ),
        )
        type(self.coordinator)._install_completion_verifier(
            child_runtime,
            {
                "runtime_binding": child_binding,
                "background_store": child_background_store,
            },
        )
        try:
            outcome = await child_runtime.run(
                system_prompt=str(child_binding["agent_system_prompt"]),
                agent_instructions=(
                    "耗时命令可先用 background_run 启动；完成其他独立工作后，必须调用 "
                    "check_background(wait=true) 等待并读取终态，不能把入队当作完成。"
                    if {"background_run", "check_background"}.issubset(
                        set(child_binding["allowed_tool_names"])
                    ) else ""
                ),
                workspace_rules=render_workspace_rules(
                    str(child_binding["workspace_root"]),
                    str(child_binding.get("agents_instructions") or ""),
                ),
                memory_index=str(child_binding.get("memory_index") or ""),
                recent_messages=[{"role": "user", "content": str(child_binding["rendered_task"])}],
                mode="auto",
            )
        except asyncio.CancelledError:
            type(self.coordinator)._persist_failure(child_run_id, RuntimeError("Delegated child execution was cancelled"))
            raise
        except Exception as exc:
            type(self.coordinator)._persist_failure(child_run_id, exc)
            return self._blocked_result(
                task_id=delegation_id,
                child_run_id=child_run_id,
                agent=child,
                code="delegate_execution_error",
                message=str(exc) or type(exc).__name__,
            )

        outcome.runtime_binding = {
            **child_binding,
            **child_registry.runtime_state(),
        }
        type(self.coordinator)._persist_outcome(
            child_run_id,
            outcome,
            child_background_store.delivered_terminal_ids(),
        )
        with database_module.SessionLocal() as db:
            persisted_delegation = db.get(DelegatedTask, delegation_id)
            if persisted_delegation is None:
                return self._blocked_result(
                    task_id=delegation_id,
                    child_run_id=child_run_id,
                    agent=child,
                    code="delegate_task_missing",
                    message="Delegated task record disappeared before its outcome could be read.",
                )
            payload = dict(persisted_delegation.result or {})
        return self._tool_result_from_payload(payload)
