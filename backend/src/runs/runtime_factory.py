"""Assemble one AgentRuntime from an already resolved durable run context."""
# 文件职责：负责运行创建、恢复、流式传输和生命周期中的 runtime_factory 子模块。
# 逻辑关系：上层通过 runs/runtime_factory.py 使用本模块；本模块把处理结果交给同领域服务、持久化层或 API 响应层。

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from src.agent import AgentRuntime, RuntimeConfig
from src.agents.collaboration import TeamToolStore
from src.artifacts.storage import ArtifactToolStore
from src.attachments.storage import AttachmentToolStore
from src.config import settings
from src.context import ContextManager, FilesystemArtifactStore
from src.memory.service import MemoryToolStore
from src.model.gateway import bind_attachment_store
from src.tasks.background import BackgroundJobToolStore
from src.tasks.graph import TaskGraphToolStore
from src.tasks.state import sync_todos_for_run, task_checkpoint_for_run
from src.tools import create_default_registry
from src.tools.registry import ToolRegistry

from .dependencies import build_model_call


# 类职责：定义 CoordinatorRuntimePort 在本领域中的数据与行为。
# 继承关系：复用基类提供的契约，并向调用方暴露本类声明的字段和方法。
class CoordinatorRuntimePort(Protocol):
    # 函数职责：完成 register_tool_canceller 对应的业务处理。
    # 参数关系：run_id 表示当前运行标识；callback 表示当前步骤使用的 callback 值。
    # 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
    def register_tool_canceller(self, run_id: str, callback: Any) -> None: ...

    # 函数职责：完成 event_sink 对应的业务处理。
    # 参数关系：run_id 表示当前运行标识。
    # 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
    @classmethod
    def _event_sink(cls, run_id: str) -> Any: ...

    # 函数职责：流式传输 sink 对应的数据或流程。
    # 参数关系：run_id 表示当前运行标识。
    # 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
    @classmethod
    def _stream_sink(cls, run_id: str) -> Any: ...


# 类职责：定义 RuntimeAssembly 在本领域中的数据与行为。
@dataclass(slots=True)
class RuntimeAssembly:
    # 变量说明：runtime 表示当前步骤使用的 runtime 值。
    runtime: AgentRuntime
    # 变量说明：background_store 表示当前步骤使用的 background_store 值。
    background_store: BackgroundJobToolStore
    # 变量说明：registry 表示当前步骤使用的 registry 值。
    registry: ToolRegistry


# 类职责：定义 RunRuntimeFactory 在本领域中的数据与行为。
class RunRuntimeFactory:
    """Create stores, tools and AgentRuntime without owning run persistence."""

    # 函数职责：完成 create 对应的业务处理。
    # 参数关系：run_id 表示当前运行标识；context 表示当前步骤使用的 context 值；coordinator 表示当前步骤使用的 coordinator 值；runtime_type 表示当前步骤使用的 runtime_type 值。
    # 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
    def create(
        self,
        *,
        run_id: str,
        context: dict[str, Any],
        coordinator: CoordinatorRuntimePort,
        runtime_type: type[AgentRuntime] = AgentRuntime,
    ) -> RuntimeAssembly:
        # 变量说明：binding 表示当前步骤使用的 binding 值。
        binding = dict(context["runtime_binding"])
        # 变量说明：delegated 表示当前步骤使用的 delegated 值。
        delegated = bool(binding.get("delegation_version"))
        # 变量说明：session_id 表示所属会话标识。
        session_id = context.get("session_id")

        # 变量说明：task_delegate 表示当前步骤使用的 task_delegate 值。
        task_delegate = None
        if not delegated:
            from .delegation import _SubagentTaskDelegate

            # 变量说明：task_delegate 表示当前步骤使用的 task_delegate 值。
            task_delegate = _SubagentTaskDelegate(
                coordinator=coordinator,
                parent_run_id=run_id,
                parent_agent_id=str(context["agent_id"]),
                parent_binding=binding,
                parent_allowed_tool_names=list(context["allowed_tool_names"]),
                permission_mode=str(context["permission_mode"]),
            )

        # 变量说明：background_store 表示当前步骤使用的 background_store 值。
        background_store = BackgroundJobToolStore(
            run_id=run_id,
            workspace_id=str(context["workspace_id"]),
            session_id=str(session_id) if session_id else None,
            workspace_root=context["workspace_root"],
            include_session_jobs=not delegated,
        )
        background_store.track_terminal_deliveries(
            context.get("terminal_background_job_ids") or ()
        )

        # 变量说明：delegated_teammate_id 表示delegated_teammate 对象的唯一标识。
        delegated_teammate_id = str(binding.get("teammate_id") or "")
        # 变量说明：team_store 表示当前步骤使用的 team_store 值。
        team_store = (
            TeamToolStore(
                run_id=str(binding.get("parent_run_id") or run_id),
                session_id=str(session_id) if session_id else None,
                workspace_root=context["workspace_root"],
                actor_worker_id=delegated_teammate_id or None,
            )
            if not delegated or delegated_teammate_id
            else None
        )
        # 变量说明：task_store 表示当前步骤使用的 task_store 值。
        task_store = None if delegated else TaskGraphToolStore(run_id=run_id)
        # 变量说明：runtime_artifact_store 表示当前步骤使用的 runtime_artifact_store 值。
        runtime_artifact_store = FilesystemArtifactStore(
            settings.data_dir / "artifacts" / str(session_id or run_id)
        )
        # 变量说明：attachment_store 表示当前步骤使用的 attachment_store 值。
        attachment_store = (
            AttachmentToolStore(str(session_id), runtime_artifact_store)
            if session_id else None
        )
        # 变量说明：registry 表示当前步骤使用的 registry 值。
        registry = create_default_registry(
            context["workspace_root"],
            allowed_tool_names=context["allowed_tool_names"],
            permission_mode=context["permission_mode"],
            skill_instructions=context["skill_instructions"],
            todo_state=context["todo_state"],
            todo_change_sink=(
                (lambda todos, key=run_id: sync_todos_for_run(key, todos))
                if not delegated
                else None
            ),
            memory_store=(
                MemoryToolStore(
                    workspace_id=str(context["workspace_id"]),
                    session_id=(
                        str(context["memory_session_id"])
                        if context.get("memory_session_id")
                        else (
                            None
                            if "memory_session_id" in context
                            else (str(session_id) if session_id else None)
                        )
                    ),
                )
                if context.get("memory_use_enabled")
                else None
            ),
            artifact_store=ArtifactToolStore(runtime_artifact_store),
            attachment_store=attachment_store,
            background_store=background_store,
            team_store=team_store,
            task_store=task_store,
            task_delegate=task_delegate,
            workflow_profile_id=str(binding.get("workflow_profile_id") or "auto"),
            workflow_evidence_state=binding.get("workflow_evidence_state"),
            coding_state=binding.get("coding_state"),
            validation_runtime=binding.get("validation_runtime"),
            active_builtin_tool_names=binding.get("builtin_active_tools"),
            expose_legacy_tools=bool(context.get("expose_legacy_tools")),
        )
        coordinator.register_tool_canceller(run_id, registry.cancel_active)
        # 变量说明：runtime 表示当前步骤使用的 runtime 值。
        runtime = runtime_type(
            model_call=bind_attachment_store(
                build_model_call(context["provider"]),
                attachment_store,
            ),
            tool_registry=registry,
            context_manager=ContextManager(max_tokens=settings.context_limit_tokens),
            artifact_store=runtime_artifact_store,
            event_sink=type(coordinator)._event_sink(run_id),
            stream_sink=(
                type(coordinator)._stream_sink(run_id)
                if context.get("stream_enabled", True)
                else None
            ),
            background_wait_provider=background_store.completion_boundary,
            config=RuntimeConfig(
                max_steps=settings.max_steps,
                max_tool_calls=settings.max_tool_calls,
                identical_call_limit=settings.max_identical_calls,
                no_progress_limit=settings.no_progress_limit,
                context_compaction_threshold_tokens=settings.compact_threshold_tokens,
                model_timeout_seconds=float(
                    context.get("model_timeout_seconds") or settings.model_timeout_seconds
                ),
                max_run_seconds=context["max_run_seconds"],
                max_task_tokens=settings.max_task_tokens,
            ),
            task_state_provider=(
                (lambda key=run_id: task_checkpoint_for_run(key))
                if not delegated
                else None
            ),
        )
        return RuntimeAssembly(
            runtime=runtime,
            background_store=background_store,
            registry=registry,
        )
