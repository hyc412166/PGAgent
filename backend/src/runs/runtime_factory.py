"""Assemble one AgentRuntime from an already resolved durable run context."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from src.agent import AgentRuntime, RuntimeConfig
from src.agents.collaboration import TeamToolStore
from src.artifacts.storage import ArtifactToolStore
from src.config import settings
from src.context import ContextManager, FilesystemArtifactStore
from src.memory.service import MemoryToolStore
from src.tasks.background import BackgroundJobToolStore
from src.tasks.graph import TaskGraphToolStore
from src.tasks.state import sync_todos_for_run, task_checkpoint_for_run
from src.tools import create_default_registry
from src.tools.registry import ToolRegistry

from .dependencies import build_model_call


class CoordinatorRuntimePort(Protocol):
    def register_tool_canceller(self, run_id: str, callback: Any) -> None: ...

    @classmethod
    def _event_sink(cls, run_id: str) -> Any: ...

    @classmethod
    def _stream_sink(cls, run_id: str) -> Any: ...


@dataclass(slots=True)
class RuntimeAssembly:
    runtime: AgentRuntime
    background_store: BackgroundJobToolStore
    registry: ToolRegistry


class RunRuntimeFactory:
    """Create stores, tools and AgentRuntime without owning run persistence."""

    def create(
        self,
        *,
        run_id: str,
        context: dict[str, Any],
        coordinator: CoordinatorRuntimePort,
        runtime_type: type[AgentRuntime] = AgentRuntime,
    ) -> RuntimeAssembly:
        binding = dict(context["runtime_binding"])
        delegated = bool(binding.get("delegation_version"))
        session_id = context.get("session_id")

        task_delegate = None
        if not delegated:
            from .delegation import _SubagentTaskDelegate

            task_delegate = _SubagentTaskDelegate(
                coordinator=coordinator,
                parent_run_id=run_id,
                parent_agent_id=str(context["agent_id"]),
                parent_binding=binding,
                parent_allowed_tool_names=list(context["allowed_tool_names"]),
                permission_mode=str(context["permission_mode"]),
            )

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

        delegated_teammate_id = str(binding.get("teammate_id") or "")
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
        task_store = None if delegated else TaskGraphToolStore(run_id=run_id)
        runtime_artifact_store = FilesystemArtifactStore(
            settings.data_dir / "artifacts" / str(session_id or run_id)
        )
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
            background_store=background_store,
            team_store=team_store,
            task_store=task_store,
            task_delegate=task_delegate,
            coding_state=binding.get("coding_state"),
            active_builtin_tool_names=binding.get("builtin_active_tools"),
        )
        coordinator.register_tool_canceller(run_id, registry.cancel_active)
        runtime = runtime_type(
            model_call=build_model_call(context["provider"]),
            tool_registry=registry,
            context_manager=ContextManager(max_tokens=settings.context_limit_tokens),
            artifact_store=runtime_artifact_store,
            event_sink=type(coordinator)._event_sink(run_id),
            stream_sink=(
                type(coordinator)._stream_sink(run_id)
                if context.get("stream_enabled", True)
                else None
            ),
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
