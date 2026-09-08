"""Delegated child-run execution and result persistence."""
# 文件职责：负责运行创建、恢复、流式传输和生命周期中的 delegation 子模块。
# 逻辑关系：上层通过 runs/delegation.py 使用本模块；本模块把处理结果交给同领域服务、持久化层或 API 响应层。

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
from src.persistence.run_events import append_run_event
from src.agent import RunOutcome
from src.attachments.contracts import ATTACHMENT_TOOL_NAMES
from src.tools.types import ToolResult

from src.model.gateway import ModelConfigurationError, ProviderConfig
from src.memory.repository import load_memory_index
from src.context.instructions import load_instruction_chain, render_workspace_rules
from src.runs.stream import run_stream_broker
from src.tasks.graph import ready_steps, settle_step, upsert_delegated_graph
from src.agents.collaboration import teammate_context
from src.sessions.delivery import (
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

# 变量说明：_CHILD_FORBIDDEN_ORCHESTRATION_TOOLS 表示当前流程使用的 _CHILD_FORBIDDEN_ORCHESTRATION_TOOLS 集合。
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
# 变量说明：_DELEGATE_CHILD_SYSTEM_SUFFIX 表示当前步骤使用的 _DELEGATE_CHILD_SYSTEM_SUFFIX 值。
_DELEGATE_CHILD_SYSTEM_SUFFIX = (
    "\n\n你正在作为受限的子 Agent 执行一项已分配任务。"
    "只完成下方任务并返回可验证的结构化结论；不要再次委派任务，"
    "不要假称调用未启用的工具，也不要把问题直接抛给用户。"
)
# 类职责：定义 _SubagentTaskDelegate 在本领域中的数据与行为。
class _SubagentTaskDelegate:
    """One parent runtime's bounded bridge to a user-created child Agent.

    The delegate is intentionally not a queue that can spawn arbitrary work:
    one ``task`` call produces at most one persisted ``DelegatedTask`` and
    one child Run.  The child uses the parent run's already-frozen workspace and
    permission mode, an intersected tool allowlist, and never receives the
    ``task`` tool.  It therefore cannot recursively create an unbounded agent
    tree or escape the user's active conversation policy.
    """

    # 函数职责：初始化实例依赖与初始状态。
    # 参数关系：coordinator 表示当前步骤使用的 coordinator 值；parent_run_id 表示parent_run 对象的唯一标识；parent_agent_id 表示parent_agent 对象的唯一标识；parent_binding 表示当前步骤使用的 parent_binding 值；parent_allowed_tool_names 表示当前流程使用的 parent_allowed_tool_names 集合；permission_mode 表示当前步骤使用的 permission_mode 值。
    # 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
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

            # 变量说明：coordinator 表示当前步骤使用的 coordinator 值。
            coordinator = default_coordinator
        # 变量说明：coordinator 表示当前步骤使用的 coordinator 值。
        self.coordinator = coordinator
        # 变量说明：parent_run_id 表示parent_run 对象的唯一标识。
        self.parent_run_id = parent_run_id
        # 变量说明：parent_agent_id 表示parent_agent 对象的唯一标识。
        self.parent_agent_id = parent_agent_id
        # 变量说明：parent_binding 表示当前步骤使用的 parent_binding 值。
        self.parent_binding = dict(parent_binding)
        # 变量说明：parent_allowed_tool_names 表示当前流程使用的 parent_allowed_tool_names 集合。
        self.parent_allowed_tool_names = tuple(
            str(name).strip() for name in parent_allowed_tool_names if str(name).strip()
        )
        # 变量说明：permission_mode 表示当前步骤使用的 permission_mode 值。
        self.permission_mode = str(permission_mode or "smart")
        # 变量说明：_plan_step_ids 表示_plan_step 对象标识集合。
        self._plan_step_ids: dict[str, dict[str, str]] = {}
        # 变量说明：_worker_by_external_id 表示_worker_by_external 对象的唯一标识。
        self._worker_by_external_id: dict[str, dict[str, str]] = {}

    # 函数职责：准备 graph 对应的数据或流程。
    # 参数关系：specs 表示当前流程使用的 specs 集合；call_id 表示call 对象的唯一标识。
    # 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
    def prepare_graph(self, specs: list[dict[str, Any]], *, call_id: str | None = None) -> None:
        """Persist the complete DAG before any child in its first wave starts."""

        # 变量说明：resolved_specs 表示当前流程使用的 resolved_specs 集合。
        resolved_specs: list[dict[str, Any]] = []
        # 变量说明：graph_key 表示当前步骤使用的 graph_key 值。
        graph_key = str(call_id or "")
        # 变量说明：worker_map 表示按键快速定位 worker_map 数据的映射。
        worker_map: dict[str, str] = {}
        with database_module.SessionLocal() as db:
            for raw in specs:
                # 变量说明：spec 表示当前步骤使用的 spec 值。
                spec = dict(raw)
                # 变量说明：worker 表示当前步骤使用的 worker 值。
                worker = db.get(TeammateWorker, str(spec.get("agent_id") or ""))
                if worker is not None:
                    # 变量说明：team 表示当前步骤使用的 team 值。
                    team = db.get(CollaborationTeam, worker.team_id)
                    if team is None or team.parent_run_id != self.parent_run_id:
                        raise ValueError("teammate does not belong to this parent run")
                    # 变量说明：worker_map 的索引项 表示该语句创建或更新的目标数据。
                    worker_map[str(spec["id"])] = worker.id
                    # 变量说明：spec 的索引项 表示该语句创建或更新的目标数据。
                    spec["agent_id"] = worker.agent_id
                    # 变量说明：spec 的索引项 表示该语句创建或更新的目标数据。
                    spec["workspace_mode"] = worker.workspace_mode
                resolved_specs.append(spec)
        # 变量说明：映射 的索引项 表示该语句创建或更新的目标数据。
        self._plan_step_ids[graph_key] = upsert_delegated_graph(
            self.parent_run_id,
            resolved_specs,
            graph_call_id=call_id,
        )
        # 变量说明：映射 的索引项 表示该语句创建或更新的目标数据。
        self._worker_by_external_id[graph_key] = worker_map

    # 函数职责：完成 block_step 对应的业务处理。
    # 参数关系：external_id 表示external 对象的唯一标识；reason 表示当前步骤使用的 reason 值；graph_call_id 表示graph_call 对象的唯一标识。
    # 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
    def block_step(
        self,
        external_id: str,
        reason: str,
        *,
        graph_call_id: str | None = None,
    ) -> None:
        # 变量说明：step_id 表示step 对象的唯一标识。
        step_id = self._plan_step_ids.get(str(graph_call_id or ""), {}).get(str(external_id))
        if step_id:
            settle_step(step_id, status="failed", error=reason)

    # 函数职责：完成 configuration_matches_parent 对应的业务处理。
    # 参数关系：connection 表示当前步骤使用的 connection 值；binding 表示当前步骤使用的 binding 值。
    # 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
    @staticmethod
    def _configuration_matches_parent(connection: ModelConnection, binding: dict[str, Any]) -> bool:
        return (
            connection.id == str(binding.get("model_connection_id") or "")
            and connection.enabled
            and connection.provider == str(binding.get("provider") or "")
            and connection.api_protocol == str(binding.get("api_protocol") or "chat_completions")
            and connection.base_url == str(binding.get("base_url") or "")
            and connection.secret_ref == str(binding.get("secret_ref") or "")
            and _configuration_digest(connection.custom_headers or {})
            == str(binding.get("custom_headers_digest") or "")
        )

    # 函数职责：完成 task_idempotency_key 对应的业务处理。
    # 参数关系：call_id 表示call 对象的唯一标识；task 表示当前步骤使用的 task 值；agent_id 表示智能体标识。
    # 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
    def _task_idempotency_key(self, *, call_id: str | None, task: str, agent_id: str) -> str:
        if call_id:
            # 变量说明：call_digest 表示当前步骤使用的 call_digest 值。
            call_digest = hashlib.sha256(str(call_id).encode("utf-8")).hexdigest()[:32]
            return f"delegate:{self.parent_run_id}:call:{call_digest}"
        # 变量说明：digest 表示当前步骤使用的 digest 值。
        digest = hashlib.sha256(f"{agent_id}\0{task}".encode("utf-8")).hexdigest()[:32]
        return f"delegate:{self.parent_run_id}:{digest}"

    # 函数职责：完成 task_title 对应的业务处理。
    # 参数关系：task 表示当前步骤使用的 task 值。
    # 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
    @staticmethod
    def _task_title(task: str) -> str:
        # 变量说明：compact 表示当前步骤使用的 compact 值。
        compact = _single_line(task, limit=200)
        return compact or "子 Agent 任务"

    # 函数职责：完成 safe_pending_summary 对应的业务处理。
    # 参数关系：pending 表示当前步骤使用的 pending 值。
    # 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
    @staticmethod
    def _safe_pending_summary(pending: dict[str, Any] | None) -> dict[str, Any] | None:
        if not pending:
            return None
        return {
            "tool_name": _single_line(pending.get("tool_name"), limit=100) or "unknown",
            "reason": _single_line(pending.get("reason"), limit=500),
            "requires_user_approval": True,
        }

    # 函数职责：完成 changed_workspace 对应的业务处理。
    # 参数关系：outcome 表示当前步骤使用的 outcome 值。
    # 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
    @staticmethod
    def _changed_workspace(outcome: RunOutcome) -> bool:
        return any(
            event.get("type") == "tool_finished" and bool(event.get("changed"))
            for event in outcome.events
            if isinstance(event, dict)
        )

    # 函数职责：完成 error_code_for_status 对应的业务处理。
    # 参数关系：status 表示当前对象或运行的状态。
    # 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
    @staticmethod
    def _error_code_for_status(status: str) -> str | None:
        return {
            "awaiting_approval": "delegate_child_awaiting_approval",
            "waiting_background": "delegate_child_waiting_event",
            "in_progress": "delegate_child_waiting_event",
            "stopped": "delegate_child_stopped",
            "failed": "delegate_child_failed",
        }.get(status, "delegate_child_incomplete") if status != "completed" else None

    # 函数职责：完成 tool_result_from_payload 对应的业务处理。
    # 参数关系：payload 表示跨层传递的数据载荷。
    # 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
    def _tool_result_from_payload(self, payload: dict[str, Any]) -> ToolResult:
        # 变量说明：status 表示当前对象或运行的状态。
        status = str(payload.get("status") or "failed")
        # 变量说明：waiting_event 表示当前步骤使用的 waiting_event 值。
        waiting_event = status in {"waiting_background", "in_progress"}
        # 变量说明：delegation_id 表示delegation 对象的唯一标识。
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
                "delegated_child_awaiting_approval": status in {"awaiting_approval", "waiting_background", "in_progress"},
                "delegated_child_waiting_event": waiting_event,
            },
        )

    # 函数职责：完成 blocked_result 对应的业务处理。
    # 参数关系：task_id 表示任务标识；child_run_id 表示child_run 对象的唯一标识；agent 表示当前步骤使用的 agent 值；code 表示当前步骤使用的 code 值；message 表示当前消息。
    # 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
    def _blocked_result(
        self,
        *,
        task_id: str | None,
        child_run_id: str | None,
        agent: Agent | None,
        code: str,
        message: str,
    ) -> ToolResult:
        # 变量说明：payload 表示跨层传递的数据载荷。
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

    # 函数职责：完成 remaining_parent_run_seconds 对应的业务处理。
    # 参数关系：db 表示当前数据库会话。
    # 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
    def _remaining_parent_run_seconds(self, db: Any) -> float | None:
        """Return the child budget left in its parent run, if bounded.

        A delegated child executes inside the parent's active tool call, so
        wall-clock time since the parent run began is deliberately a
        conservative upper bound.  We never grant a second arbitrary timeout
        to a child: an unbounded parent stays unbounded and a bounded parent
        gives the child only its remaining allowance.
        """

        # 变量说明：configured 表示当前步骤使用的 configured 值。
        configured = settings.max_run_seconds
        if not configured:
            return None
        # 变量说明：limit 表示当前步骤使用的 limit 值。
        limit = max(0.0, float(configured))
        # 变量说明：parent 表示当前步骤使用的 parent 值。
        parent = db.get(Run, self.parent_run_id)
        if parent is None:
            raise RuntimeError("Parent run no longer exists")
        # 变量说明：started_at 表示started_at 对应的时间信息。
        started_at = parent.started_at
        if started_at.tzinfo is None:
            # 变量说明：started_at 表示started_at 对应的时间信息。
            started_at = started_at.replace(tzinfo=timezone.utc)
        # 变量说明：elapsed 表示当前步骤使用的 elapsed 值。
        elapsed = max(0.0, (_utcnow() - started_at).total_seconds())
        return max(0.0, limit - elapsed)

    # 函数职责：完成 freeze_child_binding 对应的业务处理。
    # 参数关系：db 表示当前数据库会话；child 表示当前步骤使用的 child 值；workspace_root_override 表示当前步骤使用的 workspace_root_override 值。
    # 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
    def _freeze_child_binding(
        self,
        db: Any,
        child: Agent,
        *,
        workspace_root_override: str | None = None,
    ) -> tuple[ProviderConfig, dict[str, Any], list[dict[str, str]]]:
        """Snapshot all execution-relevant child settings before model I/O."""

        # 变量说明：parent_connection_id 表示parent_connection 对象的唯一标识。
        parent_connection_id = str(self.parent_binding.get("model_connection_id") or "")
        # 变量说明：requested_connection_id 表示requested_connection 对象的唯一标识。
        requested_connection_id = str(child.model_connection_id or "").strip() or parent_connection_id
        # 变量说明：connection 表示当前步骤使用的 connection 值。
        connection = db.get(ModelConnection, requested_connection_id)
        if connection is None or not connection.enabled:
            raise ModelConfigurationError("子 Agent 的模型连接不存在或已禁用")
        # 变量说明：inherits_parent_connection 表示当前步骤使用的 inherits_parent_connection 值。
        inherits_parent_connection = connection.id == parent_connection_id
        if inherits_parent_connection and not self._configuration_matches_parent(connection, self.parent_binding):
            raise RuntimeError("主会话冻结的模型连接已变化，拒绝在变化配置中启动子 Agent")

        # 变量说明：model_id 表示model 对象的唯一标识。
        model_id = _model_id_for_delegate(
            connection,
            preferred=child.model_id,
            inherited_model_id=str(self.parent_binding.get("model_id") or ""),
            may_inherit_model=inherits_parent_connection,
        )
        if not model_id:
            raise ModelConfigurationError("子 Agent 没有可用模型，请为其配置模型或主会话模型")
        # 变量说明：thinking_level 表示当前步骤使用的 thinking_level 值。
        thinking_level = _explicit_setting(
            child.thinking_level,
            connection.thinking_level,
            str(self.parent_binding.get("thinking_level") or ""),
        ) or "auto"

        # A child can only receive the capability intersection. The primary
        # coordinator normally has the full catalog, but this remains safe for
        # standalone/future restricted parent runtimes as well.
        # 变量说明：parent_allowed 表示当前步骤使用的 parent_allowed 值。
        parent_allowed = set(self.parent_allowed_tool_names)
        # 变量说明：child_tools 表示当前流程使用的 child_tools 集合。
        child_tools = [
            tool_name
            for tool_name in _allowed_runtime_tool_names(list(getattr(child, "tool_ids", []) or []))
            if tool_name in parent_allowed
            and tool_name not in _CHILD_FORBIDDEN_ORCHESTRATION_TOOLS
            and tool_name != "MemoryWrite"
        ]
        if "read_artifact" in parent_allowed and "read_artifact" not in child_tools:
            child_tools.append("read_artifact")
        for tool_name in ATTACHMENT_TOOL_NAMES:
            if tool_name in parent_allowed and tool_name not in child_tools:
                child_tools.append(tool_name)
        # 变量说明：child_skill_ids 表示child_skill 对象标识集合。
        child_skill_ids = list(getattr(child, "skill_ids", []) or [])
        # 变量说明：skill_instructions 表示当前流程使用的 skill_instructions 集合。
        skill_instructions = _read_selected_skill_instructions(db, child_skill_ids)
        # 变量说明：workspace_root 表示当前步骤使用的 workspace_root 值。
        workspace_root = str(
            workspace_root_override or self.parent_binding.get("workspace_root") or ""
        ).strip()
        if not workspace_root:
            raise RuntimeError("主会话缺少冻结的工作区")

        # 变量说明：agents_instructions 表示当前流程使用的 agents_instructions 集合；agents_instruction_sources 表示当前流程使用的 agents_instruction_sources 集合。
        agents_instructions, agents_instruction_sources = load_instruction_chain(workspace_root)
        # 变量说明：provider_config 表示当前步骤使用的 provider_config 值。
        provider_config = ProviderConfig(
            provider=connection.provider,
            base_url=connection.base_url,
            secret_ref=connection.secret_ref,
            model_id=model_id,
            model_connection_id=connection.id,
            thinking_level=thinking_level,
            custom_headers=dict(connection.custom_headers or {}),
            api_protocol=connection.api_protocol,
        )
        # 变量说明：binding 表示当前步骤使用的 binding 值。
        binding = {
            "delegation_version": 1,
            "parent_run_id": self.parent_run_id,
            "agent_id": child.id,
            "agent_system_prompt": (str(child.system_prompt or "") + _DELEGATE_CHILD_SYSTEM_SUFFIX).strip(),
            "workflow_profile_id": str(
                getattr(child, "workflow_profile_id", "auto") or "auto"
            ),
            "child_agent_id": child.id,
            "workspace_root": workspace_root,
            "agents_instructions": agents_instructions,
            "agents_instruction_sources": agents_instruction_sources,
            "model_connection_id": connection.id,
            "provider": connection.provider,
            "api_protocol": connection.api_protocol,
            "base_url": connection.base_url,
            "secret_ref": connection.secret_ref,
            "model_id": model_id,
            "thinking_level": thinking_level,
            "permission_mode": self.permission_mode,
            "tool_ids": list(getattr(child, "tool_ids", []) or []),
            "allowed_tool_names": child_tools,
            "skill_ids": child_skill_ids,
            "mcp_server_names": list(self.parent_binding.get("mcp_server_names") or []),
            "skill_instructions": skill_instructions,
            "todo_state": [],
            "validation_runtime": dict(self.parent_binding.get("validation_runtime") or {}),
            "custom_headers_digest": _configuration_digest(connection.custom_headers or {}),
        }
        return provider_config, binding, skill_instructions

    # 函数职责：完成 public_binding 对应的业务处理。
    # 参数关系：binding 表示当前步骤使用的 binding 值。
    # 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
    @staticmethod
    def _public_binding(binding: dict[str, Any]) -> dict[str, Any]:
        """Persist displayable evidence without leaking routes or credentials."""

        return {
            "model_connection_id": str(binding.get("model_connection_id") or ""),
            "provider": str(binding.get("provider") or ""),
            "api_protocol": str(binding.get("api_protocol") or "chat_completions"),
            "model_id": str(binding.get("model_id") or ""),
            "thinking_level": str(binding.get("thinking_level") or "auto"),
            "workflow_profile_id": str(binding.get("workflow_profile_id") or "auto"),
            "permission_mode": str(binding.get("permission_mode") or "smart"),
            "allowed_tool_names": list(binding.get("allowed_tool_names") or []),
            "skill_ids": list(binding.get("skill_ids") or []),
            "mcp_server_names": list(binding.get("mcp_server_names") or []),
            "workspace_mode": str(binding.get("workspace_mode") or "shared"),
            "workspace_inherited": str(binding.get("workspace_mode") or "shared") == "shared",
            "recursive_task_enabled": False,
        }

    # 函数职责：异步以可调用对象形式执行该组件。
    # 参数关系：task 表示当前步骤使用的 task 值；agent_id 表示智能体标识；call_id 表示call 对象的唯一标识；plan_step_external_id 表示plan_step_external 对象的唯一标识；graph_call_id 表示graph_call 对象的唯一标识。
    # 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
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

        # 变量说明：requested_agent_id 表示requested_agent 对象的唯一标识。
        requested_agent_id = str(agent_id or "").strip()
        # 变量说明：idempotency_key 表示当前步骤使用的 idempotency_key 值。
        idempotency_key = self._task_idempotency_key(call_id=call_id, task=task, agent_id=requested_agent_id)
        # 变量说明：child 表示当前步骤使用的 child 值。
        child: Agent | None = None
        # 变量说明：delegation_id 表示delegation 对象的唯一标识。
        delegation_id: str | None = None
        # 变量说明：child_run_id 表示child_run 对象的唯一标识。
        child_run_id: str | None = None
        # 变量说明：provider_config 表示当前步骤使用的 provider_config 值。
        provider_config: ProviderConfig | None = None
        # 变量说明：child_binding 表示当前步骤使用的 child_binding 值。
        child_binding: dict[str, Any] | None = None
        # 变量说明：child_skill_instructions 表示当前流程使用的 child_skill_instructions 集合。
        child_skill_instructions: list[dict[str, str]] = []
        # 变量说明：child_max_run_seconds 表示当前流程使用的 child_max_run_seconds 集合。
        child_max_run_seconds: float | None = None
        # 变量说明：global_memories_enabled 表示当前步骤使用的 global_memories_enabled 值。
        global_memories_enabled = bool(self.parent_binding.get("memories_enabled", True))
        # 变量说明：use_memories 表示当前流程使用的 use_memories 集合。
        use_memories = bool(self.parent_binding.get("use_memories", True))
        # 变量说明：child_memory_use_enabled 表示当前步骤使用的 child_memory_use_enabled 值。
        child_memory_use_enabled = global_memories_enabled and use_memories
        # 变量说明：graph_key 表示当前步骤使用的 graph_key 值。
        graph_key = str(graph_call_id or "")
        # 变量说明：plan_step_id 表示plan_step 对象的唯一标识。
        plan_step_id = self._plan_step_ids.get(graph_key, {}).get(str(plan_step_external_id or ""))
        # 变量说明：teammate_id 表示teammate 对象的唯一标识。
        teammate_id = self._worker_by_external_id.get(graph_key, {}).get(str(plan_step_external_id or ""))
        with database_module.SessionLocal() as db:
            if plan_step_id and db.get_bind().dialect.name == "sqlite":
                db.execute(text("BEGIN IMMEDIATE"))
            # 变量说明：existing 表示当前步骤使用的 existing 值。
            existing = db.scalar(
                select(DelegatedTask).where(DelegatedTask.idempotency_key == idempotency_key)
            )
            if existing is not None:
                # 变量说明：result 表示本步骤产生的结果。
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

            # 变量说明：teammate 表示当前步骤使用的 teammate 值。
            teammate = db.get(TeammateWorker, teammate_id or requested_agent_id)
            if teammate is not None:
                # 变量说明：team 表示当前步骤使用的 team 值。
                team = db.get(CollaborationTeam, teammate.team_id)
                if team is None or team.parent_run_id != self.parent_run_id:
                    # 变量说明：teammate 表示当前步骤使用的 teammate 值。
                    teammate = None
                elif teammate.status in {"provisioning", "stopped", "failed", "stopping"}:
                    return self._blocked_result(
                        task_id=plan_step_id,
                        child_run_id=None,
                        agent=None,
                        code="teammate_not_available",
                        message="The persistent teammate is stopped or unavailable.",
                    )
            # 变量说明：teammate_id 表示teammate 对象的唯一标识。
            teammate_id = teammate.id if teammate is not None else None
            # 变量说明：child 表示当前步骤使用的 child 值。
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
                # 变量说明：provider_config 表示当前步骤使用的 provider_config 值；child_binding 表示当前步骤使用的 child_binding 值；child_skill_instructions 表示当前流程使用的 child_skill_instructions 集合。
                provider_config, child_binding, child_skill_instructions = self._freeze_child_binding(
                    db,
                    child,
                    workspace_root_override=(
                        teammate.worktree_path if teammate is not None and teammate.workspace_mode == "worktree" else None
                    ),
                )
                if teammate is not None:
                    # 变量说明：child_binding 的索引项 表示该语句创建或更新的目标数据。
                    child_binding["workspace_mode"] = teammate.workspace_mode
                    # 变量说明：child_binding 的索引项 表示该语句创建或更新的目标数据。
                    child_binding["agent_system_prompt"] = (
                        str(child_binding["agent_system_prompt"])
                        + f"\n\nPersistent teammate identity: {teammate.name}; role: {teammate.role}.\n"
                        + teammate.prompt
                    ).strip()
                # 变量说明：child_max_run_seconds 表示当前流程使用的 child_max_run_seconds 集合。
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
                # 变量说明：step 表示当前步骤使用的 step 值。
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
                # 变量说明：ready_ids 表示ready 对象标识集合。
                ready_ids = {item.id for item in ready_steps(db, step.task_id)} if step is not None else set()
                if step is None or step.id not in ready_ids:
                    return self._blocked_result(
                        task_id=plan_step_id,
                        child_run_id=None,
                        agent=child,
                        code="delegate_step_not_ready",
                        message="The delegated plan step is not ready or is already claimed.",
                    )
                # 变量说明：status 表示当前对象或运行的状态。
                step.status = "in_progress"
                # 变量说明：started_at 表示started_at 对应的时间信息。
                step.started_at = step.started_at or _utcnow()
                # 变量说明：assigned_agent_id 表示assigned_agent 对象的唯一标识。
                step.assigned_agent_id = child.id
                # 变量说明：workspace_mode 表示当前步骤使用的 workspace_mode 值。
                step.workspace_mode = teammate.workspace_mode if teammate is not None else "shared"
                # 变量说明：worktree_path 表示worktree_path 对应的文件系统位置。
                step.worktree_path = teammate.worktree_path if teammate is not None else None
                # 变量说明：attempt 表示当前步骤使用的 attempt 值。
                step.attempt = int(step.attempt or 0) + 1
                # 变量说明：error 表示当前捕获或准备上报的错误。
                step.error = None

            # 变量说明：delegation 表示当前步骤使用的 delegation 值。
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
            # 变量说明：child_run 表示当前步骤使用的 child_run 值。
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
            # 变量说明：workspace_id 表示工作区标识。
            child_run.workspace_id = db.scalar(
                select(Run.workspace_id).where(Run.id == self.parent_run_id)
            )
            # 变量说明：rendered_child_task 表示当前步骤使用的 rendered_child_task 值。
            rendered_child_task = task
            if teammate is not None:
                # 变量说明：rendered_child_task 表示当前步骤使用的 rendered_child_task 值。
                rendered_child_task = teammate_context(db, teammate) + "\n\n" + rendered_child_task
            # 变量说明：child_memory_index 表示当前步骤使用的 child_memory_index 值。
            child_memory_index = (
                load_memory_index(
                    workspace_id=child_run.workspace_id,
                    session_id=None,
                )
                if child_memory_use_enabled else ""
            )
            # 变量说明：child_binding 表示当前步骤使用的 child_binding 值。
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
            # 变量说明：child_run_id 表示child_run 对象的唯一标识。
            delegation.child_run_id = child_run.id
            # 变量说明：result 表示本步骤产生的结果。
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
                # 变量说明：plan_step 表示当前步骤使用的 plan_step 值。
                plan_step = db.get(PlanStep, plan_step_id)
                if plan_step is not None:
                    # 变量说明：assigned_run_id 表示assigned_run 对象的唯一标识。
                    plan_step.assigned_run_id = child_run.id
            if teammate is not None:
                # 变量说明：status 表示当前对象或运行的状态。
                teammate.status = "working"
                # 变量说明：current_plan_step_id 表示current_plan_step 对象的唯一标识。
                teammate.current_plan_step_id = plan_step_id
                # 变量说明：last_run_id 表示last_run 对象的唯一标识。
                teammate.last_run_id = child_run.id
            # Persist the complete frozen child configuration in the same
            # transaction that creates the child Run. If the process exits
            # before the first provider response, recovery still has the
            # exact execution binding rather than only its public summary.
            append_run_event(
                db,
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
            )
            # This link exists before model I/O. It lets terminal failure
            # handling reconcile the delegation even if no runtime snapshot was
            # ever produced.
            append_run_event(
                db,
                run_id=child_run.id,
                event_type="delegation_link",
                payload={
                    "delegation_id": delegation.id,
                    "parent_run_id": self.parent_run_id,
                    "parent_agent_id": self.parent_agent_id,
                    "parent_session_id": child_run.session_id,
                },
            )
            # This lightweight parent event reaches the active conversation
            # before the synchronous child model work begins.  The browser can
            # therefore reveal the child side panel immediately instead of
            # looking frozen until the child has a terminal result.
            append_run_event(
                db,
                run_id=self.parent_run_id,
                event_type="delegated_child_started",
                payload={
                    "task_id": delegation.id,
                    "child_run_id": child_run.id,
                    "child_agent_id": child.id,
                    "child_agent_name": _single_line(child.name, limit=120),
                    "task_title": self._task_title(task),
                },
            )
            db.commit()
            # 变量说明：delegation_id 表示delegation 对象的唯一标识。
            delegation_id = delegation.id
            # 变量说明：child_run_id 表示child_run 对象的唯一标识。
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
        if not self.coordinator.launch(child_run_id):
            # 协调器关闭或启动竞态时不会再有 worker 负责落库；此处立即
            # 将 child/delegation/plan step 统一收敛为失败，避免侧栏永久卡在进行中。
            with database_module.SessionLocal() as db:
                # 变量说明：child_run_record 表示当前步骤使用的 child_run_record 值。
                child_run_record = db.get(Run, child_run_id)
                if child_run_record is not None and child_run_record.status not in {"completed", "failed", "stopped"}:
                    # 变量说明：status 表示当前对象或运行的状态。
                    child_run_record.status = "failed"
                    # 变量说明：error_message 表示当前步骤使用的 error_message 值。
                    child_run_record.error_message = "子 Agent 运行未能交给协调器执行"
                    # 变量说明：finished_at 表示finished_at 对应的时间信息。
                    child_run_record.finished_at = _utcnow()
                if child_run_record is not None:
                    self.coordinator.reconcile_delegated_child_terminal(
                        db,
                        child_run_record,
                        status="failed",
                        error=child_run_record.error_message,
                        error_code="delegate_execution_error",
                        runtime_binding=child_binding,
                    )
                db.commit()
            return self._blocked_result(
                task_id=delegation_id,
                child_run_id=child_run_id,
                agent=child,
                code="delegate_execution_error",
                message="子 Agent 运行未能交给协调器执行",
            )
        await self.coordinator.wait_for_run(
            child_run_id,
            timeout_seconds=settings.delegated_wait_timeout_seconds,
        )
        with database_module.SessionLocal() as db:
            # 变量说明：persisted_delegation 表示当前步骤使用的 persisted_delegation 值。
            persisted_delegation = db.get(DelegatedTask, delegation_id)
            if persisted_delegation is None:
                return self._blocked_result(
                    task_id=delegation_id,
                    child_run_id=child_run_id,
                    agent=child,
                    code="delegate_task_missing",
                    message="Delegated task record disappeared before its outcome could be read.",
                )
            # 变量说明：payload 表示跨层传递的数据载荷。
            payload = dict(persisted_delegation.result or {})
        return self._tool_result_from_payload(payload)
