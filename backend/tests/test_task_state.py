"""验证持久化任务与计划步骤的创建、更新、取消、恢复、压缩检查点和续接绑定。

测试通过 fixture 或辅助函数准备隔离环境，再调用真实服务、路由或运行时，并检查返回值、持久化状态与可观察副作用。
变量约定：tmp_path/monkeypatch 提供隔离环境，client/store/runtime 驱动被测链路，各类 *_id 串联持久化实体，payload 表示输入，response/result 表示实际输出，expected 表示期望值。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.persistence import database
from src.persistence.database import (
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
from src.runs.service import RunCoordinator
from src.tasks.state import (
    bind_recovery_task,
    cancel_durable_task,
    is_task_cancellation_request,
    is_continuation_request,
    recovery_prompt,
    sync_todos_for_run,
    task_checkpoint_for_run,
    todo_state_for_run,
    transition_run_task,
)
from src.sessions.delivery import stage_user_turn


@pytest.fixture()
# 测试夹具：task_db 创建本组用例共享的隔离资源，并在测试结束后恢复数据库、配置或进程状态。
def task_db(tmp_path: Path):
    # 临时数据库保存运行、持久任务与计划步骤，用于检查停止、恢复和续接后的状态转换。
    configure_database(f"sqlite:///{(tmp_path / 'task-state.db').as_posix()}")
    init_db()
    yield
    Base.metadata.drop_all(bind=database.engine)


# 辅助函数：_stage 封装本组测试重复使用的输入准备、状态查询或测试替身行为。
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


# 测试场景：验证状态能够可靠持久化、重放或在重启后恢复，并保持记录之间的关联；函数名 test_todowrite_creates_and_updates_one_durable_plan 精确标识本用例的具体条件。
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


# 测试场景：验证非法、越界或不满足前置条件的操作会被明确拒绝，且不会产生错误状态；函数名 test_todowrite_cannot_reopen_a_completed_step 精确标识本用例的具体条件。
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


# 测试场景：验证非法、越界或不满足前置条件的操作会被明确拒绝，且不会产生错误状态；函数名 test_empty_todowrite_does_not_create_a_phantom_task 精确标识本用例的具体条件。
def test_empty_todowrite_does_not_create_a_phantom_task(task_db) -> None:
    _session_id, run_id = _stage("简单回答")
    sync_todos_for_run(run_id, [])
    with database.SessionLocal() as db:
        assert db.get(Run, run_id).task_id is None
        assert db.query(DurableTask).count() == 0


# 测试场景：验证该正常业务场景从输入准备到结果断言的完整链路；函数名 test_continuation_request_accepts_common_resume_wording 精确标识本用例的具体条件。
def test_continuation_request_accepts_common_resume_wording() -> None:
    assert is_continuation_request("继续刚刚没完成的工作？")
    assert is_continuation_request("接着之前的步骤做")
    assert is_continuation_request("continue where we left off")


# 测试场景：验证取消或终止请求会收敛相关运行状态，并正确清理或保留应有资源；函数名 test_task_cancellation_request_accepts_explicit_short_commands 精确标识本用例的具体条件。
def test_task_cancellation_request_accepts_explicit_short_commands() -> None:
    assert is_task_cancellation_request("取消这个任务")
    assert is_task_cancellation_request("cancel this task")
    assert not is_task_cancellation_request("取消这个任务后，帮我创建另一个发布任务")


# 测试场景：验证状态能够可靠持久化、重放或在重启后恢复，并保持记录之间的关联；函数名 test_cancel_durable_task_is_terminal_and_preserves_completed_steps 精确标识本用例的具体条件。
def test_cancel_durable_task_is_terminal_and_preserves_completed_steps(task_db) -> None:
    _session_id, run_id = _stage("Cancel a durable task")
    sync_todos_for_run(run_id, [
        {"id": "done", "content": "Completed work", "status": "completed"},
        {"id": "active", "content": "Active work", "status": "in_progress"},
        {"id": "next", "content": "Pending work", "status": "pending"},
    ])

    with database.SessionLocal() as db:
        run = db.get(Run, run_id)
        task = db.get(DurableTask, run.task_id)
        cancel_durable_task(db, task)
        db.commit()

        assert task.status == "cancelled"
        assert task.active_step_id is None
        assert task.completed_at is not None
        steps = list(db.query(PlanStep).filter_by(task_id=task.id).order_by(PlanStep.position))
        assert [step.status for step in steps] == ["completed", "cancelled", "cancelled"]
        assert all(step.completed_at is not None for step in steps)


# 测试场景：验证非法、越界或不满足前置条件的操作会被明确拒绝，且不会产生错误状态；函数名 test_late_todowrite_cannot_reopen_a_stopped_task 精确标识本用例的具体条件。
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


# 测试场景：验证该正常业务场景从输入准备到结果断言的完整链路；函数名 test_todowrite_requires_stable_step_ids 精确标识本用例的具体条件。
def test_todowrite_requires_stable_step_ids(task_db) -> None:
    _session_id, run_id = _stage("稳定步骤")
    with pytest.raises(ValueError, match="stable id"):
        sync_todos_for_run(run_id, [{"content": "无标识步骤", "status": "pending"}])


# 测试场景：验证状态能够可靠持久化、重放或在重启后恢复，并保持记录之间的关联；函数名 test_failed_graph_step_is_retained_for_model_recovery 精确标识本用例的具体条件。
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


# 测试场景：验证状态能够可靠持久化、重放或在重启后恢复，并保持记录之间的关联；函数名 test_compaction_checkpoint_preserves_recovery_evidence_and_dependencies 精确标识本用例的具体条件。
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


# 测试场景：验证状态能够可靠持久化、重放或在重启后恢复，并保持记录之间的关联；函数名 test_restart_marks_active_step_for_recovery_and_continuation_binds_new_run 精确标识本用例的具体条件。
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


# 测试场景：验证取消或终止请求会收敛相关运行状态，并正确清理或保留应有资源；函数名 test_user_stop_pauses_task_and_marks_active_step_for_verification 精确标识本用例的具体条件。
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
