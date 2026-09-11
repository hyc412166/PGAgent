"""验证后台进程作业的持久化、输入输出、等待、恢复、终止通知及其与运行协调器和任务图的联动。

测试通过 fixture 或辅助函数准备隔离环境，再调用真实服务、路由或运行时，并检查返回值、持久化状态与可观察副作用。
变量约定：tmp_path/monkeypatch 提供隔离环境，client/store/runtime 驱动被测链路，各类 *_id 串联持久化实体，payload 表示输入，response/result 表示实际输出，expected 表示期望值。
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import time
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from src.persistence import database
from src.api.routes import delete_session, list_session_background_jobs
from src.persistence.database import Agent, BackgroundJob, Base, CollaborationEvent, PlanStep, Run, Session, Workspace
from src.agent import AgentRuntime, RunOutcome
from src.tasks import background as background_job_service
from src.tasks.background import BackgroundJobManager, BackgroundJobToolStore
from src.runs.service import RunCoordinator
from src.tasks.state import sync_todos_for_run
from src.tools import create_default_registry


# 辅助函数：_wait_for_job_status 封装本组测试重复使用的输入准备、状态查询或测试替身行为。
def _wait_for_job_status(
    job_id: str,
    statuses: set[str],
    *,
    timeout: float = 10.0,
    require_pid: bool = False,
) -> BackgroundJob:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with database.SessionLocal() as db:
            job = db.get(BackgroundJob, job_id)
            if job is not None and job.status in statuses and (not require_pid or job.pid is not None):
                db.expunge(job)
                return job
        time.sleep(0.05)
    raise AssertionError(f"background job {job_id} did not reach {sorted(statuses)}")


@pytest.fixture()
# 测试夹具：background_store 创建本组用例共享的隔离资源，并在测试结束后恢复数据库、配置或进程状态。
def background_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    # 临时数据库保存作业、运行和协作事件；workspace_root 隔离子进程文件，store/manager 分别负责查询与进程生命周期。
    database.configure_database(f"sqlite:///{(tmp_path / 'background.db').as_posix()}")
    database.init_db()
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    monkeypatch.setattr(
        background_job_service,
        "settings",
        SimpleNamespace(data_dir=tmp_path / "data"),
        raising=False,
    )
    with database.SessionLocal() as db:
        workspace = Workspace(name="Background", root_path=str(workspace_root))
        db.add(workspace)
        db.flush()
        agent = Agent(name="Coordinator", workspace_id=workspace.id)
        db.add(agent)
        db.flush()
        session = Session(title="Background", workspace_id=workspace.id, agent_id=agent.id)
        db.add(session)
        db.flush()
        run = Run(session_id=session.id, workspace_id=workspace.id, agent_id=agent.id)
        db.add(run)
        db.commit()
        values = (run.id, session.id, workspace.id)
    manager = BackgroundJobManager()
    monkeypatch.setattr(background_job_service, "background_job_manager", manager)
    store = BackgroundJobToolStore(
        run_id=values[0],
        session_id=values[1],
        workspace_id=values[2],
        workspace_root=str(workspace_root),
    )
    yield store, manager
    manager.shutdown()
    Base.metadata.drop_all(bind=database.engine)


# 测试场景：验证状态能够可靠持久化、重放或在重启后恢复，并保持记录之间的关联；函数名 test_background_job_is_persisted_and_wait_returns_terminal_output 精确标识本用例的具体条件。
def test_background_job_is_persisted_and_wait_returns_terminal_output(background_store) -> None:
    store, _manager = background_store
    started = store.start(
        command="Start-Sleep -Milliseconds 150; Write-Output durable-result",
        shell="powershell",
        timeout=30,
    )
    assert started.ok is True
    job_id = started.metadata["background_job_id"]

    result = store.check(task_id=job_id, wait=True, wait_timeout=10)

    assert result.ok is True
    assert "durable-result" in result.content
    with database.SessionLocal() as db:
        job = db.get(BackgroundJob, job_id)
        assert job is not None
        assert job.status == "completed"
        assert job.exit_code == 0
        assert job.observed_at is not None
        log_path = Path(job.log_path).resolve()
        assert log_path.exists()
        assert log_path.parent == (Path(store.workspace_root).parent / "data" / "background-jobs").resolve()
        assert not (Path(store.workspace_root) / ".pgagent").exists()


# 测试场景：验证该正常业务场景从输入准备到结果断言的完整链路；函数名 test_write_stdin_sends_input_to_running_background_job 精确标识本用例的具体条件。
def test_write_stdin_sends_input_to_running_background_job(background_store) -> None:
    store, _manager = background_store
    started = store.start(
        command='$line = [Console]::In.ReadLine(); Write-Output "stdin:$line"',
        shell="powershell",
        timeout=30,
    )
    job_id = started.metadata["background_job_id"]
    _wait_for_job_status(job_id, {"running"}, require_pid=True)

    sent = store.write_stdin(task_id=job_id, input="hello\n", wait_ms=10_000)

    assert sent.ok
    assert '"chars_sent": 6' in sent.content
    assert "stdin:hello" in sent.content


# 测试场景：验证取消或终止请求会收敛相关运行状态，并正确清理或保留应有资源；函数名 test_background_wait_timeout_yields_without_stopping_process 精确标识本用例的具体条件。
def test_background_wait_timeout_yields_without_stopping_process(background_store) -> None:
    store, _manager = background_store
    started = store.start(
        command="Start-Sleep -Seconds 2; Write-Output late",
        shell="powershell",
        timeout=30,
    )
    job_id = started.metadata["background_job_id"]
    running = _wait_for_job_status(job_id, {"running"}, require_pid=True)

    yielded = store.check(task_id=job_id, wait=True, wait_timeout=0.05)

    assert yielded.ok is True
    assert yielded.metadata["background_job_active"] is True
    assert yielded.metadata["background_wait_timed_out"] is True
    with database.SessionLocal() as db:
        current = db.get(BackgroundJob, job_id)
        assert current.status == "running"
        assert current.pid == running.pid
    assert _manager.cancel(job_id) is True


# 测试场景：验证该正常业务场景从输入准备到结果断言的完整链路；函数名 test_background_output_offsets_return_only_new_bytes 精确标识本用例的具体条件。
def test_background_output_offsets_return_only_new_bytes(background_store) -> None:
    store, _manager = background_store
    started = store.start(
        command="Write-Output first; Start-Sleep -Seconds 1; Write-Output second",
        shell="powershell",
        timeout=30,
    )
    job_id = started.metadata["background_job_id"]
    _wait_for_job_status(job_id, {"running"}, require_pid=True)
    deadline = time.monotonic() + 5
    first_payload = None
    while time.monotonic() < deadline:
        first_result = store.check(task_id=job_id, output_offset=0)
        candidate = json.loads(first_result.content)
        if "first" in candidate["output"]:
            first_payload = candidate
            break
        time.sleep(0.05)
    assert first_payload is not None

    terminal = store.check(
        task_id=job_id,
        wait=True,
        wait_timeout=10,
        output_offset=first_payload["next_output_offset"],
    )
    terminal_payload = json.loads(terminal.content)

    assert terminal.ok is True
    assert "second" in terminal_payload["output"]
    assert "first" not in terminal_payload["output"]


# 测试场景：验证状态能够可靠持久化、重放或在重启后恢复，并保持记录之间的关联；函数名 test_bash_yields_a_durable_session_without_starting_a_second_process 精确标识本用例的具体条件。
def test_bash_yields_a_durable_session_without_starting_a_second_process(background_store) -> None:
    store, _manager = background_store
    registry = create_default_registry(
        store.workspace_root,
        allowed_tool_names=["bash", "check_background"],
        permission_mode="full",
        background_store=store,
    )

    yielded = registry.execute(
        "bash",
        {
            "command": ["python", "-c", "__import__('time').sleep(1)"],
            "yield-time_ms": 10,
            "timeout_seconds": 30,
        },
        approved=True,
    )
    job_id = yielded.metadata["session_id"]
    running = _wait_for_job_status(job_id, {"running"}, require_pid=True)
    terminal = store.check(task_id=job_id, wait=True, wait_timeout=10)

    assert yielded.ok is True
    assert yielded.metadata["background_job_active"] is True
    assert yielded.metadata["background_wait_timed_out"] is True
    assert terminal.ok is True
    with database.SessionLocal() as db:
        assert db.query(BackgroundJob).filter_by(id=job_id).count() == 1
        assert running.pid is not None


# 测试场景：验证该正常业务场景从输入准备到结果断言的完整链路；函数名 test_bash_returns_terminal_output_when_command_finishes_inside_yield_window 精确标识本用例的具体条件。
def test_bash_returns_terminal_output_when_command_finishes_inside_yield_window(background_store) -> None:
    store, _manager = background_store
    registry = create_default_registry(
        store.workspace_root,
        allowed_tool_names=["bash"],
        permission_mode="full",
        background_store=store,
    )

    result = registry.execute(
        "bash",
        {"command": ["python", "-c", "print('fast')"], "yield-time_ms": 10_000},
        approved=True,
    )

    assert result.ok is True
    assert result.metadata["background_job_active"] is False
    assert "fast" in result.content


def test_shell_persists_a_powershell_background_job(background_store) -> None:
    """默认 shell 必须把 PowerShell 类型写入持久任务，供恢复流程使用同一解释器。"""

    store, _manager = background_store
    registry = create_default_registry(
        store.workspace_root,
        allowed_tool_names=["shell"],
        permission_mode="full",
        background_store=store,
    )

    result = registry.execute(
        "shell",
        {
            "command": "Write-Output powershell-session",
            "yield_time_ms": 10_000,
        },
        approved=True,
    )

    job_id = result.metadata["background_job_id"]
    assert result.ok is True
    assert result.metadata["shell"] == "powershell"
    assert "powershell-session" in result.content
    with database.SessionLocal() as db:
        job = db.get(BackgroundJob, job_id)
        assert job is not None
        assert job.shell == "powershell"


# 测试场景：验证状态能够可靠持久化、重放或在重启后恢复，并保持记录之间的关联；函数名 test_recover_relaunches_queued_job_and_settles_stale_running_job 精确标识本用例的具体条件。
def test_recover_relaunches_queued_job_and_settles_stale_running_job(background_store) -> None:
    store, manager = background_store
    with database.SessionLocal() as db:
        queued = BackgroundJob(
            run_id=store.run_id,
            session_id=store.session_id,
            workspace_id=store.workspace_id,
            workspace_root=store.workspace_root,
            command="Write-Output recovered",
            shell="powershell",
            status="queued",
            timeout_seconds=30,
            log_path=str(Path(store.workspace_root) / ".pgagent" / "background-jobs" / "queued.log"),
        )
        stale = BackgroundJob(
            run_id=store.run_id,
            session_id=store.session_id,
            workspace_id=store.workspace_id,
            workspace_root=store.workspace_root,
            command="Write-Output stale",
            shell="powershell",
            status="running",
            timeout_seconds=30,
            log_path=str(Path(store.workspace_root) / ".pgagent" / "background-jobs" / "stale.log"),
        )
        db.add_all([queued, stale])
        db.commit()
        queued_id, stale_id = queued.id, stale.id

    assert queued_id in manager.recover()
    result = store.check(task_id=queued_id, wait=True, wait_timeout=10)
    assert result.ok is True
    assert "recovered" in result.content
    with database.SessionLocal() as db:
        stale = db.get(BackgroundJob, stale_id)
        assert stale is not None
        assert stale.status == "failed"
        assert "restarted" in str(stale.error)


# 测试场景：验证状态能够可靠持久化、重放或在重启后恢复，并保持记录之间的关联；函数名 test_shutdown_marks_started_job_failed_instead_of_replaying_it 精确标识本用例的具体条件。
def test_shutdown_marks_started_job_failed_instead_of_replaying_it(background_store) -> None:
    store, manager = background_store
    sync_todos_for_run(store.run_id, [{
        "id": "install",
        "content": "Install environment",
        "status": "pending",
        "executor_kind": "background",
    }])
    started = store.start(
        command="Start-Sleep -Seconds 30; Write-Output must-not-replay",
        shell="powershell",
        timeout=60,
        plan_step_id="install",
    )
    assert started.ok is True
    job_id = started.metadata["background_job_id"]
    running = _wait_for_job_status(job_id, {"running"}, require_pid=True)
    assert running.pid is not None

    manager.shutdown()

    with database.SessionLocal() as db:
        job = db.get(BackgroundJob, job_id)
        step = db.get(PlanStep, job.plan_step_id)
        assert job.status == "failed"
        assert "shutdown" in str(job.error)
        assert step.status == "failed"
        assert db.query(CollaborationEvent).filter_by(source_id=job_id).count() == 1


# 测试场景：验证非法、越界或不满足前置条件的操作会被明确拒绝，且不会产生错误状态；函数名 test_completion_verifier_rejects_unobserved_background_job 精确标识本用例的具体条件。
def test_completion_verifier_rejects_unobserved_background_job(tmp_path: Path) -> None:
    # 测试替身类：Store 保存该局部场景的可控状态。
    class Store:
        active = [{"id": "job-1", "status": "running"}]
        terminal = []
        registered = False

        # 辅助方法：active_jobs 实现测试替身在此调用阶段需要的最小行为。
        def active_jobs(self):
            return list(self.active)

        # 辅助方法：register_waiter 实现测试替身在此调用阶段需要的最小行为。
        def register_waiter(self):
            self.registered = True
            return ["job-1"]

        # 辅助方法：observe_terminal_results 实现测试替身在此调用阶段需要的最小行为。
        def observe_terminal_results(self):
            results = list(self.terminal)
            self.terminal = []
            return results

    store = Store()
    runtime = AgentRuntime(
        model_call=lambda **_kwargs: None,
        tool_registry=create_default_registry(str(tmp_path), allowed_tool_names=[]),
    )
    RunCoordinator._install_completion_verifier(
        runtime,
        {"runtime_binding": {}, "background_store": store},
    )
    candidate = {
        "output": "done",
        "messages": [{"role": "assistant", "content": "done"}],
        "tool_calls": 0,
        "pending_approval": None,
    }

    rejected = runtime.completion_verifier(candidate)
    assert rejected.accepted is False
    assert rejected.defer_until_event is True
    assert store.registered is True
    assert rejected.report["background_jobs"] == [{"id": "job-1", "status": "running"}]

    store.active = []
    assert runtime.completion_verifier(candidate).accepted is True


# 测试场景：验证并发或批量执行时的顺序、隔离性和最终状态一致性；函数名 test_completion_verifier_consumes_terminal_race_instead_of_dead_waiting 精确标识本用例的具体条件。
def test_completion_verifier_consumes_terminal_race_instead_of_dead_waiting(tmp_path: Path) -> None:
    # 测试替身类：Store 保存该局部场景的可控状态。
    class Store:
        # 辅助方法：active_jobs 实现测试替身在此调用阶段需要的最小行为。
        def active_jobs(self):
            return [{"id": "job-race", "status": "running"}]

        # 辅助方法：register_waiter 实现测试替身在此调用阶段需要的最小行为。
        def register_waiter(self):
            return []

        # 辅助方法：observe_terminal_results 实现测试替身在此调用阶段需要的最小行为。
        def observe_terminal_results(self):
            return [{"id": "job-race", "status": "completed", "output_preview": "ready"}]

    runtime = AgentRuntime(
        model_call=lambda **_kwargs: None,
        tool_registry=create_default_registry(str(tmp_path), allowed_tool_names=[]),
    )
    RunCoordinator._install_completion_verifier(
        runtime,
        {"runtime_binding": {}, "background_store": Store()},
    )
    decision = runtime.completion_verifier({
        "output": "done",
        "messages": [{"role": "assistant", "content": "done"}],
        "tool_calls": 0,
        "pending_approval": None,
    })

    assert decision.accepted is False
    assert decision.defer_until_event is False
    assert decision.report["background_jobs"] == [{"id": "job-race", "status": "completed"}]


# 测试场景：验证状态能够可靠持久化、重放或在重启后恢复，并保持记录之间的关联；函数名 test_terminal_job_writes_durable_collaboration_event 精确标识本用例的具体条件。
def test_terminal_job_writes_durable_collaboration_event(background_store) -> None:
    store, _manager = background_store
    started = store.start(command="Write-Output event-result", shell="powershell", timeout=30)
    job_id = started.metadata["background_job_id"]
    assert store.check(task_id=job_id, wait=True, wait_timeout=10).ok is True

    with database.SessionLocal() as db:
        event = db.query(CollaborationEvent).filter_by(
            source_kind="background_job",
            source_id=job_id,
        ).one()
        assert event.event_type == "background_job_completed"
        assert "event-result" in event.payload["output"]


# 测试场景：验证状态能够可靠持久化、重放或在重启后恢复，并保持记录之间的关联；函数名 test_terminal_event_is_acknowledged_only_with_persisted_model_outcome 精确标识本用例的具体条件。
def test_terminal_event_is_acknowledged_only_with_persisted_model_outcome(background_store) -> None:
    store, _manager = background_store
    with database.SessionLocal() as db:
        job = BackgroundJob(
            run_id=store.run_id,
            session_id=store.session_id,
            workspace_id=store.workspace_id,
            workspace_root=store.workspace_root,
            command="Write-Output durable-event",
            shell="powershell",
            status="completed",
            timeout_seconds=30,
            exit_code=0,
            output_preview="durable-event",
            log_path="",
        )
        db.add(job)
        db.flush()
        event = CollaborationEvent(
            run_id=store.run_id,
            source_kind="background_job",
            source_id=job.id,
            event_type="background_job_completed",
            payload={"status": "completed", "output": "durable-event"},
        )
        db.add(event)
        db.commit()
        job_id, event_id = job.id, event.id

    delivered = store.observe_terminal_results()
    assert [item["id"] for item in delivered] == [job_id]
    assert store.observe_terminal_results() == []
    with database.SessionLocal() as db:
        assert db.get(BackgroundJob, job_id).observed_at is None
        assert db.get(CollaborationEvent, event_id).consumed_at is None

    retry_store = BackgroundJobToolStore(
        run_id=store.run_id,
        session_id=store.session_id,
        workspace_id=store.workspace_id,
        workspace_root=store.workspace_root,
    )
    assert [item["id"] for item in retry_store.observe_terminal_results()] == [job_id]

    RunCoordinator._persist_outcome(
        store.run_id,
        RunOutcome(
            status="completed",
            output="used the durable event",
            messages=[{"role": "assistant", "content": "used the durable event"}],
            events=[{"type": "run_completed"}],
            steps=1,
            tool_calls=0,
        ),
        retry_store.delivered_terminal_ids(),
    )
    with database.SessionLocal() as db:
        job = db.get(BackgroundJob, job_id)
        event = db.get(CollaborationEvent, event_id)
        assert job.observed_at is not None and job.observed_by_run_id == store.run_id
        assert event.consumed_at is not None and event.consumer_run_id == store.run_id


# 测试场景：验证该正常业务场景从输入准备到结果断言的完整链路；函数名 test_background_job_claims_and_settles_a_task_graph_step 精确标识本用例的具体条件。
def test_background_job_claims_and_settles_a_task_graph_step(background_store) -> None:
    store, _manager = background_store
    sync_todos_for_run(store.run_id, [{
        "id": "download-model",
        "content": "Download the large model",
        "status": "pending",
        "executor_kind": "background",
    }])

    started = store.start(
        command="Write-Output model-ready",
        shell="powershell",
        timeout=30,
        plan_step_id="download-model",
    )
    assert started.ok
    with database.SessionLocal() as db:
        job = db.get(BackgroundJob, started.metadata["background_job_id"])
        step = db.get(PlanStep, job.plan_step_id)
        assert step.status == "in_progress"
        assert step.executor_kind == "background"

    assert store.check(task_id=started.metadata["background_job_id"], wait=True, wait_timeout=10).ok
    with database.SessionLocal() as db:
        step = db.get(PlanStep, started.metadata["plan_step_id"])
        assert step.status == "completed"
        assert "model-ready" in step.result


# 测试场景：验证状态能够可靠持久化、重放或在重启后恢复，并保持记录之间的关联；函数名 test_session_background_job_api_reads_durable_state 精确标识本用例的具体条件。
def test_session_background_job_api_reads_durable_state(background_store) -> None:
    store, _manager = background_store
    started = store.start(command="Write-Output api-result", shell="powershell", timeout=30)
    job_id = started.metadata["background_job_id"]
    assert store.check(task_id=job_id, wait=True, wait_timeout=10).ok is True

    with database.SessionLocal() as db:
        rows = list_session_background_jobs(store.session_id, db)

    assert [item.id for item in rows] == [job_id]
    assert rows[0].status == "completed"
    assert "api-result" in rows[0].output_preview


# 测试场景：验证状态能够可靠持久化、重放或在重启后恢复，并保持记录之间的关联；函数名 test_new_run_in_same_session_can_observe_recovered_job 精确标识本用例的具体条件。
def test_new_run_in_same_session_can_observe_recovered_job(background_store) -> None:
    original_store, _manager = background_store
    started = original_store.start(command="Write-Output inherited", shell="powershell", timeout=30)
    job_id = started.metadata["background_job_id"]

    with database.SessionLocal() as db:
        resumed_run = Run(
            session_id=original_store.session_id,
            workspace_id=original_store.workspace_id,
            status="received",
            run_kind="recovery",
        )
        db.add(resumed_run)
        db.commit()
        resumed_run_id = resumed_run.id
    resumed_store = BackgroundJobToolStore(
        run_id=resumed_run_id,
        session_id=original_store.session_id,
        workspace_id=original_store.workspace_id,
        workspace_root=original_store.workspace_root,
    )

    result = resumed_store.check(task_id=job_id, wait=True, wait_timeout=10)

    assert result.ok is True
    assert "inherited" in result.content
    with database.SessionLocal() as db:
        job = db.get(BackgroundJob, job_id)
        assert job is not None
        assert job.run_id == original_store.run_id
        assert job.observed_by_run_id == resumed_run_id


# 测试场景：验证并发或批量执行时的顺序、隔离性和最终状态一致性；函数名 test_concurrent_terminal_notifications_queue_only_one_resume 精确标识本用例的具体条件。
def test_concurrent_terminal_notifications_queue_only_one_resume(background_store, monkeypatch) -> None:
    store, _manager = background_store
    with database.SessionLocal() as db:
        run = db.get(Run, store.run_id)
        run.status = "stopped"
        run.stop_reason = "waiting_background"
        jobs = [BackgroundJob(
            run_id=store.run_id,
            session_id=store.session_id,
            workspace_id=store.workspace_id,
            workspace_root=store.workspace_root,
            command="Write-Output done",
            shell="powershell",
            status="completed",
            timeout_seconds=30,
            log_path="",
            waiting_run_id=store.run_id,
        ) for _ in range(2)]
        db.add_all(jobs)
        db.commit()
        job_ids = [job.id for job in jobs]

    local_coordinator = RunCoordinator()
    scheduled: list[tuple[str, str]] = []
    monkeypatch.setattr(
        local_coordinator,
        "_schedule_background_continuation",
        lambda run_id, job_id: scheduled.append((run_id, job_id)),
    )
    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(local_coordinator.notify_background_terminal, job_ids))

    assert sorted(outcomes) == [False, True]
    assert len(scheduled) == 1
    with database.SessionLocal() as db:
        run = db.get(Run, store.run_id)
        assert run.status == "received"
        assert db.query(database.RunEvent).filter_by(
            run_id=store.run_id,
            event_type="background_continuation_queued",
        ).count() == 1


# 测试场景：验证取消或终止请求会收敛相关运行状态，并正确清理或保留应有资源；函数名 test_user_can_stop_a_run_while_it_waits_for_background_work 精确标识本用例的具体条件。
def test_user_can_stop_a_run_while_it_waits_for_background_work(background_store) -> None:
    store, _manager = background_store
    started = store.start(
        command="Start-Sleep -Seconds 30; Write-Output should-be-cancelled",
        shell="powershell",
        timeout=60,
    )
    job_id = started.metadata["background_job_id"]
    running = _wait_for_job_status(job_id, {"running"}, require_pid=True)
    assert running.pid is not None
    with database.SessionLocal() as db:
        run = db.get(Run, store.run_id)
        run.status = "stopped"
        run.stop_reason = "waiting_background"
        db.commit()

    stopped = RunCoordinator().stop(store.run_id)

    assert stopped is not None
    assert stopped.status == "stopped"
    assert stopped.stop_reason == "user_interrupted"
    cancelled = _wait_for_job_status(job_id, {"cancelled"}, timeout=5)
    assert cancelled.pid is None


# 测试场景：验证非法、越界或不满足前置条件的操作会被明确拒绝，且不会产生错误状态；函数名 test_session_delete_rejects_active_background_job 精确标识本用例的具体条件。
def test_session_delete_rejects_active_background_job(background_store) -> None:
    store, _manager = background_store
    with database.SessionLocal() as db:
        run = db.get(Run, store.run_id)
        run.status = "stopped"
        run.stop_reason = "waiting_background"
        db.add(BackgroundJob(
            run_id=store.run_id,
            session_id=store.session_id,
            workspace_id=store.workspace_id,
            workspace_root=store.workspace_root,
            command="Write-Output queued",
            shell="powershell",
            status="queued",
            timeout_seconds=30,
            log_path="",
        ))
        db.commit()

        with pytest.raises(HTTPException) as error:
            delete_session(store.session_id, db)

        assert error.value.status_code == 409
        assert "background" in str(error.value.detail).lower()


# 测试场景：验证该正常业务场景从输入准备到结果断言的完整链路；函数名 test_sibling_child_completion_only_sees_its_own_background_jobs 精确标识本用例的具体条件。
def test_sibling_child_completion_only_sees_its_own_background_jobs(background_store) -> None:
    first_store, _manager = background_store
    with database.SessionLocal() as db:
        sibling_run = Run(
            session_id=first_store.session_id,
            workspace_id=first_store.workspace_id,
            status="running",
        )
        db.add(sibling_run)
        db.flush()
        sibling_job = BackgroundJob(
            run_id=sibling_run.id,
            session_id=first_store.session_id,
            workspace_id=first_store.workspace_id,
            workspace_root=first_store.workspace_root,
            command="Write-Output sibling",
            shell="powershell",
            status="running",
            timeout_seconds=30,
            log_path=str(Path(first_store.workspace_root) / ".pgagent" / "background-jobs" / "sibling.log"),
        )
        db.add(sibling_job)
        db.commit()

    assert first_store.unresolved_jobs() == []
    recovery_store = BackgroundJobToolStore(
        run_id=first_store.run_id,
        session_id=first_store.session_id,
        workspace_id=first_store.workspace_id,
        workspace_root=first_store.workspace_root,
        include_session_jobs=True,
    )
    assert [item["id"] for item in recovery_store.unresolved_jobs()] == [sibling_job.id]


# 测试场景：验证并发或批量执行时的顺序、隔离性和最终状态一致性；函数名 test_background_wait_is_not_part_of_generic_parallel_read_batch 精确标识本用例的具体条件。
def test_background_wait_is_not_part_of_generic_parallel_read_batch(tmp_path: Path) -> None:
    registry = create_default_registry(
        str(tmp_path),
        allowed_tool_names=["read", "check_background"],
    )
    assert registry.can_execute_batch_in_parallel(["read", "check_background"]) is False
