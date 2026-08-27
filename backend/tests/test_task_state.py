from __future__ import annotations

from pathlib import Path

import pytest

from app import database
from app.database import (
    Base,
    DEFAULT_AGENT_ID,
    DEFAULT_WORKSPACE_ID,
    DurableTask,
    PlanStep,
    Run,
    Session,
    configure_database,
    init_db,
)
from app.services.run_service import RunCoordinator
from app.services.task_state import (
    bind_recovery_task,
    is_continuation_request,
    recovery_prompt,
    sync_todos_for_run,
    task_checkpoint_for_run,
    todo_state_for_run,
    transition_run_task,
)
from app.services.turn_delivery import stage_user_turn


@pytest.fixture()
def task_db(tmp_path: Path):
    configure_database(f"sqlite:///{(tmp_path / 'task-state.db').as_posix()}")
    init_db()
    yield
    Base.metadata.drop_all(bind=database.engine)


def _stage(content: str, *, status: str = "received") -> tuple[str, str]:
    with database.SessionLocal() as db:
        session = Session(
            title="Durable plan",
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
            content=content,
            mode="auto",
            client_message_id=None,
        )
        run.status = status
        db.commit()
        return session.id, run.id


def test_todowrite_creates_and_updates_one_durable_plan(task_db) -> None:
    session_id, run_id = _stage("实现带测试的缓存重构")

    sync_todos_for_run(run_id, [
        {"id": "inspect", "content": "检查缓存实现", "status": "completed"},
        {"id": "change", "content": "修改缓存实现", "status": "in_progress", "active_form": "正在修改缓存"},
        {"id": "test", "content": "运行测试", "status": "pending"},
    ])

    with database.SessionLocal() as db:
        run = db.get(Run, run_id)
        assert run is not None and run.task_id and run.plan_step_id
        task = db.get(DurableTask, run.task_id)
        assert task is not None
        assert task.session_id == session_id
        assert task.goal == "实现带测试的缓存重构"
        assert task.status == "running"
        steps = list(db.query(PlanStep).filter_by(task_id=task.id).order_by(PlanStep.position))
        assert [step.status for step in steps] == ["completed", "in_progress", "pending"]
        assert task.active_step_id == steps[1].id
        assert steps[1].next_action == "正在修改缓存"

    sync_todos_for_run(run_id, [
        {"id": "inspect", "content": "检查缓存实现", "status": "completed"},
        {"id": "change", "content": "修改缓存实现", "status": "completed"},
        {"id": "test", "content": "运行测试", "status": "completed"},
    ])

    with database.SessionLocal() as db:
        task = db.get(DurableTask, db.get(Run, run_id).task_id)
        assert task is not None and task.status == "completed"
        assert task.active_step_id is None


def test_todowrite_cannot_reopen_a_completed_step(task_db) -> None:
    _session_id, run_id = _stage("Preserve completed work")
    sync_todos_for_run(run_id, [
        {"id": "done", "content": "Completed work", "status": "completed"},
        {"id": "next", "content": "Remaining work", "status": "pending"},
    ])
    with database.SessionLocal() as db:
        run = db.get(Run, run_id)
        done = db.query(PlanStep).filter_by(task_id=run.task_id, external_id="done").one()
        completed_at = done.completed_at

    sync_todos_for_run(run_id, [
        {"id": "done", "content": "Completed work", "status": "in_progress"},
        {"id": "next", "content": "Remaining work", "status": "pending"},
    ])

    with database.SessionLocal() as db:
        run = db.get(Run, run_id)
        task = db.get(DurableTask, run.task_id)
        done = db.query(PlanStep).filter_by(task_id=task.id, external_id="done").one()
        next_step = db.query(PlanStep).filter_by(task_id=task.id, external_id="next").one()
        assert done.status == "completed"
        assert done.completed_at == completed_at
        assert done.remaining_work == []
        assert task.active_step_id == next_step.id


def test_empty_todowrite_does_not_create_a_phantom_task(task_db) -> None:
    _session_id, run_id = _stage("简单回答")
    sync_todos_for_run(run_id, [])
    with database.SessionLocal() as db:
        assert db.get(Run, run_id).task_id is None
        assert db.query(DurableTask).count() == 0


def test_continuation_request_accepts_common_resume_wording() -> None:
    assert is_continuation_request("继续刚刚没完成的工作？")
    assert is_continuation_request("接着之前的步骤做")
    assert is_continuation_request("continue where we left off")


def test_late_todowrite_cannot_reopen_a_stopped_task(task_db) -> None:
    _session_id, run_id = _stage("停止竞态", status="acting")
    sync_todos_for_run(run_id, [
        {"id": "active", "content": "写入结果", "status": "in_progress"},
    ])
    with database.SessionLocal() as db:
        run = db.get(Run, run_id)
        assert run is not None
        run.status = "stopped"
        run.stop_reason = "user_interrupted"
        transition_run_task(db, run, status="stopped", stop_reason=run.stop_reason)
        db.commit()

    sync_todos_for_run(run_id, [
        {"id": "active", "content": "写入结果", "status": "completed"},
    ])
    with database.SessionLocal() as db:
        run = db.get(Run, run_id)
        task = db.get(DurableTask, run.task_id)
        step = db.get(PlanStep, task.active_step_id)
        assert task is not None and task.status == "paused"
        assert step is not None and step.status == "needs_recovery"


def test_todowrite_requires_stable_step_ids(task_db) -> None:
    _session_id, run_id = _stage("稳定步骤")
    with pytest.raises(ValueError, match="stable id"):
        sync_todos_for_run(run_id, [{"content": "无标识步骤", "status": "pending"}])


def test_failed_graph_step_is_retained_for_model_recovery(task_db) -> None:
    _session_id, run_id = _stage("恢复失败步骤")
    sync_todos_for_run(run_id, [
        {"id": "failed-step", "content": "重试失败工作", "status": "pending"},
        {"id": "later", "content": "后续工作", "status": "pending", "depends_on": ["failed-step"]},
    ])
    with database.SessionLocal() as db:
        run = db.get(Run, run_id)
        failed = db.query(PlanStep).filter_by(task_id=run.task_id, external_id="failed-step").one()
        failed.status = "failed"
        failed.error = "temporary child failure"
        db.commit()
        todos = todo_state_for_run(db, run)

    assert [item["id"] for item in todos] == ["failed-step", "later"]
    assert todos[0]["status"] == "in_progress"
    sync_todos_for_run(run_id, todos)
    with database.SessionLocal() as db:
        run = db.get(Run, run_id)
        failed = db.query(PlanStep).filter_by(task_id=run.task_id, external_id="failed-step").one()
        assert failed.status == "in_progress"


def test_compaction_checkpoint_preserves_recovery_evidence_and_dependencies(task_db) -> None:
    _session_id, run_id = _stage("Resume the durable plan")
    sync_todos_for_run(run_id, [
        {"id": "inspect", "content": "Inspect current state", "status": "in_progress"},
        {"id": "test", "content": "Run tests", "status": "pending", "depends_on": ["inspect"]},
    ])
    with database.SessionLocal() as db:
        run = db.get(Run, run_id)
        inspect = db.query(PlanStep).filter_by(task_id=run.task_id, external_id="inspect").one()
        inspect.status = "needs_recovery"
        inspect.next_action = "Verify the partially written file"
        inspect.evidence = ["git diff shows a partial edit"]
        inspect.error = "process interrupted"
        db.commit()

    checkpoint = task_checkpoint_for_run(run_id)

    assert checkpoint["goal"] == "Resume the durable plan"
    inspect_state, test_state = checkpoint["steps"]
    assert inspect_state["status"] == "needs_recovery"
    assert inspect_state["next_action"] == "Verify the partially written file"
    assert inspect_state["evidence"] == ["git diff shows a partial edit"]
    assert inspect_state["error"] == "process interrupted"
    assert test_state["depends_on"] == ["inspect"]


def test_restart_marks_active_step_for_recovery_and_continuation_binds_new_run(task_db) -> None:
    session_id, interrupted_run_id = _stage("实现恢复功能", status="acting")
    sync_todos_for_run(interrupted_run_id, [
        {"id": "model", "content": "增加任务模型", "status": "completed"},
        {"id": "runtime", "content": "接入运行恢复", "status": "in_progress"},
        {"id": "tests", "content": "补充测试", "status": "pending"},
    ])

    RunCoordinator.reconcile_interrupted_runs()

    with database.SessionLocal() as db:
        interrupted = db.get(Run, interrupted_run_id)
        assert interrupted is not None and interrupted.status == "stopped"
        task = db.get(DurableTask, interrupted.task_id)
        assert task is not None and task.status == "needs_recovery"
        active = db.get(PlanStep, task.active_step_id)
        assert active is not None and active.status == "needs_recovery"
        _turn, _message, recovery_run = stage_user_turn(
            db,
            session_id=session_id,
            workspace_id=DEFAULT_WORKSPACE_ID,
            agent_id=DEFAULT_AGENT_ID,
            content="继续刚刚的工作",
            mode="auto",
            client_message_id=None,
        )
        bound = bind_recovery_task(db, recovery_run, "继续刚刚的工作")
        assert bound is not None and bound.id == task.id
        assert recovery_run.task_id == task.id
        assert recovery_run.plan_step_id == active.id
        assert recovery_run.run_kind == "recovery"
        assert recovery_run.resumed_from_run_id == interrupted_run_id
        prompt = recovery_prompt(db, recovery_run)
        assert "实现恢复功能" in prompt
        assert '"status":"needs_recovery"' in prompt
        assert "接入运行恢复" in prompt


def test_user_stop_pauses_task_and_marks_active_step_for_verification(task_db) -> None:
    _session_id, run_id = _stage("暂停后继续", status="acting")
    sync_todos_for_run(run_id, [
        {"id": "done", "content": "已完成工作", "status": "completed"},
        {"id": "active", "content": "正在写文件", "status": "in_progress"},
    ])

    with database.SessionLocal() as db:
        run = db.get(Run, run_id)
        assert run is not None
        transition_run_task(db, run, status="stopped", stop_reason="user_interrupted")
        db.commit()
        task = db.get(DurableTask, run.task_id)
        step = db.get(PlanStep, task.active_step_id)
        assert task is not None and task.status == "paused"
        assert step is not None and step.status == "needs_recovery"
        assert "核验" in step.next_action
