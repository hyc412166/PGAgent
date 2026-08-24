from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import asyncio
import json
from pathlib import Path

import pytest

from app import database
from app.database import Base, DEFAULT_AGENT_ID, DEFAULT_WORKSPACE_ID, DurableTask, PlanStep, Session
from app.services.task_graph import TaskGraphToolStore, claim_step, dependency_map, ready_steps, settle_step, upsert_delegated_graph
from app.services.task_state import sync_todos_for_run
from app.services.turn_delivery import stage_user_turn
from app.tools import builtins
from app.tools import create_default_registry
from app.tools.sandbox import WorkspaceSandbox
from app.tools.types import ToolResult


@pytest.fixture()
def graph_db(tmp_path: Path):
    database.configure_database(f"sqlite:///{(tmp_path / 'task-graph.db').as_posix()}")
    database.init_db()
    yield
    Base.metadata.drop_all(bind=database.engine)


def _create_run() -> str:
    with database.SessionLocal() as db:
        session = Session(
            title="Parallel graph",
            workspace_id=DEFAULT_WORKSPACE_ID,
            agent_id=DEFAULT_AGENT_ID,
        )
        db.add(session)
        db.flush()
        _turn, _message, run = stage_user_turn(
            db,
            session_id=session.id,
            workspace_id=DEFAULT_WORKSPACE_ID,
            agent_id=DEFAULT_AGENT_ID,
            content="Run two independent investigations and then integrate them.",
            mode="auto",
            client_message_id=None,
        )
        db.commit()
        return run.id


def test_dependency_graph_unlocks_parallel_steps_in_waves(graph_db) -> None:
    run_id = _create_run()
    sync_todos_for_run(run_id, [
        {
            "id": "research-a",
            "content": "Research A",
            "status": "pending",
            "executor_kind": "subagent",
            "agent_id": DEFAULT_AGENT_ID,
        },
        {
            "id": "research-b",
            "content": "Research B",
            "status": "pending",
            "executor_kind": "subagent",
            "agent_id": DEFAULT_AGENT_ID,
        },
        {
            "id": "integrate",
            "content": "Integrate both results",
            "status": "pending",
            "depends_on": ["research-a", "research-b"],
        },
    ])

    with database.SessionLocal() as db:
        run = db.get(database.Run, run_id)
        steps = {step.external_id: step for step in db.query(PlanStep).filter_by(task_id=run.task_id)}
        assert dependency_map(db, run.task_id) == {
            "integrate": ["research-a", "research-b"],
            "research-a": [],
            "research-b": [],
        }
        assert [step.external_id for step in ready_steps(db, run.task_id)] == ["research-a", "research-b"]
        task_id = run.task_id
        first_id = steps["research-a"].id
        second_id = steps["research-b"].id
        integrate_id = steps["integrate"].id

    assert claim_step(first_id, assigned_run_id=run_id, assigned_agent_id=DEFAULT_AGENT_ID)
    assert settle_step(first_id, status="completed", result="A result", assigned_run_id=run_id)
    with database.SessionLocal() as db:
        assert [step.external_id for step in ready_steps(db, task_id)] == ["research-b"]

    assert claim_step(second_id, assigned_run_id=run_id, assigned_agent_id=DEFAULT_AGENT_ID)
    assert settle_step(second_id, status="completed", result="B result", assigned_run_id=run_id)
    with database.SessionLocal() as db:
        assert [step.external_id for step in ready_steps(db, task_id)] == ["integrate"]

    assert claim_step(integrate_id, assigned_run_id=run_id)
    assert settle_step(integrate_id, status="completed", result="integrated", assigned_run_id=run_id)
    with database.SessionLocal() as db:
        task = db.get(DurableTask, task_id)
        assert task is not None and task.status == "completed"


def test_atomic_claim_allows_only_one_worker(graph_db) -> None:
    run_id = _create_run()
    sync_todos_for_run(run_id, [
        {"id": "only-once", "content": "Claim once", "status": "pending"},
    ])
    with database.SessionLocal() as db:
        run = db.get(database.Run, run_id)
        step_id = db.query(PlanStep).filter_by(task_id=run.task_id).one().id

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(lambda _: claim_step(step_id, assigned_run_id=run_id), range(2)))
    assert sorted(outcomes) == [False, True]


def test_cycle_is_rejected_before_plan_is_persisted(graph_db) -> None:
    run_id = _create_run()
    with pytest.raises(ValueError, match="cycle"):
        sync_todos_for_run(run_id, [
            {"id": "a", "content": "A", "status": "pending", "depends_on": ["b"]},
            {"id": "b", "content": "B", "status": "pending", "depends_on": ["a"]},
        ])
    with database.SessionLocal() as db:
        run = db.get(database.Run, run_id)
        assert run is not None and run.task_id is None


def test_compatibility_task_tools_share_the_canonical_sqlite_graph(graph_db, tmp_path: Path) -> None:
    run_id = _create_run()
    store = TaskGraphToolStore(run_id=run_id)
    registry = create_default_registry(
        str(tmp_path),
        allowed_tool_names=["task_create", "task_update", "task_list", "claim_task"],
        permission_mode="full",
        task_store=store,
    )
    first = registry.execute("task_create", {"subject": "Research A"})
    second = registry.execute("task_create", {"subject": "Integrate"})
    first_id = json.loads(first.content)["id"]
    second_id = json.loads(second.content)["id"]

    blocked = registry.execute("task_update", {
        "task_id": second_id,
        "add_blocked_by": [first_id],
    })
    early_claim = registry.execute("claim_task", {"task_id": second_id, "owner": "lead"})
    first_claim = registry.execute("claim_task", {"task_id": first_id, "owner": "researcher"})
    completed = registry.execute("task_update", {
        "task_id": first_id,
        "status": "completed",
        "output": "evidence",
    })
    second_claim = registry.execute("claim_task", {"task_id": second_id, "owner": "lead"})
    listed = json.loads(registry.execute("task_list", {}).content)

    assert first.ok and second.ok and blocked.ok and first_claim.ok and completed.ok and second_claim.ok
    assert not early_claim.ok and early_claim.error_code == "task_blocked"
    assert listed[0]["owner"] == "researcher"
    assert listed[0]["output"] == "evidence"
    assert listed[1]["blockedBy"] == [first_id]
    assert not (tmp_path / ".pgagent" / "tasks.json").exists()


@pytest.mark.asyncio
async def test_delegate_batch_runs_ready_nodes_concurrently_then_dependency(graph_db, tmp_path: Path) -> None:
    class Delegate:
        def __init__(self) -> None:
            self.active = 0
            self.max_active = 0
            self.finished: set[str] = set()
            self.started_after: dict[str, set[str]] = {}
            self.prepared: list[dict[str, object]] = []
            self.call_ids: dict[str, str | None] = {}
            self.graph_call_ids: dict[str, str | None] = {}
            self.prepared_call_id: str | None = None

        def prepare_graph(self, specs, *, call_id=None):  # type: ignore[no-untyped-def]
            self.prepared = [dict(item) for item in specs]
            self.prepared_call_id = call_id

        async def __call__(
            self,
            task: str,
            *,
            agent_id: str,
            call_id: str | None,
            plan_step_external_id: str,
            graph_call_id: str | None,
        ) -> ToolResult:
            self.started_after[plan_step_external_id] = set(self.finished)
            self.call_ids[plan_step_external_id] = call_id
            self.graph_call_ids[plan_step_external_id] = graph_call_id
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            await asyncio.sleep(0.02)
            self.active -= 1
            self.finished.add(plan_step_external_id)
            return ToolResult(
                "task",
                True,
                json.dumps({"status": "completed", "step": plan_step_external_id}),
                metadata={"plan_step_external_id": plan_step_external_id},
            )

    delegate = Delegate()
    result = await builtins.delegate_task_async(
        WorkspaceSandbox(tmp_path),
        tasks=[
            {"id": "a", "task": "A", "agent_id": "agent-a"},
            {"id": "b", "task": "B", "agent_id": "agent-b"},
            {"id": "c", "task": "C", "agent_id": "agent-c", "depends_on": ["a", "b"]},
        ],
        delegate=delegate,
        call_id="graph-call",
    )

    payload = json.loads(result.content)
    assert result.ok
    assert payload["execution_waves"] == [["a", "b"], ["c"]]
    assert payload["parallel"] is True
    assert delegate.max_active == 2
    assert delegate.started_after["c"] == {"a", "b"}
    assert [item["id"] for item in delegate.prepared] == ["a", "b", "c"]
    assert delegate.prepared_call_id == "graph-call"
    assert delegate.call_ids == {
        "a": "graph-call:a",
        "b": "graph-call:b",
        "c": "graph-call:c",
    }
    assert set(delegate.graph_call_ids.values()) == {"graph-call"}


@pytest.mark.asyncio
async def test_delegate_preflight_failure_settles_failed_and_downstream_steps(
    graph_db, tmp_path: Path
) -> None:
    class Delegate:
        def __init__(self) -> None:
            self.blocked: dict[str, str] = {}

        def prepare_graph(self, _specs):  # type: ignore[no-untyped-def]
            return None

        def block_step(self, external_id: str, reason: str) -> None:
            self.blocked[external_id] = reason

        async def __call__(self, task: str, **_kwargs) -> ToolResult:  # type: ignore[no-untyped-def]
            return ToolResult("task", False, f"cannot start {task}", error_code="delegate_configuration_invalid")

    delegate = Delegate()
    result = await builtins.delegate_task_async(
        WorkspaceSandbox(tmp_path),
        tasks=[
            {"id": "a", "task": "A", "agent_id": "agent-a"},
            {"id": "b", "task": "B", "agent_id": "agent-b", "depends_on": ["a"]},
        ],
        delegate=delegate,
        call_id="failed-graph",
    )

    assert not result.ok
    assert set(delegate.blocked) == {"a", "b"}
    assert "delegate_configuration_invalid" in delegate.blocked["a"]
    assert "prerequisite task failed" in delegate.blocked["b"]


@pytest.mark.asyncio
async def test_waiting_prerequisite_pauses_downstream_without_failing_it(graph_db, tmp_path: Path) -> None:
    class Delegate:
        def __init__(self) -> None:
            self.called: list[str] = []
            self.blocked: list[str] = []

        def prepare_graph(self, _specs, *, call_id=None):  # type: ignore[no-untyped-def]
            return None

        def block_step(self, external_id: str, _reason: str, *, graph_call_id=None) -> None:
            self.blocked.append(external_id)

        async def __call__(self, _task: str, *, plan_step_external_id: str, **_kwargs) -> ToolResult:
            self.called.append(plan_step_external_id)
            return ToolResult(
                "task",
                False,
                json.dumps({"status": "waiting_background"}),
                error_code="delegate_child_waiting_event",
                metadata={
                    "delegated_child_awaiting_approval": True,
                    "delegated_child_waiting_event": True,
                },
            )

    delegate = Delegate()
    result = await builtins.delegate_task_async(
        WorkspaceSandbox(tmp_path),
        tasks=[
            {"id": "download", "task": "Download", "agent_id": "agent-a"},
            {"id": "consume", "task": "Consume", "agent_id": "agent-b", "depends_on": ["download"]},
        ],
        delegate=delegate,
        call_id="waiting-graph",
    )

    payload = json.loads(result.content)
    assert not result.ok
    assert payload["status"] == "waiting_background"
    assert delegate.called == ["download"]
    assert delegate.blocked == []


def test_replaying_completed_delegated_graph_keeps_task_completed(graph_db) -> None:
    run_id = _create_run()
    specs = [{"id": "done", "task": "Already done", "agent_id": DEFAULT_AGENT_ID}]
    step_id = upsert_delegated_graph(run_id, specs)["done"]
    assert claim_step(step_id, assigned_run_id=run_id)
    assert settle_step(step_id, status="completed", result="accepted", assigned_run_id=run_id)

    replayed = upsert_delegated_graph(run_id, specs)

    assert replayed["done"] == step_id
    with database.SessionLocal() as db:
        run = db.get(database.Run, run_id)
        task = db.get(DurableTask, run.task_id)
        assert task.status == "completed"
        assert task.active_step_id is None
