from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import select

from src.persistence import database
from src.api.routes import list_session_delegations
from src.persistence.database import (
    Agent,
    AgentTool,
    Approval,
    BackgroundJob,
    Base,
    ChatMessage,
    DelegatedTask,
    ModelConnection,
    PlanStep,
    Run,
    RunEvent,
    Session,
    Workspace,
)
from src.agent import RunOutcome
from src.agent.engine import ModelToolCall, ModelTurn
from src.runs import service as run_service
from src.runs import delegation as delegation_module
from src.runs import runtime_factory as runtime_factory_module
from src.tasks.background import background_job_manager
from src.runs.service import RunCoordinator


@pytest.mark.asyncio
async def test_parent_wait_timeout_does_not_cancel_delegated_child() -> None:
    coordinator = RunCoordinator()
    release = asyncio.Event()
    completed = asyncio.Event()

    async def child() -> None:
        await release.wait()
        completed.set()

    child_task = asyncio.create_task(child())
    coordinator._tasks["child-run"] = child_task

    finished = await coordinator.wait_for_run("child-run", timeout_seconds=0.01)

    assert finished is False
    assert child_task.done() is False
    assert child_task.cancelled() is False
    release.set()
    await child_task
    assert completed.is_set()


def test_session_delegations_include_legacy_child_run_history(
    delegated_run: dict[str, str],
) -> None:
    """Pre-dedicated-table child runs must remain visible in the side panel."""

    with database.SessionLocal() as db:
        child_run = Run(
            session_id=delegated_run["session_id"],
            workspace_id=db.get(Run, delegated_run["run_id"]).workspace_id,
            agent_id=delegated_run["child_id"],
            status="completed",
            current_step=3,
            tool_calls=4,
        )
        db.add(child_run)
        db.flush()
        db.add_all([
            RunEvent(
                run_id=child_run.id,
                event_type="delegation_link",
                payload={
                    "team_task_id": "legacy-task-id",
                    "parent_run_id": delegated_run["run_id"],
                    "parent_session_id": delegated_run["session_id"],
                },
            ),
            RunEvent(
                run_id=child_run.id,
                event_type="runtime_snapshot",
                payload={
                    "output": "legacy child result",
                    "messages": [
                        {"role": "system", "content": "child instructions"},
                        {"role": "user", "content": "Inspect the old task history."},
                    ],
                },
            ),
        ])
        db.commit()

        rows = list_session_delegations(delegated_run["session_id"], db)

        assert len(rows) == 1
        assert rows[0].id == "legacy-task-id"
        assert rows[0].child_run_id == child_run.id
        assert rows[0].child_agent_id == delegated_run["child_id"]
        assert rows[0].description == "Inspect the old task history."
        assert rows[0].result["output"] == "legacy child result"
        assert rows[0].result["steps"] == 3
        assert rows[0].result["tool_calls"] == 4


@pytest.fixture()
def delegated_run(tmp_path: Path) -> dict[str, str]:
    database.configure_database(f"sqlite:///{(tmp_path / 'delegation.db').as_posix()}")
    database.init_db()
    parent_root = tmp_path / "parent-workspace"
    child_root = tmp_path / "child-profile-workspace"
    parent_root.mkdir()
    child_root.mkdir()
    with database.SessionLocal() as db:
        parent_workspace = Workspace(
            name="Parent",
            root_path=str(parent_root),
            validation_runtime={"kind": "docker", "image": "swebench/parent:latest"},
        )
        child_workspace = Workspace(name="Child profile", root_path=str(child_root))
        parent_connection = ModelConnection(
            name="Parent connection",
            provider="openai_compatible",
            base_url="https://parent.example/v1",
            secret_ref="parent-delegation-secret",
            default_model="parent-model",
            status="connected",
        )
        child_connection = ModelConnection(
            name="Child connection",
            provider="openai_compatible",
            base_url="https://child.example/v1",
            secret_ref="child-delegation-secret",
            default_model="child-default-model",
            status="connected",
        )
        db.add_all([parent_workspace, child_workspace, parent_connection, child_connection])
        db.flush()
        child = Agent(
            name="Research child",
            description="Inspect project files and report concise evidence.",
            system_prompt="Return only evidence from the delegated task.",
            workspace_id=child_workspace.id,
            model_connection_id=child_connection.id,
            model_id="child-model",
            thinking_level="high",
            enabled=True,
        )
        db.add(child)
        db.flush()
        # Deliberately include task. The child runtime must strip it even when
        # the child profile has it configured.
        db.add_all([
            AgentTool(agent_id=child.id, tool_id="read"),
            AgentTool(agent_id=child.id, tool_id="task"),
        ])
        session = Session(
            title="Delegation chat",
            workspace_id=parent_workspace.id,
            model_connection_id=parent_connection.id,
            model_id="parent-model",
            permission_mode="full",
        )
        db.add(session)
        db.flush()
        db.add(ChatMessage(session_id=session.id, role="user", content="请让研究子 Agent 检查项目。"))
        run = Run(session_id=session.id, workspace_id=parent_workspace.id, status="received")
        db.add(run)
        db.commit()
        yield {
            "run_id": run.id,
            "session_id": session.id,
            "child_id": child.id,
            "parent_root": str(parent_root),
            "child_root": str(child_root),
        }
    Base.metadata.drop_all(bind=database.engine)


def test_concurrent_task_calls_keep_separate_dag_bindings(delegated_run: dict[str, str]) -> None:
    delegate = run_service._SubagentTaskDelegate(
        parent_run_id=delegated_run["run_id"],
        parent_agent_id=database.DEFAULT_AGENT_ID,
        parent_binding={},
        parent_allowed_tool_names=[],
        permission_mode="full",
    )
    delegate.prepare_graph(
        [{"id": "shared", "task": "First graph", "agent_id": delegated_run["child_id"]}],
        call_id="tool-call-first",
    )
    delegate.prepare_graph(
        [{"id": "shared", "task": "Second graph", "agent_id": delegated_run["child_id"]}],
        call_id="tool-call-second",
    )

    assert set(delegate._plan_step_ids) == {"tool-call-first", "tool-call-second"}
    first_id = delegate._plan_step_ids["tool-call-first"]["shared"]
    second_id = delegate._plan_step_ids["tool-call-second"]["shared"]
    assert first_id != second_id
    delegate.block_step("shared", "first graph failed", graph_call_id="tool-call-first")

    with database.SessionLocal() as db:
        assert db.get(PlanStep, first_id).status == "failed"
        assert db.get(PlanStep, second_id).status == "pending"


@pytest.mark.asyncio
async def test_task_executes_child_with_frozen_limited_binding_and_returns_structured_result(
    delegated_run: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    parent_calls = 0
    child_calls = 0
    observed_child_tools: list[str] = []
    with database.SessionLocal() as db:
        session = db.get(Session, delegated_run["session_id"])
        assert session is not None
        session.use_memories = False
        child = db.get(Agent, delegated_run["child_id"])
        assert child is not None
        child.workflow_profile_id = "review"
        db.commit()

    async def parent_model(**kwargs):  # type: ignore[no-untyped-def]
        nonlocal parent_calls
        parent_calls += 1
        if parent_calls == 1:
            return ModelTurn(tool_calls=[ModelToolCall(
                "delegate-1",
                "task",
                {"task": "检查当前项目并给出证据", "agent_id": delegated_run["child_id"]},
            )])
        tool_messages = [item for item in kwargs["messages"] if item.get("role") == "tool"]
        tool_result = json.loads(tool_messages[-1]["content"])
        payload = json.loads(tool_result["content"])
        assert payload["status"] == "completed"
        assert payload["agent"]["id"] == delegated_run["child_id"]
        assert payload["output"] == "子 Agent 已完成检查。"
        return ModelTurn(content="主控已收到子 Agent 的检查结果。")

    async def child_model(**kwargs):  # type: ignore[no-untyped-def]
        nonlocal child_calls
        child_calls += 1
        observed_child_tools[:] = [item["function"]["name"] for item in kwargs["tools"]]
        if child_calls == 1:
            with database.SessionLocal() as db:
                task_record = db.scalar(select(DelegatedTask).where(
                    DelegatedTask.parent_run_id == delegated_run["run_id"]
                ))
                assert task_record is not None and task_record.child_run_id
                initial_snapshot = db.scalar(select(RunEvent).where(
                    RunEvent.run_id == task_record.child_run_id,
                    RunEvent.event_type == "runtime_snapshot",
                ))
                assert initial_snapshot is not None
                frozen_binding = initial_snapshot.payload["runtime_binding"]
                assert frozen_binding["delegation_id"] == task_record.id
                assert frozen_binding["workspace_root"] == delegated_run["parent_root"]
                assert frozen_binding["rendered_task"]
                assert "memory_snapshot" not in frozen_binding
                assert "memory_index" in frozen_binding
                assert frozen_binding["memory_index"] == ""
                assert frozen_binding["memories_enabled"] is True
                assert frozen_binding["use_memories"] is False
                assert "skill_instructions" in frozen_binding
                assert frozen_binding["workflow_profile_id"] == "review"
                assert frozen_binding["validation_runtime"] == {
                    "kind": "docker",
                    "image": "swebench/parent:latest",
                }
        # A malicious/buggy provider response that tries to recurse is still
        # rejected by the child registry; the second turn gives a real result.
        if child_calls == 1:
            assert "task" not in observed_child_tools
            return ModelTurn(tool_calls=[ModelToolCall(
                "nested-task",
                "task",
                {"task": "must not run", "agent_id": delegated_run["child_id"]},
            )])
        nested_results = [item for item in kwargs["messages"] if item.get("role") == "tool"]
        assert json.loads(nested_results[-1]["content"])["error_code"] == "tool_not_offered"
        return ModelTurn(content="子 Agent 已完成检查。")

    def fake_build_model_call(config, **_kwargs):  # type: ignore[no-untyped-def]
        if config.model_id == "parent-model":
            return parent_model
        if config.model_id == "child-model":
            return child_model
        raise AssertionError(f"unexpected model: {config.model_id}")

    monkeypatch.setattr(run_service, "build_model_call", fake_build_model_call)
    monkeypatch.setattr(runtime_factory_module, "build_model_call", fake_build_model_call)
    runtime, context = RunCoordinator._resolve_runtime(delegated_run["run_id"])
    assert delegated_run["child_id"] in context["agent_instructions"]

    outcome = await runtime.run(
        system_prompt=context["system_prompt"],
        agent_instructions=context["agent_instructions"],
        workspace_rules=context["workspace_rules"],
        recent_messages=context["recent_messages"],
        mode=context["mode"],
    )

    assert outcome.status == "completed", f"{outcome.error}; events={outcome.events}"
    assert outcome.output == "主控已收到子 Agent 的检查结果。"
    assert child_calls == 2
    assert observed_child_tools == ["read", "read_artifact"]

    with database.SessionLocal() as db:
        task = db.scalar(select(DelegatedTask))
        assert task is not None and task.status == "completed"
        assert task.child_agent_id == delegated_run["child_id"]
        assert task.result["binding"]["model_id"] == "child-model"
        assert task.result["binding"]["workflow_profile_id"] == "review"
        assert task.result["binding"]["allowed_tool_names"] == ["read", "read_artifact"]
        assert task.result["binding"]["recursive_task_enabled"] is False
        assert task.result["child_run_id"]
        child_run = db.get(Run, task.result["child_run_id"])
        assert child_run is not None and child_run.status == "completed"
        child_snapshot = db.scalar(select(RunEvent).where(
            RunEvent.run_id == child_run.id,
            RunEvent.event_type == "runtime_snapshot",
        ))
        assert child_snapshot is not None
        frozen = child_snapshot.payload["runtime_binding"]
        assert frozen["workspace_root"] == delegated_run["parent_root"]
        assert frozen["workspace_root"] != delegated_run["child_root"]
        assert frozen["model_id"] == "child-model"
        assert frozen["workflow_profile_id"] == "review"
        assert frozen["allowed_tool_names"] == ["read", "read_artifact"]
        assert frozen["validation_runtime"] == {
            "kind": "docker",
            "image": "swebench/parent:latest",
        }


@pytest.mark.asyncio
async def test_child_approval_stays_recoverable_then_syncs_task_without_duplicate_chat_message(
    delegated_run: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A child approval must not turn into a permanently blocked task."""

    child_calls = 0
    parent_calls = 0

    with database.SessionLocal() as db:
        db.add(AgentTool(agent_id=delegated_run["child_id"], tool_id="apply_patch"))
        db.commit()

    async def parent_model(**kwargs):  # type: ignore[no-untyped-def]
        nonlocal parent_calls
        parent_calls += 1
        if parent_calls == 1:
            return ModelTurn(tool_calls=[ModelToolCall(
                "delegate-awaits",
                "task",
                {"task": "write the delegated result", "agent_id": delegated_run["child_id"]},
            )])
        updates = [
            item for item in kwargs["messages"]
            if item.get("role") == "user"
            and str(item.get("content") or "").startswith("<delegated-task-update>")
        ]
        assert len(updates) == 1
        result_payload = json.loads(str(updates[0]["content"]).splitlines()[2])
        delegated_payload = json.loads(result_payload["content"])
        assert delegated_payload["status"] == "completed"
        assert delegated_payload["output"] == "child result after approval"
        return ModelTurn(content="parent summary after delegated child")

    async def child_model(**kwargs):  # type: ignore[no-untyped-def]
        nonlocal child_calls
        child_calls += 1
        if child_calls == 1:
            assert "apply_patch" in [item["function"]["name"] for item in kwargs["tools"]]
            return ModelTurn(tool_calls=[ModelToolCall(
                "child-write",
                "apply_patch",
                {
                    "patch": (
                        "*** Begin Patch\n"
                        "*** Add File: delegated.txt\n"
                        "+approved child content\n"
                        "*** End Patch"
                    )
                },
            )])
        return ModelTurn(content="child result after approval")

    def fake_build_model_call(config):  # type: ignore[no-untyped-def]
        return child_model if config.model_id == "child-model" else parent_model

    monkeypatch.setattr(run_service, "build_model_call", fake_build_model_call)
    parent_runtime, context = RunCoordinator._resolve_runtime(delegated_run["run_id"])
    # The session is full access for this test so the parent can dispatch
    # immediately.  Use request-approval for the child to exercise its own
    # resumable approval lifecycle; smart mode now allows routine writes.
    parent_runtime.tool_registry._task_delegate.permission_mode = "ask"  # type: ignore[attr-defined]
    parent_outcome = await parent_runtime.run(
        system_prompt=context["system_prompt"],
        agent_instructions=context["agent_instructions"],
        workspace_rules=context["workspace_rules"],
        recent_messages=context["recent_messages"],
        mode=context["mode"],
    )
    assert parent_outcome.status == "stopped"
    assert parent_outcome.stop_reason == "delegated_child_awaiting_approval"
    parent_outcome.runtime_binding = {
        **dict(context["runtime_binding"]),
        **parent_runtime.tool_registry.runtime_state(),
    }
    RunCoordinator._persist_outcome(delegated_run["run_id"], parent_outcome)

    with database.SessionLocal() as db:
        task = db.scalar(select(DelegatedTask))
        assert task is not None
        assert task.status == "in_progress"
        assert task.result["status"] == "awaiting_approval"
        child_run = db.get(Run, task.result["child_run_id"])
        assert child_run is not None
        assert child_run.status == "awaiting_approval"
        assert child_run.session_id == delegated_run["session_id"]
        approval = db.scalar(select(Approval).where(Approval.run_id == child_run.id))
        assert approval is not None and approval.status == "pending"
        task_id = task.id
        child_run_id = child_run.id
        approval.status = "approved"
        child_run.status = "received"
        db.commit()

    await run_service.coordinator._resume(child_run_id)

    # A terminal child must hand its idempotent task result back to the actual
    # parent runtime.  Poll because the continuation is deliberately launched
    # asynchronously only after the child outcome transaction commits.
    for _ in range(100):
        with database.SessionLocal() as db:
            parent = db.get(Run, delegated_run["run_id"])
            if parent is not None and parent.status == "completed":
                break
        await asyncio.sleep(0.01)
    else:
        with database.SessionLocal() as db:
            parent = db.get(Run, delegated_run["run_id"])
            parent_events = list(db.scalars(
                select(RunEvent)
                .where(RunEvent.run_id == delegated_run["run_id"])
                .order_by(RunEvent.created_at.asc(), RunEvent.id.asc())
            ))
            pytest.fail(
                "the parent run did not resume after the child completed; "
                f"status={getattr(parent, 'status', None)}; "
                f"stop_reason={getattr(parent, 'stop_reason', None)}; "
                f"events={[(item.event_type, item.payload) for item in parent_events[-8:]]}"
            )

    with database.SessionLocal() as db:
        task = db.get(DelegatedTask, task_id)
        child_run = db.get(Run, child_run_id)
        parent = db.get(Run, delegated_run["run_id"])
        assert task is not None and child_run is not None and parent is not None
        assert task.status == "completed"
        assert task.result["status"] == "completed"
        assert child_run.status == "completed"
        assert parent.status == "completed"
        session_messages = list(db.scalars(select(ChatMessage).where(
            ChatMessage.session_id == delegated_run["session_id"],
        )))
        # The child result belongs to DelegatedTask and its own Run;
        # only the parent coordinator may write a user-visible chat reply.
        assert not [item for item in session_messages if item.extra.get("delegated_child") is True]
        assert [item.content for item in session_messages if item.extra.get("run_id") == parent.id] == [
            "parent summary after delegated child",
        ]
        parent_events = list(db.scalars(select(RunEvent).where(
            RunEvent.run_id == delegated_run["run_id"],
        )))
        assert {event.event_type for event in parent_events} >= {
            "delegated_child_awaiting_approval",
            "delegated_child_completed",
            "delegated_child_continuation_queued",
        }
        result_before = dict(task.result)
        updated_at_before = task.updated_at
        visible_count_before = len(list(db.scalars(select(ChatMessage).where(
            ChatMessage.session_id == delegated_run["session_id"],
        ))))
        latest_snapshot = db.scalar(select(RunEvent).where(
            RunEvent.run_id == child_run_id,
            RunEvent.event_type == "runtime_snapshot",
        ).order_by(RunEvent.created_at.desc(), RunEvent.id.desc()))
        assert latest_snapshot is not None
        duplicate_outcome = RunCoordinator._outcome_from_snapshot(latest_snapshot.payload)

    RunCoordinator._persist_outcome(child_run_id, duplicate_outcome)

    with database.SessionLocal() as db:
        task = db.get(DelegatedTask, task_id)
        assert task is not None and task.result == result_before
        assert task.updated_at == updated_at_before
        assert len(list(db.scalars(select(ChatMessage).where(
            ChatMessage.session_id == delegated_run["session_id"],
        )))) == visible_count_before


def test_restart_recovery_settles_active_delegated_child_and_keeps_binding(
    delegated_run: dict[str, str],
) -> None:
    """A process restart cannot strand a delegated child as in_progress."""

    with database.SessionLocal() as db:
        parent = db.get(Run, delegated_run["run_id"])
        assert parent is not None
        parent.status = "stopped"
        parent.stop_reason = "delegated_child_awaiting_approval"
        child_run = Run(
            session_id=delegated_run["session_id"],
            workspace_id=None,
            agent_id=delegated_run["child_id"],
            status="acting",
        )
        db.add(child_run)
        db.flush()
        task = DelegatedTask(
            parent_run_id=delegated_run["run_id"],
            parent_session_id=delegated_run["session_id"],
            child_run_id=child_run.id,
            child_agent_id=delegated_run["child_id"],
            title="restart child",
            description="restart child",
            status="in_progress",
            idempotency_key=f"restart-delegation:{child_run.id}",
            result={
                "child_run_id": child_run.id,
                "status": "in_progress",
                "binding": {
                    "model_connection_id": "child-connection",
                    "provider": "openai_compatible",
                    "model_id": "child-model",
                    "thinking_level": "high",
                    "permission_mode": "smart",
                    "allowed_tool_names": ["read"],
                    "skill_ids": [],
                },
            },
        )
        db.add(task)
        db.flush()
        db.add(RunEvent(
            run_id=child_run.id,
            event_type="delegation_link",
            payload={
                "delegation_id": task.id,
                "parent_run_id": delegated_run["run_id"],
                "parent_agent_id": "",
                "parent_session_id": delegated_run["session_id"],
            },
        ))
        db.commit()
        child_run_id, task_id = child_run.id, task.id

    queued_parent_run_ids = RunCoordinator.reconcile_interrupted_runs()
    assert queued_parent_run_ids == [delegated_run["run_id"]]

    with database.SessionLocal() as db:
        child_run = db.get(Run, child_run_id)
        task = db.get(DelegatedTask, task_id)
        parent = db.get(Run, delegated_run["run_id"])
        assert child_run is not None and child_run.status == "stopped"
        assert child_run.stop_reason == "interrupted_restart"
        assert task is not None and task.status == "blocked"
        assert parent is not None and parent.status == "received"
        assert task.result["status"] == "stopped"
        assert task.result["binding"]["model_id"] == "child-model"


@pytest.mark.asyncio
async def test_child_failure_blocks_once_and_repeated_delegate_call_is_idempotent(
    delegated_run: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    async def child_model(**_kwargs):  # type: ignore[no-untyped-def]
        raise RuntimeError("child provider failed")

    monkeypatch.setattr(run_service, "build_model_call", lambda _config: child_model)
    parent_runtime, _context = RunCoordinator._resolve_runtime(delegated_run["run_id"])
    delegate = parent_runtime.tool_registry._task_delegate  # type: ignore[attr-defined]
    first = await delegate("fail once", agent_id=delegated_run["child_id"], call_id="failed-child")
    second = await delegate("fail once", agent_id=delegated_run["child_id"], call_id="failed-child")
    first_payload = json.loads(first.content)
    second_payload = json.loads(second.content)
    assert first_payload["status"] == second_payload["status"] == "failed"
    assert first.error_code == second.error_code == "delegate_child_failed"

    with database.SessionLocal() as db:
        task = db.scalar(select(DelegatedTask))
        assert task is not None and task.status == "blocked"
        assert task.result["status"] == "failed"
        child_run = db.get(Run, task.child_run_id)
        assert child_run is not None and child_run.turn_id is None
        assert not list(db.scalars(select(ChatMessage).where(
            ChatMessage.session_id == delegated_run["session_id"],
            ChatMessage.role == "assistant",
        )))
        assert len(list(db.scalars(select(RunEvent).where(
            RunEvent.run_id == delegated_run["run_id"],
            RunEvent.event_type == "delegated_child_failed",
        )))) == 1


@pytest.mark.asyncio
async def test_child_timeout_is_limited_to_the_parent_remaining_budget(
    delegated_run: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(run_service, "build_model_call", lambda _config: (lambda **_kwargs: None))
    parent_runtime, _context = RunCoordinator._resolve_runtime(delegated_run["run_id"])
    delegate = parent_runtime.tool_registry._task_delegate  # type: ignore[attr-defined]
    with database.SessionLocal() as db:
        parent = db.get(Run, delegated_run["run_id"])
        assert parent is not None
        parent.started_at = datetime.now(timezone.utc) - timedelta(seconds=7)
        db.commit()

    monkeypatch.setattr(run_service.settings, "max_run_seconds", 10.0)
    await delegate("bounded", agent_id=delegated_run["child_id"], call_id="budget-child")
    with database.SessionLocal() as db:
        task = db.scalar(select(DelegatedTask))
        assert task is not None and task.child_run_id
        snapshot = db.scalar(select(RunEvent).where(
            RunEvent.run_id == task.child_run_id,
            RunEvent.event_type == "runtime_snapshot",
        ).order_by(RunEvent.created_at.asc()))
        assert snapshot is not None
        child_limit = snapshot.payload["runtime_binding"]["max_run_seconds"]
        assert child_limit is not None
        assert 0 < child_limit < 5


@pytest.mark.asyncio
@pytest.mark.xfail(reason="background subprocess event delivery is unavailable in this test environment", strict=False)
async def test_delegated_child_uses_durable_background_job_and_must_observe_it(
    delegated_run: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    with database.SessionLocal() as db:
        db.add_all([
            AgentTool(agent_id=delegated_run["child_id"], tool_id="background_run"),
            AgentTool(agent_id=delegated_run["child_id"], tool_id="check_background"),
        ])
        db.commit()

    parent_calls = 0
    child_calls = 0

    async def parent_model(**_kwargs):  # type: ignore[no-untyped-def]
        nonlocal parent_calls
        parent_calls += 1
        if parent_calls == 1:
            return ModelTurn(tool_calls=[ModelToolCall(
                "delegate-background",
                "task",
                {"task": "run durable background work", "agent_id": delegated_run["child_id"]},
            )])
        return ModelTurn(content="child background work verified")

    async def child_model(**kwargs):  # type: ignore[no-untyped-def]
        nonlocal child_calls
        child_calls += 1
        if child_calls == 1:
            tool_names = {item["function"]["name"] for item in kwargs["tools"]}
            assert {"background_run", "check_background"}.issubset(tool_names)
            return ModelTurn(tool_calls=[ModelToolCall(
                "child-background-start",
                "background_run",
                {
                    "command": "python -c \"import time; time.sleep(5); print('child-durable-result')\"",
                    "shell": "command",
                    "timeout": 30,
                },
            )])
        if child_calls == 2:
            return ModelTurn(content="premature child completion")
        assert any(
            "<background-task-events>" in str(item.get("content") or "")
            and "child-durable-result" in str(item.get("content") or "")
            for item in kwargs["messages"]
        )
        return ModelTurn(content="child durable background result verified")

    def fake_build_model_call(config):  # type: ignore[no-untyped-def]
        return parent_model if config.model_id == "parent-model" else child_model

    monkeypatch.setattr(run_service, "build_model_call", fake_build_model_call)
    run_service.coordinator._event_loop = asyncio.get_running_loop()
    background_job_manager.set_terminal_listener(
        run_service.coordinator.notify_background_terminal
    )
    try:
        runtime, context = RunCoordinator._resolve_runtime(delegated_run["run_id"])
        RunCoordinator._install_completion_verifier(runtime, context)
        outcome = await runtime.run(
            system_prompt=context["system_prompt"],
            agent_instructions=context["agent_instructions"],
            workspace_rules=context["workspace_rules"],
            recent_messages=context["recent_messages"],
            mode=context["mode"],
        )

        assert outcome.status == "completed"
        outcome.runtime_binding = {
            **dict(context["runtime_binding"]),
            **runtime.tool_registry.runtime_state(),
        }
        RunCoordinator._persist_outcome(delegated_run["run_id"], outcome)
        for _ in range(300):
            with database.SessionLocal() as db:
                parent = db.get(Run, delegated_run["run_id"])
                if parent is not None and parent.status == "completed":
                    break
            await asyncio.sleep(0.01)
        else:
            pytest.fail("event-driven child/background continuation did not complete")
    finally:
        background_job_manager.set_terminal_listener(None)
        run_service.coordinator._event_loop = None

    assert child_calls == 2
    with database.SessionLocal() as db:
        job = db.scalar(select(BackgroundJob))
        task = db.scalar(select(DelegatedTask))
        assert job is not None and job.status == "completed"
        assert task is not None and task.status == "completed" and job.run_id == task.child_run_id
