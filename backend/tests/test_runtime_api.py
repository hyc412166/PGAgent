"""验证运行与草稿 API 的启动、停止、续接、审批、幂等物化和上下文查询。

测试通过 fixture 或辅助函数准备隔离环境，再调用真实服务、路由或运行时，并检查返回值、持久化状态与可观察副作用。
变量约定：tmp_path/monkeypatch 提供隔离环境，client/store/runtime 驱动被测链路，各类 *_id 串联持久化实体，payload 表示输入，response/result 表示实际输出，expected 表示期望值。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

from src.persistence import database
from src.api.runtime import _stream_event_is_terminal, router
from src.api.routes import router as resources_router
from src.persistence.database import (
    DEFAULT_AGENT_ID,
    DEFAULT_WORKSPACE_ID,
    Agent,
    Approval,
    Base,
    ChatMessage,
    ConversationCompaction,
    ConversationTurn,
    DelegatedTask,
    DraftLaunch,
    DurableTask,
    ModelConnection,
    PlanStep,
    Run,
    RunEvent,
    Session,
    Workspace,
    configure_database,
    init_db,
)
from src.runs.service import RunCoordinator, coordinator
from src.runs.stream import run_stream_broker
from src.tasks.state import sync_todos_for_run, transition_run_task
from src.observability import current_observability_context


@pytest.fixture()
# 测试夹具：client 创建本组用例共享的隔离资源，并在测试结束后恢复数据库、配置或进程状态。
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[TestClient, dict[str, list]]:
    # 临时数据库承载草稿和运行状态；scheduled 捕获协调器调度，test_client 驱动运行与审批 API。
    configure_database(f"sqlite:///{(tmp_path / 'runtime-api.db').as_posix()}")
    init_db()
    launched: dict[str, list] = {"calls": []}
    monkeypatch.setattr(
        coordinator,
        "launch",
        lambda run_id, resume=False: launched["calls"].append((run_id, resume)) or True,
    )
    app = FastAPI()
    app.include_router(resources_router)
    app.include_router(router)
    with TestClient(app) as test_client:
        yield test_client, launched
    Base.metadata.drop_all(bind=database.engine)


# 测试场景：验证取消或终止请求会收敛相关运行状态，并正确清理或保留应有资源；函数名 test_waiting_stop_events_do_not_close_the_run_stream 精确标识本用例的具体条件。
def test_waiting_stop_events_do_not_close_the_run_stream() -> None:
    assert not _stream_event_is_terminal({"type": "run_stopped", "reason": "waiting_background"})
    assert not _stream_event_is_terminal({"type": "run_stopped", "reason": "delegated_child_waiting_event"})
    assert _stream_event_is_terminal({"type": "run_stopped", "reason": "user_interrupted"})
    assert _stream_event_is_terminal({"type": "run_completed"})


# 辅助函数：_seed 封装本组测试重复使用的输入准备、状态查询或测试替身行为。
def _seed() -> tuple[str, str, str]:
    with database.SessionLocal() as db:
        workspace = Workspace(name="Demo", root_path="C:/demo")
        db.add(workspace)
        db.flush()
        agent = Agent(name="Builder", workspace_id=workspace.id)
        db.add(agent)
        db.flush()
        session = Session(title="Chat", workspace_id=workspace.id, agent_id=agent.id)
        db.add(session)
        db.commit()
        return workspace.id, agent.id, session.id


def test_run_coordinator_binds_root_and_delegated_observability_context(
    client: tuple[TestClient, dict[str, list]],
) -> None:
    _test_client, _launched = client
    workspace_id, agent_id, session_id = _seed()
    with database.SessionLocal() as db:
        turn = ConversationTurn(session_id=session_id, trace_id="trace-observability")
        db.add(turn)
        db.flush()
        parent = Run(
            session_id=session_id,
            workspace_id=workspace_id,
            agent_id=agent_id,
            turn_id=turn.id,
        )
        child = Run(
            session_id=session_id,
            workspace_id=workspace_id,
            agent_id=agent_id,
            run_kind="delegated",
        )
        db.add_all([parent, child])
        db.flush()
        db.add(
            DelegatedTask(
                parent_run_id=parent.id,
                parent_session_id=session_id,
                child_run_id=child.id,
                child_agent_id=agent_id,
                title="检查日志链路",
                idempotency_key="observability-context",
            )
        )
        db.commit()
        parent_id = parent.id
        child_id = child.id
        turn_id = turn.id

    local_coordinator = RunCoordinator()
    with local_coordinator._observability_scope(parent_id):
        assert current_observability_context() == {
            "trace_id": "trace-observability",
            "run_id": parent_id,
            "turn_id": turn_id,
        }

    with local_coordinator._observability_scope(child_id):
        assert current_observability_context() == {
            "trace_id": "trace-observability",
            "run_id": child_id,
            "turn_id": turn_id,
            "parent_run_id": parent_id,
            "child_run_id": child_id,
        }

    assert current_observability_context() == {}


# 测试场景：验证状态能够可靠持久化、重放或在重启后恢复，并保持记录之间的关联；函数名 test_launch_session_run_persists_message_and_returns_immediately 精确标识本用例的具体条件。
def test_launch_session_run_persists_message_and_returns_immediately(
    client: tuple[TestClient, dict[str, list]],
) -> None:
    test_client, launched = client
    _workspace_id, _agent_id, session_id = _seed()
    response = test_client.post(f"/api/sessions/{session_id}/run", json={"content": "读取文件"})
    assert response.status_code == 202, response.text
    run_id = response.json()["id"]
    assert response.json()["status"] == "received"
    assert launched["calls"] == [(run_id, False)]
    with database.SessionLocal() as db:
        run = db.get(Run, run_id)
        assert run is not None and run.mode == "auto"
        assert run.turn_id is not None
        turn = db.get(ConversationTurn, run.turn_id)
        assert turn is not None and turn.user_message_id is not None
        assert turn.reply_status == "pending"
        assert run.agent_id == DEFAULT_AGENT_ID
        session = db.get(Session, session_id)
        assert session is not None and session.agent_id == DEFAULT_AGENT_ID
        assert db.query(database.ChatMessage).filter_by(session_id=session_id).one().content == "读取文件"


# 测试场景：验证状态能够可靠持久化、重放或在重启后恢复，并保持记录之间的关联；函数名 test_continue_turn_binds_latest_interrupted_durable_task 精确标识本用例的具体条件。
def test_continue_turn_binds_latest_interrupted_durable_task(
    client: tuple[TestClient, dict[str, list]],
) -> None:
    test_client, launched = client
    _workspace_id, _agent_id, session_id = _seed()
    initial = test_client.post(f"/api/sessions/{session_id}/run", json={"content": "实现恢复链路"})
    assert initial.status_code == 202
    initial_run_id = initial.json()["id"]
    sync_todos_for_run(initial_run_id, [
        {"id": "schema", "content": "持久化计划", "status": "completed"},
        {"id": "resume", "content": "恢复未完成步骤", "status": "in_progress"},
    ])
    with database.SessionLocal() as db:
        interrupted = db.get(Run, initial_run_id)
        assert interrupted is not None
        task_id = interrupted.task_id
        assert task_id
        interrupted.status = "stopped"
        interrupted.stop_reason = "interrupted_restart"
        transition_run_task(db, interrupted, status="stopped", stop_reason=interrupted.stop_reason)
        db.commit()

    resumed = test_client.post(
        f"/api/sessions/{session_id}/run",
        json={"content": "继续刚刚的工作"},
    )
    assert resumed.status_code == 202, resumed.text
    payload = resumed.json()
    assert payload["run_kind"] == "recovery"
    assert payload["resumed_from_run_id"] == initial_run_id
    assert payload["task_id"] == task_id
    with database.SessionLocal() as db:
        recovered = db.get(Run, payload["id"])
        assert recovered is not None and recovered.task_id
        task = db.get(DurableTask, recovered.task_id)
        step = db.get(PlanStep, recovered.plan_step_id)
        assert task is not None and task.goal == "实现恢复链路"
        assert step is not None and step.status == "needs_recovery"
    assert launched["calls"] == [(initial_run_id, False), (payload["id"], False)]


# 测试场景：验证状态能够可靠持久化、重放或在重启后恢复，并保持记录之间的关联；函数名 test_explicit_cancel_turn_terminates_the_durable_task_before_model_launch 精确标识本用例的具体条件。
def test_explicit_cancel_turn_terminates_the_durable_task_before_model_launch(
    client: tuple[TestClient, dict[str, list]],
) -> None:
    test_client, launched = client
    _workspace_id, _agent_id, session_id = _seed()
    initial = test_client.post(f"/api/sessions/{session_id}/run", json={"content": "Build a durable feature"})
    initial_run_id = initial.json()["id"]
    sync_todos_for_run(initial_run_id, [
        {"id": "done", "content": "Completed work", "status": "completed"},
        {"id": "active", "content": "Active work", "status": "in_progress"},
    ])

    cancelled = test_client.post(
        f"/api/sessions/{session_id}/run",
        json={"content": "取消这个任务"},
    )

    assert cancelled.status_code == 202, cancelled.text
    assert cancelled.json()["task_id"] is None
    with database.SessionLocal() as db:
        initial_run = db.get(Run, initial_run_id)
        task = db.get(DurableTask, initial_run.task_id)
        steps = list(db.query(PlanStep).filter_by(task_id=task.id).order_by(PlanStep.position))
        assert initial_run.status == "stopped"
        assert task.status == "cancelled"
        assert task.active_step_id is None
        assert [step.status for step in steps] == ["completed", "cancelled"]
    assert launched["calls"] == [(initial_run_id, False), (cancelled.json()["id"], False)]


# 测试场景：验证该正常业务场景从输入准备到结果断言的完整链路；函数名 test_session_run_idempotency_reuses_the_same_accepted_turn 精确标识本用例的具体条件。
def test_session_run_idempotency_reuses_the_same_accepted_turn(
    client: tuple[TestClient, dict[str, list]],
) -> None:
    test_client, launched = client
    _workspace_id, _agent_id, session_id = _seed()
    payload = {"content": "读取文件", "idempotency_key": "turn-message-1"}

    first = test_client.post(f"/api/sessions/{session_id}/run", json=payload)
    retry = test_client.post(f"/api/sessions/{session_id}/run", json=payload)
    conflict = test_client.post(
        f"/api/sessions/{session_id}/run",
        json={"content": "不同任务", "idempotency_key": "turn-message-1"},
    )

    assert first.status_code == 202, first.text
    assert retry.status_code == 200, retry.text
    assert retry.json()["id"] == first.json()["id"]
    assert conflict.status_code == 409
    assert launched["calls"] == [(first.json()["id"], False)]
    with database.SessionLocal() as db:
        assert db.query(ConversationTurn).count() == 1
        assert db.query(ChatMessage).filter_by(session_id=session_id, role="user").count() == 1


# 测试场景：验证非法、越界或不满足前置条件的操作会被明确拒绝，且不会产生错误状态；函数名 test_second_active_run_is_rejected 精确标识本用例的具体条件。
def test_second_active_run_is_rejected(client: tuple[TestClient, dict[str, list]]) -> None:
    test_client, _launched = client
    workspace_id, agent_id, session_id = _seed()
    with database.SessionLocal() as db:
        db.add(Run(session_id=session_id, workspace_id=workspace_id, agent_id=agent_id, status="acting"))
        db.commit()
    response = test_client.post(f"/api/sessions/{session_id}/run", json={"content": "again"})
    assert response.status_code == 409


# 测试场景：验证状态能够可靠持久化、重放或在重启后恢复，并保持记录之间的关联；函数名 test_run_stream_sends_current_state_replay_and_terminal_with_stable_ids 精确标识本用例的具体条件。
def test_run_stream_sends_current_state_replay_and_terminal_with_stable_ids(
    client: tuple[TestClient, dict[str, list]],
) -> None:
    test_client, _launched = client
    workspace_id, agent_id, session_id = _seed()
    with database.SessionLocal() as db:
        run = Run(
            session_id=session_id,
            workspace_id=workspace_id,
            agent_id=agent_id,
            status="completed",
        )
        db.add(run)
        db.commit()
        run_id = run.id

    started = run_stream_broker.publish(run_id, {"type": "context_prepared"})
    terminal = run_stream_broker.publish(run_id, {"type": "run_completed", "output": "done"})

    response = test_client.get(
        f"/api/runs/{run_id}/stream",
        headers={"Last-Event-ID": started["event_id"]},
    )

    assert response.status_code == 200
    assert "event: run_state" in response.text
    assert '"status":"completed"' in response.text
    assert "context_prepared" not in response.text
    assert "event: run_completed" in response.text
    assert f"id: {terminal['event_id']}" in response.text
    assert run_stream_broker.subscriber_count(run_id) == 0


def test_run_stream_projects_durable_events_without_private_payloads(
    client: tuple[TestClient, dict[str, list]],
) -> None:
    test_client, _launched = client
    workspace_id, agent_id, session_id = _seed()
    with database.SessionLocal() as db:
        run = Run(
            session_id=session_id,
            workspace_id=workspace_id,
            agent_id=agent_id,
            status="completed",
        )
        db.add(run)
        db.commit()
        run_id = run.id

    run_stream_broker.publish(run_id, {
        "type": "tool_started",
        "tool_name": "read_file",
        "tool_call_id": "call-secret-test",
        "arguments": {
            "path": "safe.txt",
            "content": "private-file-body",
            "api_key": "private-api-key",
        },
    })
    run_stream_broker.publish(run_id, {
        "type": "run_completed",
        "output": "private-assistant-output",
    })

    response = test_client.get(f"/api/runs/{run_id}/stream")

    assert response.status_code == 200
    assert '"path":"safe.txt"' in response.text
    assert "private-file-body" not in response.text
    assert "private-api-key" not in response.text
    assert "private-assistant-output" not in response.text


# 测试场景：验证取消或终止请求会收敛相关运行状态，并正确清理或保留应有资源；函数名 test_stop_run_is_idempotent_and_keeps_partial_stream_evidence 精确标识本用例的具体条件。
def test_stop_run_is_idempotent_and_keeps_partial_stream_evidence(
    client: tuple[TestClient, dict[str, list]],
) -> None:
    test_client, _launched = client
    workspace_id, agent_id, session_id = _seed()
    with database.SessionLocal() as db:
        run = Run(
            session_id=session_id,
            workspace_id=workspace_id,
            agent_id=agent_id,
            status="acting",
        )
        db.add(run)
        db.commit()
        run_id = run.id

    # Tool lifecycle events are already durable; token deltas remain transient
    # until the stop endpoint snapshots the visible partial response.
    RunCoordinator._event_sink(run_id)({
        "type": "tool_started",
        "tool_name": "read_file",
        "tool_call_id": "call-1",
    })
    RunCoordinator._stream_sink(run_id)({"type": "assistant_delta", "delta": "partial answer"})
    RunCoordinator._stream_sink(run_id)({"type": "progress", "summary": "partial thought"})

    response = test_client.post(f"/api/runs/{run_id}/stop")
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "stopped"
    assert response.json()["stop_reason"] == "user_interrupted"
    assert response.json()["error_code"] == "run_interrupted"

    retry = test_client.post(f"/api/runs/{run_id}/stop", json={"reason": "user_interrupted"})
    assert retry.status_code == 200, retry.text
    assert retry.json()["status"] == "stopped"
    assert retry.json()["stop_reason"] == "user_interrupted"

    # A synchronous provider can finish its worker thread after the stop
    # commit.  The event sink must reject that late lifecycle event as well.
    RunCoordinator._event_sink(run_id)({"type": "tool_finished", "tool_name": "read_file"})

    with database.SessionLocal() as db:
        run = db.get(Run, run_id)
        assert run is not None and run.status == "stopped"
        events = list(db.scalars(select(RunEvent).where(RunEvent.run_id == run_id)))
        interrupted = [event for event in events if event.event_type == "run_interrupted"]
        assert len(interrupted) == 1
        assert interrupted[0].payload["partial_output"] == "partial answer"
        assert interrupted[0].payload["partial_thought"] == "partial thought"
        assert any(event.event_type == "tool_started" for event in events)
        assert not any(event.event_type == "tool_finished" for event in events)


# 测试场景：验证状态能够可靠持久化、重放或在重启后恢复，并保持记录之间的关联；函数名 test_stopping_an_accepted_turn_persists_exactly_one_terminal_reply 精确标识本用例的具体条件。
def test_stopping_an_accepted_turn_persists_exactly_one_terminal_reply(
    client: tuple[TestClient, dict[str, list]],
) -> None:
    test_client, _launched = client
    _workspace_id, _agent_id, session_id = _seed()
    launched = test_client.post(
        f"/api/sessions/{session_id}/run",
        json={"content": "执行一个长任务", "idempotency_key": "turn-stop-1"},
    )
    assert launched.status_code == 202, launched.text
    run_id = launched.json()["id"]
    first = test_client.post(f"/api/runs/{run_id}/stop")
    second = test_client.post(f"/api/runs/{run_id}/stop")
    assert first.status_code == 200
    assert second.status_code == 200

    with database.SessionLocal() as db:
        run = db.get(Run, run_id)
        assert run is not None and run.turn_id is not None
        turn = db.get(ConversationTurn, run.turn_id)
        assert turn is not None and turn.reply_status == "delivered"
        replies = list(db.scalars(select(ChatMessage).where(
            ChatMessage.terminal_for_turn_id == turn.id,
        )))
        assert len(replies) == 1
        assert "按你的要求停止" in replies[0].content


# 测试场景：验证非法、越界或不满足前置条件的操作会被明确拒绝，且不会产生错误状态；函数名 test_stop_run_cancels_in_process_task_and_does_not_rewrite_completed 精确标识本用例的具体条件。
def test_stop_run_cancels_in_process_task_and_does_not_rewrite_completed(
    client: tuple[TestClient, dict[str, list]],
) -> None:
    test_client, _launched = client
    workspace_id, agent_id, session_id = _seed()

    # 测试替身类：PendingTask 保存该局部场景的可控状态。
    class PendingTask:
        # 辅助方法：__init__ 实现测试替身在此调用阶段需要的最小行为。
        def __init__(self) -> None:
            self.cancelled = False

        # 辅助方法：done 实现测试替身在此调用阶段需要的最小行为。
        def done(self) -> bool:
            return False

        # 辅助方法：cancel 实现测试替身在此调用阶段需要的最小行为。
        def cancel(self) -> bool:
            self.cancelled = True
            return True

    with database.SessionLocal() as db:
        active = Run(session_id=session_id, workspace_id=workspace_id, agent_id=agent_id, status="acting")
        completed = Run(
            session_id=session_id,
            workspace_id=workspace_id,
            agent_id=agent_id,
            status="completed",
        )
        db.add_all([active, completed])
        db.commit()
        active_id, completed_id = active.id, completed.id

    pending = PendingTask()
    coordinator._tasks[active_id] = pending  # type: ignore[assignment]
    try:
        response = test_client.post(f"/api/runs/{active_id}/stop")
        assert response.status_code == 200
        assert pending.cancelled is True
    finally:
        coordinator._tasks.pop(active_id, None)

    untouched = test_client.post(f"/api/runs/{completed_id}/stop")
    assert untouched.status_code == 200
    assert untouched.json()["status"] == "completed"
    assert untouched.json()["stop_reason"] is None
    with database.SessionLocal() as db:
        assert db.scalar(
            select(RunEvent).where(
                RunEvent.run_id == completed_id,
                RunEvent.event_type == "run_interrupted",
            )
        ) is None


# 测试场景：验证取消或终止请求会收敛相关运行状态，并正确清理或保留应有资源；函数名 test_stop_waiting_approval_supersedes_pending_and_never_resumes 精确标识本用例的具体条件。
def test_stop_waiting_approval_supersedes_pending_and_never_resumes(
    client: tuple[TestClient, dict[str, list]],
) -> None:
    test_client, launched = client
    workspace_id, agent_id, session_id = _seed()
    with database.SessionLocal() as db:
        run = Run(
            session_id=session_id,
            workspace_id=workspace_id,
            agent_id=agent_id,
            status="awaiting_approval",
        )
        db.add(run)
        db.flush()
        approval = Approval(run_id=run.id, tool_name="write_file", arguments={"path": "a.txt"})
        snapshot = RunEvent(
            run_id=run.id,
            event_type="runtime_snapshot",
            payload={
                "status": "awaiting_approval",
                "pending_approval": {
                    "id": "call-approval",
                    "tool_name": "write_file",
                    "arguments": {"path": "a.txt"},
                },
            },
        )
        db.add_all([approval, snapshot])
        db.commit()
        run_id, approval_id = run.id, approval.id

    response = test_client.post(f"/api/runs/{run_id}/stop")
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "stopped"
    assert launched["calls"] == []
    with database.SessionLocal() as db:
        cancelled = db.get(Approval, approval_id)
        assert cancelled is not None and cancelled.status == "superseded"
        assert db.scalar(
            select(RunEvent).where(
                RunEvent.run_id == run_id,
                RunEvent.event_type == "approval_cancelled",
            )
        ) is not None

    # The stale approval click cannot resurrect an explicitly stopped run.
    rejected = test_client.post(
        f"/api/approvals/{approval_id}/decide",
        json={"decision": "approve"},
    )
    assert rejected.status_code == 409


# 测试场景：验证取消或终止请求会收敛相关运行状态，并正确清理或保留应有资源；函数名 test_stopping_parent_settles_active_delegated_child 精确标识本用例的具体条件。
def test_stopping_parent_settles_active_delegated_child(
    client: tuple[TestClient, dict[str, list]],
) -> None:
    test_client, _launched = client
    workspace_id, agent_id, session_id = _seed()
    with database.SessionLocal() as db:
        parent = Run(session_id=session_id, workspace_id=workspace_id, agent_id=agent_id, status="acting")
        child = Run(session_id=session_id, workspace_id=workspace_id, agent_id=agent_id, status="acting")
        db.add_all([parent, child])
        db.flush()
        delegation = DelegatedTask(
            parent_run_id=parent.id,
            parent_session_id=session_id,
            child_run_id=child.id,
            child_agent_id=agent_id,
            title="Inspect files",
            description="Inspect files",
            status="in_progress",
            idempotency_key=f"stop-child-{parent.id}",
        )
        db.add(delegation)
        db.commit()
        parent_id, child_id, delegation_id = parent.id, child.id, delegation.id

    response = test_client.post(f"/api/runs/{parent_id}/stop")
    assert response.status_code == 200, response.text
    with database.SessionLocal() as db:
        child = db.get(Run, child_id)
        task = db.get(DelegatedTask, delegation_id)
        assert child is not None and child.status == "stopped"
        assert child.stop_reason == "parent_user_interrupted"
        assert task is not None and task.status == "blocked"
        assert (task.result or {}).get("status") == "stopped"
        assert db.scalar(
            select(RunEvent).where(
                RunEvent.run_id == parent_id,
                RunEvent.event_type == "delegated_child_stopped",
            )
        ) is not None


# 测试场景：验证取消或终止请求会收敛相关运行状态，并正确清理或保留应有资源；函数名 test_stopping_delegated_child_settles_task_and_parent 精确标识本用例的具体条件。
def test_stopping_delegated_child_settles_task_and_parent(
    client: tuple[TestClient, dict[str, list]],
) -> None:
    test_client, _launched = client
    workspace_id, agent_id, session_id = _seed()
    with database.SessionLocal() as db:
        parent = Run(session_id=session_id, workspace_id=workspace_id, agent_id=agent_id, status="acting")
        child = Run(session_id=session_id, workspace_id=workspace_id, agent_id=agent_id, status="acting")
        db.add_all([parent, child])
        db.flush()
        delegation = DelegatedTask(
            parent_run_id=parent.id,
            parent_session_id=session_id,
            child_run_id=child.id,
            child_agent_id=agent_id,
            title="Inspect files",
            description="Inspect files",
            status="in_progress",
            idempotency_key=f"stop-direct-child-{parent.id}",
        )
        db.add(delegation)
        db.flush()
        db.add(RunEvent(
            run_id=child.id,
            event_type="delegation_link",
            payload={
                "delegation_id": delegation.id,
                "parent_run_id": parent.id,
                "parent_session_id": session_id,
            },
        ))
        db.commit()
        parent_id, child_id, delegation_id = parent.id, child.id, delegation.id

    response = test_client.post(f"/api/runs/{child_id}/stop")
    assert response.status_code == 200, response.text
    with database.SessionLocal() as db:
        parent = db.get(Run, parent_id)
        child = db.get(Run, child_id)
        task = db.get(DelegatedTask, delegation_id)
        assert parent is not None and parent.status == "stopped"
        assert parent.stop_reason == "user_interrupted"
        assert child is not None and child.status == "stopped"
        assert child.stop_reason == "user_interrupted"
        assert task is not None and task.status == "blocked"
        assert (task.result or {}).get("stop_reason") == "user_interrupted"
        assert db.scalar(
            select(RunEvent).where(
                RunEvent.run_id == parent_id,
                RunEvent.event_type == "delegated_child_stopped",
            )
        ) is not None


# 测试场景：验证权限、审批或敏感数据边界在完整调用链路中保持有效；函数名 test_approval_decision_resumes_only_when_approved 精确标识本用例的具体条件。
def test_approval_decision_resumes_only_when_approved(client: tuple[TestClient, dict[str, list]]) -> None:
    test_client, launched = client
    workspace_id, agent_id, session_id = _seed()
    with database.SessionLocal() as db:
        run = Run(session_id=session_id, workspace_id=workspace_id, agent_id=agent_id, status="awaiting_approval")
        db.add(run)
        db.flush()
        approval = Approval(run_id=run.id, tool_name="write_file", arguments={"path": "a.txt"})
        snapshot = RunEvent(
            run_id=run.id,
            event_type="runtime_snapshot",
            payload={
                "status": "awaiting_approval",
                "pending_approval": {
                    "id": "call-1",
                    "tool_name": "write_file",
                    "arguments": {"path": "a.txt"},
                },
            },
        )
        db.add_all([approval, snapshot])
        db.commit()
        approval_id, run_id = approval.id, run.id

    response = test_client.post(f"/api/approvals/{approval_id}/decide", json={"decision": "approve"})
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "approved"
    assert launched["calls"] == [(run_id, True)]


# 测试场景：验证非法、越界或不满足前置条件的操作会被明确拒绝，且不会产生错误状态；函数名 test_rejecting_root_approval_persists_exactly_one_terminal_reply 精确标识本用例的具体条件。
def test_rejecting_root_approval_persists_exactly_one_terminal_reply(
    client: tuple[TestClient, dict[str, list]],
) -> None:
    test_client, launched = client
    _workspace_id, _agent_id, session_id = _seed()
    accepted = test_client.post(
        f"/api/sessions/{session_id}/run",
        json={"content": "请写入文件", "idempotency_key": "reject-root-turn"},
    )
    assert accepted.status_code == 202, accepted.text
    run_id = accepted.json()["id"]

    with database.SessionLocal() as db:
        run = db.get(Run, run_id)
        assert run is not None and run.turn_id is not None
        run.status = "awaiting_approval"
        approval = Approval(
            run_id=run.id,
            tool_name="write_file",
            arguments={"path": "a.txt"},
        )
        snapshot = RunEvent(
            run_id=run.id,
            event_type="runtime_snapshot",
            payload={
                "status": "awaiting_approval",
                "pending_approval": {
                    "id": "reject-root-call",
                    "tool_name": "write_file",
                    "arguments": {"path": "a.txt"},
                },
            },
        )
        db.add_all([approval, snapshot])
        db.commit()
        approval_id = approval.id

    response = test_client.post(
        f"/api/approvals/{approval_id}/decide",
        json={"decision": "reject", "reason": "暂不允许写入"},
    )

    assert response.status_code == 200, response.text
    assert response.json()["status"] == "rejected"
    assert launched["calls"] == [(run_id, False)]
    with database.SessionLocal() as db:
        run = db.get(Run, run_id)
        assert run is not None and run.status == "stopped"
        assert run.stop_reason == "approval_rejected"
        turn = db.get(ConversationTurn, run.turn_id)
        assert turn is not None and turn.reply_status == "delivered"
        replies = list(db.scalars(select(ChatMessage).where(
            ChatMessage.terminal_for_turn_id == turn.id,
        )))
        assert len(replies) == 1
        assert replies[0].extra["error_code"] == "approval_rejected"
        assert replies[0].extra["trace_id"] == turn.trace_id
        assert "没有获得批准" in replies[0].content


# 测试场景：验证非法、越界或不满足前置条件的操作会被明确拒绝，且不会产生错误状态；函数名 test_approval_decision_is_restored_when_resume_cannot_be_scheduled 精确标识本用例的具体条件。
def test_approval_decision_is_restored_when_resume_cannot_be_scheduled(
    client: tuple[TestClient, dict[str, list]], monkeypatch: pytest.MonkeyPatch
) -> None:
    test_client, _launched = client
    workspace_id, agent_id, session_id = _seed()
    with database.SessionLocal() as db:
        run = Run(session_id=session_id, workspace_id=workspace_id, agent_id=agent_id, status="awaiting_approval")
        db.add(run)
        db.flush()
        approval = Approval(
            run_id=run.id,
            tool_name="write_file",
            arguments={"path": "a.txt"},
            reason="write access",
        )
        snapshot = RunEvent(
            run_id=run.id,
            event_type="runtime_snapshot",
            payload={
                "status": "awaiting_approval",
                "pending_approval": {
                    "id": "call-1",
                    "tool_name": "write_file",
                    "arguments": {"path": "a.txt"},
                },
            },
        )
        db.add_all([approval, snapshot])
        db.commit()
        approval_id, run_id = approval.id, run.id

    monkeypatch.setattr(coordinator, "launch", lambda _run_id, resume=False: False)
    response = test_client.post(f"/api/approvals/{approval_id}/decide", json={"decision": "approve"})

    assert response.status_code == 503
    assert response.json()["detail"] == "续跑任务暂时无法启动，请重试审批"
    with database.SessionLocal() as db:
        restored_approval = db.get(Approval, approval_id)
        restored_run = db.get(Run, run_id)
        assert restored_approval is not None
        assert restored_approval.status == "pending"
        assert restored_approval.reason == "write access"
        assert restored_approval.decided_at is None
        assert restored_run is not None and restored_run.status == "awaiting_approval"


# 测试场景：验证非法、越界或不满足前置条件的操作会被明确拒绝，且不会产生错误状态；函数名 test_rejecting_delegated_child_approval_settles_its_delegation_and_parent_audit 精确标识本用例的具体条件。
def test_rejecting_delegated_child_approval_settles_its_delegation_and_parent_audit(
    client: tuple[TestClient, dict[str, list]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    test_client, launched = client
    continuation_events: list[dict[str, object] | None] = []
    monkeypatch.setattr(
        coordinator,
        "launch_parent_continuation_if_queued",
        lambda event: continuation_events.append(event) or True,
    )
    workspace_id, agent_id, session_id = _seed()
    with database.SessionLocal() as db:
        parent = Run(
            session_id=session_id,
            workspace_id=workspace_id,
            agent_id=DEFAULT_AGENT_ID,
            status="stopped",
            stop_reason="delegated_child_awaiting_approval",
        )
        child = Run(
            session_id=session_id,
            workspace_id=workspace_id,
            agent_id=agent_id,
            status="awaiting_approval",
        )
        db.add_all([parent, child])
        db.flush()
        task = DelegatedTask(
            parent_run_id=parent.id,
            parent_session_id=session_id,
            child_run_id=child.id,
            child_agent_id=agent_id,
            title="delegated child",
            description="delegated child",
            status="in_progress",
            idempotency_key=f"delegated-child:{child.id}",
            result={
                "child_run_id": child.id,
                "status": "awaiting_approval",
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
        db.add_all([
            RunEvent(
                run_id=child.id,
                event_type="delegation_link",
                payload={
                    "delegation_id": task.id,
                    "parent_run_id": parent.id,
                    "parent_agent_id": DEFAULT_AGENT_ID,
                    "parent_session_id": session_id,
                },
            ),
            RunEvent(
                run_id=child.id,
                event_type="runtime_snapshot",
                payload={
                    "status": "awaiting_approval",
                    "runtime_binding": {
                        "delegation_version": 1,
                        "agent_id": agent_id,
                        "model_connection_id": "child-connection",
                        "provider": "openai_compatible",
                        "model_id": "child-model",
                        "thinking_level": "high",
                        "permission_mode": "smart",
                        "allowed_tool_names": ["read"],
                        "skill_ids": [],
                    },
                    "pending_approval": {
                        "id": "child-call",
                        "tool_name": "write",
                        "arguments": {"path": "child.txt", "content": "secret"},
                    },
                },
            ),
        ])
        approval = Approval(
            run_id=child.id,
            tool_name="write",
            arguments={"path": "child.txt", "content": "secret"},
        )
        db.add(approval)
        db.commit()
        approval_id, task_id, parent_run_id = approval.id, task.id, parent.id

    response = test_client.post(
        f"/api/approvals/{approval_id}/decide",
        json={"decision": "reject", "reason": "not now"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "rejected"
    assert launched["calls"] == []
    assert len(continuation_events) == 1
    assert continuation_events[0] is not None
    assert continuation_events[0]["continuation_queued"] is True
    with database.SessionLocal() as db:
        task = db.get(DelegatedTask, task_id)
        assert task is not None and task.status == "blocked"
        assert task.result["status"] == "stopped"
        assert task.result["binding"]["model_id"] == "child-model"
        assert task.result["binding"]["allowed_tool_names"] == ["read"]
        parent = db.get(Run, parent_run_id)
        assert parent is not None and parent.status == "received"
        assert db.scalar(select(RunEvent).where(
            RunEvent.run_id == parent_run_id,
            RunEvent.event_type == "delegated_child_stopped",
        )) is not None
        # A stopped child is visible through its DelegatedTask and child Run, not
        # injected as a duplicate assistant message in the conversation.
        assert not [
            item for item in db.scalars(select(ChatMessage).where(ChatMessage.session_id == session_id))
            if item.extra.get("delegated_child") is True
        ]


# 测试场景：验证非法、越界或不满足前置条件的操作会被明确拒绝，且不会产生错误状态；函数名 test_approval_decision_rejects_run_not_awaiting_approval 精确标识本用例的具体条件。
def test_approval_decision_rejects_run_not_awaiting_approval(
    client: tuple[TestClient, dict[str, list]],
) -> None:
    test_client, launched = client
    workspace_id, agent_id, session_id = _seed()
    with database.SessionLocal() as db:
        run = Run(session_id=session_id, workspace_id=workspace_id, agent_id=agent_id, status="acting")
        db.add(run)
        db.flush()
        approval = Approval(run_id=run.id, tool_name="write_file", arguments={"path": "a.txt"})
        db.add(approval)
        db.commit()
        approval_id = approval.id

    response = test_client.post(f"/api/approvals/{approval_id}/decide", json={"decision": "approve"})
    assert response.status_code == 409
    assert launched["calls"] == []
    with database.SessionLocal() as db:
        assert db.get(Approval, approval_id).status == "pending"


# 测试场景：验证非法、越界或不满足前置条件的操作会被明确拒绝，且不会产生错误状态；函数名 test_approval_decision_rejects_mismatch_with_latest_runtime_snapshot 精确标识本用例的具体条件。
def test_approval_decision_rejects_mismatch_with_latest_runtime_snapshot(
    client: tuple[TestClient, dict[str, list]],
) -> None:
    test_client, launched = client
    workspace_id, agent_id, session_id = _seed()
    with database.SessionLocal() as db:
        run = Run(session_id=session_id, workspace_id=workspace_id, agent_id=agent_id, status="awaiting_approval")
        db.add(run)
        db.flush()
        approval = Approval(run_id=run.id, tool_name="write_file", arguments={"path": "approved.txt"})
        latest_snapshot = RunEvent(
            run_id=run.id,
            event_type="runtime_snapshot",
            sequence=2,
            created_at=datetime.now(timezone.utc) - timedelta(minutes=1),
            payload={
                "status": "awaiting_approval",
                "pending_approval": {
                    "id": "call-new",
                    "tool_name": "write_file",
                    "arguments": {"path": "different.txt"},
                },
            },
        )
        misleading_newer_timestamp = RunEvent(
            run_id=run.id,
            event_type="runtime_snapshot",
            sequence=1,
            created_at=datetime.now(timezone.utc),
            payload={
                "status": "awaiting_approval",
                "pending_approval": {
                    "id": "call-old",
                    "tool_name": "write_file",
                    "arguments": {"path": "approved.txt"},
                },
            },
        )
        db.add_all([approval, latest_snapshot, misleading_newer_timestamp])
        db.commit()
        approval_id = approval.id

    response = test_client.post(f"/api/approvals/{approval_id}/decide", json={"decision": "approve"})
    assert response.status_code == 409
    assert launched["calls"] == []
    with database.SessionLocal() as db:
        assert db.get(Approval, approval_id).status == "pending"


# 测试场景：验证该正常业务场景从输入准备到结果断言的完整链路；函数名 test_launch_uses_fixed_defaults_when_session_has_no_bindings 精确标识本用例的具体条件。
def test_launch_uses_fixed_defaults_when_session_has_no_bindings(
    client: tuple[TestClient, dict[str, list]],
) -> None:
    test_client, _launched = client
    with database.SessionLocal() as db:
        session = Session(title="Uses defaults")
        db.add(session)
        db.commit()
        session_id = session.id

    response = test_client.post(f"/api/sessions/{session_id}/run", json={"content": "hello"})
    assert response.status_code == 202, response.text
    with database.SessionLocal() as db:
        run = db.get(Run, response.json()["id"])
        assert run is not None
        assert run.agent_id == DEFAULT_AGENT_ID
        assert run.workspace_id == DEFAULT_WORKSPACE_ID
        assert run.mode == "auto"


# 测试场景：会话在两次用户消息之间修改模型设置时，下一次普通 Run 必须重新读取 Session，而非复用上一轮快照。
def test_next_user_turn_resolves_patched_session_model_settings(
    client: tuple[TestClient, dict[str, list]],
) -> None:
    test_client, _launched = client
    with database.SessionLocal() as db:
        first_connection = ModelConnection(
            name="First relay",
            provider="openai_compatible",
            base_url="https://first.example.invalid/v1",
            secret_ref="test:first-relay",
            default_model="first-model",
            thinking_level="low",
            status="connected",
        )
        second_connection = ModelConnection(
            name="Second relay",
            provider="openai_compatible",
            base_url="https://second.example.invalid/v1",
            secret_ref="test:second-relay",
            discovered_models=["second-model", "second-default"],
            default_model="second-default",
            thinking_level="medium",
            status="connected",
        )
        db.add_all([first_connection, second_connection])
        db.flush()
        session = Session(
            title="Switch settings between turns",
            workspace_id=DEFAULT_WORKSPACE_ID,
            agent_id=DEFAULT_AGENT_ID,
            model_connection_id=first_connection.id,
            model_id="first-model",
            thinking_level="low",
        )
        db.add(session)
        db.commit()
        session_id = session.id
        first_connection_id = first_connection.id
        second_connection_id = second_connection.id

    first = test_client.post(
        f"/api/sessions/{session_id}/run",
        json={"content": "first turn", "idempotency_key": "turn-first"},
    )
    assert first.status_code == 202, first.text
    _first_runtime, first_context = RunCoordinator._resolve_runtime(first.json()["id"])
    assert first_context["provider"].model_connection_id == first_connection_id
    assert first_context["provider"].model_id == "first-model"
    assert first_context["provider"].thinking_level == "low"
    with database.SessionLocal() as db:
        first_run = db.get(Run, first.json()["id"])
        assert first_run is not None
        first_run.status = "completed"
        db.add(RunEvent(
            run_id=first_run.id,
            event_type="runtime_snapshot",
            payload={"runtime_binding": first_context["runtime_binding"]},
        ))
        db.commit()

    updated = test_client.patch(
        f"/api/sessions/{session_id}",
        json={
            "model_connection_id": second_connection_id,
            "model_id": "second-model",
            "thinking_level": "high",
        },
    )
    assert updated.status_code == 200, updated.text

    second = test_client.post(
        f"/api/sessions/{session_id}/run",
        json={"content": "second turn", "idempotency_key": "turn-second"},
    )
    assert second.status_code == 202, second.text
    _runtime, context = RunCoordinator._resolve_runtime(second.json()["id"])
    provider = context["provider"]
    assert provider.model_connection_id == second_connection_id
    assert provider.model_id == "second-model"
    assert provider.thinking_level == "high"


# 辅助函数：_draft_payload 封装本组测试重复使用的输入准备、状态查询或测试替身行为。
def _draft_payload(
    *,
    key: str,
    content: str = "Create the project summary",
    title: str = "Create project summary",
    root_path: str | None = None,
    model_connection_id: str | None = None,
) -> dict[str, str]:
    payload = {
        "idempotency_key": key,
        "title": title,
        "content": content,
        "thinking_level": "high",
    }
    if root_path is not None:
        payload["root_path"] = root_path
    if model_connection_id is not None:
        payload["model_connection_id"] = model_connection_id
    return payload


# 辅助函数：_draft_resource_counts 封装本组测试重复使用的输入准备、状态查询或测试替身行为。
def _draft_resource_counts() -> tuple[int, int, int, int]:
    with database.SessionLocal() as db:
        return (
            db.query(Workspace).count(),
            db.query(Session).count(),
            db.query(ChatMessage).count(),
            db.query(DraftLaunch).count(),
        )


# 测试场景：验证该正常业务场景从输入准备到结果断言的完整链路；函数名 test_launch_draft_materializes_once_and_retries_without_rescheduling 精确标识本用例的具体条件。
def test_launch_draft_materializes_once_and_retries_without_rescheduling(
    client: tuple[TestClient, dict[str, list]],
) -> None:
    test_client, launched = client
    payload = _draft_payload(key="draft-repeat")

    first = test_client.post("/api/drafts/launch", json=payload)
    assert first.status_code == 202, first.text
    first_body = first.json()
    assert first_body["reused"] is False
    assert first_body["workspace"]["id"] == DEFAULT_WORKSPACE_ID

    retry = test_client.post("/api/drafts/launch", json=payload)
    assert retry.status_code == 200, retry.text
    retry_body = retry.json()
    assert retry_body["reused"] is True
    assert retry_body["session"]["id"] == first_body["session"]["id"]
    assert retry_body["run"]["id"] == first_body["run"]["id"]
    assert launched["calls"] == [(first_body["run"]["id"], False)]

    with database.SessionLocal() as db:
        session = db.get(Session, first_body["session"]["id"])
        run = db.get(Run, first_body["run"]["id"])
        assert session is not None
        assert session.workspace_id == DEFAULT_WORKSPACE_ID
        assert session.agent_id == DEFAULT_AGENT_ID
        assert session.model_connection_id is None
        assert session.thinking_level == "high"
        assert run is not None and run.agent_id == DEFAULT_AGENT_ID
        messages = list(db.scalars(select(ChatMessage).where(ChatMessage.session_id == session.id)))
        assert [(item.role, item.content) for item in messages] == [("user", payload["content"])]
        assert db.query(DraftLaunch).count() == 1


# 测试场景：验证非法、越界或不满足前置条件的操作会被明确拒绝，且不会产生错误状态；函数名 test_launch_draft_rejects_reusing_key_for_different_request 精确标识本用例的具体条件。
def test_launch_draft_rejects_reusing_key_for_different_request(
    client: tuple[TestClient, dict[str, list]],
) -> None:
    test_client, launched = client
    first = test_client.post("/api/drafts/launch", json=_draft_payload(key="draft-mismatch"))
    assert first.status_code == 202, first.text
    counts_after_first = _draft_resource_counts()

    changed = test_client.post(
        "/api/drafts/launch",
        json=_draft_payload(key="draft-mismatch", title="A different destination"),
    )
    assert changed.status_code == 409
    assert "different draft payload" in changed.json()["detail"]
    assert _draft_resource_counts() == counts_after_first
    assert launched["calls"] == [(first.json()["run"]["id"], False)]


@pytest.mark.parametrize("failure_mode", ["false", "exception"])
# 测试场景：验证状态能够可靠持久化、重放或在重启后恢复，并保持记录之间的关联；函数名 test_launch_draft_marks_persisted_run_failed_when_initial_scheduling_fails 精确标识本用例的具体条件。
def test_launch_draft_marks_persisted_run_failed_when_initial_scheduling_fails(
    client: tuple[TestClient, dict[str, list]],
    monkeypatch: pytest.MonkeyPatch,
    failure_mode: str,
) -> None:
    test_client, launched = client
    attempts: list[tuple[str, bool]] = []

    # 辅助方法：cannot_schedule 实现测试替身在此调用阶段需要的最小行为。
    def cannot_schedule(run_id: str, resume: bool = False) -> bool:
        attempts.append((run_id, resume))
        if failure_mode == "exception":
            raise RuntimeError("coordinator unavailable")
        return False

    monkeypatch.setattr(coordinator, "launch", cannot_schedule)
    payload = _draft_payload(key=f"draft-schedule-{failure_mode}")
    failed = test_client.post("/api/drafts/launch", json=payload)
    # The user turn was durably accepted, so scheduling failure is an
    # execution outcome rather than a transport-level rejection.
    assert failed.status_code == 202, failed.text
    assert failed.json()["run"]["status"] == "failed"
    assert _draft_resource_counts() == (1, 1, 2, 1)

    with database.SessionLocal() as db:
        record = db.scalar(select(DraftLaunch).where(DraftLaunch.idempotency_key == payload["idempotency_key"]))
        assert record is not None and record.run_id is not None
        run = db.get(Run, record.run_id)
        assert run is not None
        assert run.status == "failed"
        assert run.error_code == "launch_unavailable"
        assert run.turn_id is not None
        turn = db.get(ConversationTurn, run.turn_id)
        assert turn is not None and turn.reply_status == "delivered"
        replies = list(db.scalars(select(ChatMessage).where(
            ChatMessage.session_id == run.session_id,
            ChatMessage.role == "assistant",
        )))
        assert len(replies) == 1
        assert replies[0].terminal_for_turn_id == turn.id
        assert "本地执行器未能启动" in replies[0].content
        failed_event = db.scalar(select(RunEvent).where(
            RunEvent.run_id == run.id,
            RunEvent.event_type == "integration_failed",
        ))
        assert failed_event is not None
        if failure_mode == "exception":
            assert failed_event.payload["exception_type"] == "RuntimeError"
            assert failed_event.payload["internal_error"] == "coordinator unavailable"
            assert "coordinator unavailable" not in replies[0].content
        else:
            assert "internal_error" not in failed_event.payload
        run_id = run.id

    retry = test_client.post("/api/drafts/launch", json=payload)
    assert retry.status_code == 200, retry.text
    assert retry.json()["run"]["id"] == run_id
    assert retry.json()["run"]["status"] == "failed"
    assert _draft_resource_counts() == (1, 1, 2, 1)
    assert attempts == [(run_id, False)]
    assert launched["calls"] == []


# 测试场景：验证该正常业务场景从输入准备到结果断言的完整链路；函数名 test_launch_draft_reuses_project_for_distinct_keys_with_same_normalized_root 精确标识本用例的具体条件。
def test_launch_draft_reuses_project_for_distinct_keys_with_same_normalized_root(
    client: tuple[TestClient, dict[str, list]], tmp_path: Path
) -> None:
    test_client, _launched = client
    project_root = tmp_path / "project-root"
    project_root.mkdir()
    first = test_client.post(
        "/api/drafts/launch",
        json=_draft_payload(key="draft-project-one", root_path=str(project_root / ".")),
    )
    second = test_client.post(
        "/api/drafts/launch",
        json=_draft_payload(key="draft-project-two", root_path=str(project_root)),
    )
    assert first.status_code == 202, first.text
    assert second.status_code == 202, second.text
    assert first.json()["workspace"]["id"] == second.json()["workspace"]["id"]
    assert first.json()["workspace"]["root_path"] == str(project_root.resolve())
    with database.SessionLocal() as db:
        assert db.query(Workspace).count() == 2  # default one-off workspace + selected project


# 测试场景：验证非法、越界或不满足前置条件的操作会被明确拒绝，且不会产生错误状态；函数名 test_invalid_or_unavailable_draft_launch_leaves_no_rows 精确标识本用例的具体条件。
def test_invalid_or_unavailable_draft_launch_leaves_no_rows(
    client: tuple[TestClient, dict[str, list]],
) -> None:
    test_client, launched = client
    before = _draft_resource_counts()

    invalid = test_client.post(
        "/api/drafts/launch",
        json=_draft_payload(key="draft-invalid", title="   "),
    )
    assert invalid.status_code == 422
    assert _draft_resource_counts() == before

    unavailable = test_client.post(
        "/api/drafts/launch",
        json=_draft_payload(key="draft-unavailable", model_connection_id="not-a-connection"),
    )
    assert unavailable.status_code == 409
    assert _draft_resource_counts() == before
    assert launched["calls"] == []


# 测试场景：验证该正常业务场景从输入准备到结果断言的完整链路；函数名 test_session_context_recalculates_messages_when_cached_count_is_zero 精确标识本用例的具体条件。
def test_session_context_recalculates_messages_when_cached_count_is_zero(
    client: tuple[TestClient, dict[str, list]],
) -> None:
    test_client, _launched = client
    _workspace_id, _agent_id, session_id = _seed()
    with database.SessionLocal() as db:
        session = db.get(Session, session_id)
        assert session is not None
        session.context_tokens = 0
        db.add_all([
            ChatMessage(session_id=session_id, role="user", content="old question"),
            ChatMessage(session_id=session_id, role="assistant", content="old answer"),
        ])
        db.commit()

    response = test_client.get(f"/api/sessions/{session_id}/context")
    assert response.status_code == 200
    body = response.json()
    assert body["used_tokens"] > 0
    assert body["limit_tokens"] == 200_000
    assert body["compact_threshold_tokens"] == 180_000
    assert body["percent"] > 0
    assert body["last_compaction_at"] is None
    with database.SessionLocal() as db:
        session = db.get(Session, session_id)
        assert session is not None and session.context_tokens == body["used_tokens"]


# 测试场景：验证时间、容量或上下文预算边界以及达到边界后的可观察处理结果；函数名 test_session_context_get_reports_threshold_without_mutating_history 精确标识本用例的具体条件。
def test_session_context_get_reports_threshold_without_mutating_history(
    client: tuple[TestClient, dict[str, list]],
) -> None:
    test_client, _launched = client
    _workspace_id, _agent_id, session_id = _seed()
    old_time = datetime.now(timezone.utc)
    with database.SessionLocal() as db:
        db.add_all([
            ChatMessage(
                session_id=session_id,
                role="user",
                content="界" * 90_100,
                created_at=old_time,
            ),
            ChatMessage(
                session_id=session_id,
                role="user",
                content="keep latest",
                created_at=old_time + timedelta(microseconds=1),
            ),
        ])
        db.commit()

    response = test_client.get(f"/api/sessions/{session_id}/context")
    assert response.status_code == 200
    body = response.json()
    assert body["used_tokens"] >= body["compact_threshold_tokens"]
    assert body["last_compaction_at"] is None
    with database.SessionLocal() as db:
        session = db.get(Session, session_id)
        assert session is not None
        assert session.context_tokens == body["used_tokens"]
        assert db.query(ConversationCompaction).filter_by(session_id=session_id).count() == 0
