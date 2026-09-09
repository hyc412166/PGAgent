"""验证运行协调器的启动恢复、结果持久化、压缩提交、审批等待、用量聚合、指令与记忆冻结以及终态一致性。

测试通过 fixture 或辅助函数准备隔离环境，再调用真实服务、路由或运行时，并检查返回值、持久化状态与可观察副作用。
变量约定：tmp_path/monkeypatch 提供隔离环境，client/store/runtime 驱动被测链路，各类 *_id 串联持久化实体，payload 表示输入，response/result 表示实际输出，expected 表示期望值。
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import select

from src.persistence import database
from src.persistence.database import (
    Agent,
    Approval,
    Artifact,
    ConversationCompaction,
    Base,
    ChatMessage,
    ConversationTurn,
    DelegatedTask,
    MemoryJob,
    MemorySettings,
    ModelConnection,
    Run,
    RunEvent,
    Session,
    UsageRecord,
    Workspace,
)
from src.agent import RunOutcome
from src.runs.service import RunCoordinator, _prepare_session_history
from src.runs import lifecycle as lifecycle_service
from src.tasks.state import sync_todos_for_run
from src.context import instructions as instruction_service
from src.agent import AgentRuntime, decide_deterministic_completion
from src.tools import create_default_registry


@pytest.mark.asyncio
# 测试场景：验证取消或终止请求会收敛相关运行状态，并正确清理或保留应有资源；函数名 test_coordinator_start_and_shutdown_bind_the_active_event_loop 精确标识本用例的具体条件。
async def test_coordinator_start_and_shutdown_bind_the_active_event_loop() -> None:
    coordinator = RunCoordinator()

    coordinator.start()

    assert coordinator._event_loop is asyncio.get_running_loop()
    assert not coordinator._shutting_down

    await coordinator.shutdown()

    assert coordinator._event_loop is None
    assert coordinator._shutting_down


@pytest.mark.asyncio
# 测试场景：验证该正常业务场景从输入准备到结果断言的完整链路；函数名 test_resume_is_queued_until_the_original_run_task_finishes 精确标识本用例的具体条件。
async def test_resume_is_queued_until_the_original_run_task_finishes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    coordinator = RunCoordinator()
    execute_started = asyncio.Event()
    release_execute = asyncio.Event()
    resume_started = asyncio.Event()

    # 局部测试函数：fake_execute 模拟该步骤的返回结果或异常。
    async def fake_execute(_run_id: str) -> None:
        execute_started.set()
        await release_execute.wait()

    # 局部测试函数：fake_resume 模拟该步骤的返回结果或异常。
    async def fake_resume(_run_id: str) -> None:
        resume_started.set()

    monkeypatch.setattr(coordinator, "_execute", fake_execute)
    monkeypatch.setattr(coordinator, "_resume", fake_resume)

    assert coordinator.launch("run-1") is True
    await execute_started.wait()
    assert coordinator.launch("run-1", resume=True) is True
    assert not resume_started.is_set()

    release_execute.set()
    await asyncio.wait_for(resume_started.wait(), timeout=1)
    await coordinator.shutdown()


@pytest.fixture()
# 测试夹具：seeded_run 创建本组用例共享的隔离资源，并在测试结束后恢复数据库、配置或进程状态。
def seeded_run(tmp_path: Path) -> tuple[str, str]:
    # 临时数据库中的 session/run 标识组成夹具返回值，供用例驱动协调器并核对消息与终态持久化。
    database.configure_database(f"sqlite:///{(tmp_path / 'run-service.db').as_posix()}")
    database.init_db()
    with database.SessionLocal() as db:
        workspace = Workspace(name="Demo", root_path=str(tmp_path / "workspace"))
        db.add(workspace)
        db.flush()
        agent = Agent(name="Builder", workspace_id=workspace.id)
        db.add(agent)
        db.flush()
        session = Session(title="Chat", workspace_id=workspace.id, agent_id=agent.id)
        db.add(session)
        db.flush()
        run = Run(session_id=session.id, workspace_id=workspace.id, agent_id=agent.id)
        db.add(run)
        db.commit()
        ids = (run.id, session.id)
    yield ids
    Base.metadata.drop_all(bind=database.engine)


@pytest.fixture()
# 测试夹具：accepted_run 创建本组用例共享的隔离资源，并在测试结束后恢复数据库、配置或进程状态。
def accepted_run(tmp_path: Path) -> tuple[str, str]:
    """A legacy-shaped accepted user turn without the new delivery linkage."""

    # session_id 与 run_id 指向旧结构的已接收轮次，用于验证兼容修复不会依赖新的投递关联字段。
    database.configure_database(f"sqlite:///{(tmp_path / 'accepted-run.db').as_posix()}")
    database.init_db()
    with database.SessionLocal() as db:
        workspace = Workspace(name="Demo", root_path=str(tmp_path / "workspace"))
        db.add(workspace)
        db.flush()
        agent = Agent(name="Builder", workspace_id=workspace.id)
        db.add(agent)
        db.flush()
        session = Session(title="Chat", workspace_id=workspace.id, agent_id=agent.id)
        db.add(session)
        db.flush()
        db.add(ChatMessage(session_id=session.id, role="user", content="完成这个任务", sequence=1))
        run = Run(session_id=session.id, workspace_id=workspace.id, agent_id=agent.id)
        db.add(run)
        db.commit()
        ids = (run.id, session.id)
    yield ids
    Base.metadata.drop_all(bind=database.engine)


# 测试场景：验证状态能够可靠持久化、重放或在重启后恢复，并保持记录之间的关联；函数名 test_completed_outcome_persists_snapshot_and_assistant_message 精确标识本用例的具体条件。
def test_completed_outcome_persists_snapshot_and_assistant_message(seeded_run: tuple[str, str]) -> None:
    run_id, session_id = seeded_run
    outcome = RunOutcome(
        status="completed",
        output="任务完成",
        messages=[{
            "role": "assistant",
            "content": "任务完成",
            "reasoning_content": "verified the requested files and tests",
        }],
        events=[{"type": "run_completed"}],
        steps=2,
        tool_calls=1,
        guard_snapshot={"steps": 2, "calls": 1},
    )
    RunCoordinator._persist_outcome(run_id, outcome)

    with database.SessionLocal() as db:
        run = db.get(Run, run_id)
        assert run is not None and run.status == "completed"
        assert run.current_step == 2 and run.tool_calls == 1
        message = db.scalar(select(ChatMessage).where(
            ChatMessage.session_id == session_id,
            ChatMessage.role == "assistant",
        ))
        assert message is not None and message.content == "任务完成"
        assert message.provider_payload == {
            "reasoning_content": "verified the requested files and tests"
        }
        session_row = db.get(Session, session_id)
        assert session_row is not None
        prepared = _prepare_session_history(db, session_row)
        assert prepared[-1]["reasoning_content"] == "verified the requested files and tests"
        session = db.get(Session, session_id)
        assert session is not None and session.context_tokens > 0
        snapshot = db.scalar(select(RunEvent).where(RunEvent.run_id == run_id, RunEvent.event_type == "runtime_snapshot"))
        assert snapshot is not None and snapshot.payload["guard_snapshot"]["calls"] == 1


# 测试场景：验证状态能够可靠持久化、重放或在重启后恢复，并保持记录之间的关联；函数名 test_terminal_stop_without_model_output_still_persists_one_user_reply 精确标识本用例的具体条件。
def test_terminal_stop_without_model_output_still_persists_one_user_reply(
    accepted_run: tuple[str, str],
) -> None:
    run_id, session_id = accepted_run
    outcome = RunOutcome(
        status="stopped",
        output=None,
        messages=[],
        transcript_delta=[],
        events=[{"type": "run_stopped", "code": "acceptance_failed"}],
        steps=3,
        tool_calls=0,
        stop_reason="acceptance_failed",
        error="candidate did not meet acceptance criteria",
    )

    RunCoordinator._persist_outcome(run_id, outcome)
    RunCoordinator._persist_outcome(run_id, outcome)

    with database.SessionLocal() as db:
        replies = list(db.scalars(select(ChatMessage).where(
            ChatMessage.session_id == session_id,
            ChatMessage.role == "assistant",
        )))
        assert len(replies) == 1
        assert replies[0].extra["run_id"] == run_id
        assert replies[0].extra["error_code"] == "acceptance_failed"
        assert replies[0].extra["trace_id"]
        assert "追踪号" in replies[0].content


# 测试场景：验证失败会保留可诊断信息并收敛为一致、可恢复的状态；函数名 test_completed_empty_output_is_failed_and_receives_deterministic_reply 精确标识本用例的具体条件。
def test_completed_empty_output_is_failed_and_receives_deterministic_reply(
    accepted_run: tuple[str, str],
) -> None:
    run_id, session_id = accepted_run

    RunCoordinator._persist_outcome(run_id, RunOutcome(
        status="completed",
        output="   ",
        messages=[],
        transcript_delta=[],
        events=[{"type": "run_completed"}],
        steps=1,
        tool_calls=0,
    ))

    with database.SessionLocal() as db:
        run = db.get(Run, run_id)
        assert run is not None and run.status == "failed"
        assert run.error_code == "empty_model_output"
        reply = db.scalar(select(ChatMessage).where(
            ChatMessage.session_id == session_id,
            ChatMessage.role == "assistant",
        ))
        assert reply is not None
        assert "没有返回可用内容" in reply.content


# 测试场景：验证并发或批量执行时的顺序、隔离性和最终状态一致性；函数名 test_integration_failure_reply_is_traceable_idempotent_and_sanitized 精确标识本用例的具体条件。
def test_integration_failure_reply_is_traceable_idempotent_and_sanitized(
    accepted_run: tuple[str, str],
) -> None:
    run_id, session_id = accepted_run

    RunCoordinator._persist_failure(run_id, RuntimeError("private-provider-payload"))
    RunCoordinator._persist_failure(run_id, RuntimeError("private-provider-payload"))

    with database.SessionLocal() as db:
        run = db.get(Run, run_id)
        assert run is not None and run.status == "failed"
        assert "private-provider-payload" not in str(run.error_message)
        replies = list(db.scalars(select(ChatMessage).where(
            ChatMessage.session_id == session_id,
            ChatMessage.role == "assistant",
        )))
        assert len(replies) == 1
        assert "private-provider-payload" not in replies[0].content
        assert "追踪号" in replies[0].content


# 测试场景：验证失败会保留可诊断信息并收敛为一致、可恢复的状态；函数名 test_model_failure_status_code_is_preserved_for_classification 精确标识本用例的具体条件。
def test_model_failure_status_code_is_preserved_for_classification(
    accepted_run: tuple[str, str],
) -> None:
    run_id, session_id = accepted_run
    outcome = RunOutcome(
        status="failed",
        output=None,
        messages=[],
        transcript_delta=[],
        events=[{
            "type": "model_failed",
            "error_type": "InternalServerError",
            "status_code": 502,
            "retry_exhausted": True,
        }],
        error="private upstream response",
        steps=1,
        tool_calls=0,
    )

    RunCoordinator._persist_outcome(run_id, outcome)

    with database.SessionLocal() as db:
        run = db.get(Run, run_id)
        assert run is not None and run.error_code == "model_unavailable"
        assert "private upstream response" not in str(run.error_message)
        reply = db.scalar(select(ChatMessage).where(
            ChatMessage.session_id == session_id,
            ChatMessage.role == "assistant",
        ))
        assert reply is not None
        assert "private upstream response" not in reply.content


# 测试场景：Responses 流式重试耗尽后应明确归类为模型服务不可用，而不是 Agent 内部错误。
def test_stream_interrupted_failure_is_classified_as_model_unavailable(
    accepted_run: tuple[str, str],
) -> None:
    run_id, session_id = accepted_run
    outcome = RunOutcome(
        status="failed",
        output=None,
        messages=[],
        transcript_delta=[],
        events=[{
            "type": "model_failed",
            "error_type": "StreamInterrupted",
            "retryable": True,
            "retry_exhausted": True,
        }],
        error="StreamInterrupted",
        steps=1,
        tool_calls=0,
    )

    RunCoordinator._persist_outcome(run_id, outcome)

    with database.SessionLocal() as db:
        run = db.get(Run, run_id)
        assert run is not None and run.error_code == "model_unavailable"
        reply = db.scalar(select(ChatMessage).where(
            ChatMessage.session_id == session_id,
            ChatMessage.role == "assistant",
        ))
        assert reply is not None
        assert "模型服务暂时不可用" in reply.content


@pytest.mark.parametrize(
    ("provider_error_code", "error_kind", "expected_code", "expected_message"),
    [
        ("rate_limit_exceeded", "rate_limit", "model_rate_limited", "模型服务当前请求过多"),
        ("vector_store_timeout", "timeout", "model_timeout", "模型服务响应超时"),
        ("max_output_tokens", "unknown", "model_output_limit", "模型输出达到长度上限"),
        ("content_filter", "unknown", "model_content_filtered", "模型输出被内容安全策略阻止"),
        ("bio_policy", "invalid_request", "model_content_filtered", "模型输出被内容安全策略阻止"),
        ("invalid_prompt", "invalid_request", "model_input_error", "模型服务无法处理本次输入内容"),
        (
            "data_residency_mismatch",
            "invalid_request",
            "model_data_residency_error",
            "模型服务的数据驻留配置与本次请求不匹配",
        ),
        ("vendor_specific_failure", "unknown", "model_provider_error", "模型服务返回了无法识别的错误"),
    ],
)
# 测试场景：模型返回的稳定错误码必须映射为明确用户提示，同时不泄露供应商原始正文。
def test_provider_response_errors_have_specific_public_messages(
    accepted_run: tuple[str, str],
    provider_error_code: str,
    error_kind: str,
    expected_code: str,
    expected_message: str,
) -> None:
    run_id, session_id = accepted_run
    outcome = RunOutcome(
        status="failed",
        output=None,
        messages=[],
        transcript_delta=[],
        events=[{
            "type": "model_failed",
            "error_type": "IncompleteResponse",
            "error_kind": error_kind,
            "provider_error_code": provider_error_code,
            "retryable": error_kind in {"rate_limit", "timeout", "server"},
            "retry_exhausted": error_kind in {"rate_limit", "timeout", "server"},
        }],
        error="private provider response body",
        steps=1,
        tool_calls=0,
    )

    RunCoordinator._persist_outcome(run_id, outcome)

    with database.SessionLocal() as db:
        run = db.get(Run, run_id)
        assert run is not None and run.error_code == expected_code
        assert "private provider response body" not in str(run.error_message)
        reply = db.scalar(select(ChatMessage).where(
            ChatMessage.session_id == session_id,
            ChatMessage.role == "assistant",
        ))
        assert reply is not None
        assert expected_message in reply.content
        assert "private provider response body" not in reply.content


# 测试场景：验证失败会保留可诊断信息并收敛为一致、可恢复的状态；函数名 test_parent_failure_fallback_summarizes_mixed_delegated_results 精确标识本用例的具体条件。
def test_parent_failure_fallback_summarizes_mixed_delegated_results(
    accepted_run: tuple[str, str],
) -> None:
    run_id, session_id = accepted_run
    with database.SessionLocal() as db:
        db.add_all([
            DelegatedTask(
                parent_run_id=run_id,
                parent_session_id=session_id,
                title="添加背景音乐",
                description="",
                status="completed",
                idempotency_key="delivery-task-a",
                result={"status": "completed", "workspace_changed": True},
            ),
            DelegatedTask(
                parent_run_id=run_id,
                parent_session_id=session_id,
                title="添加动态音效",
                description="",
                status="blocked",
                idempotency_key="delivery-task-b",
                result={"status": "failed", "error_code": "tool_error"},
            ),
        ])
        db.commit()

    RunCoordinator._persist_failure(run_id, RuntimeError("parent synthesis failed"))

    with database.SessionLocal() as db:
        reply = db.scalar(select(ChatMessage).where(
            ChatMessage.session_id == session_id,
            ChatMessage.role == "assistant",
        ))
        assert reply is not None
        assert "添加背景音乐：已完成" in reply.content
        assert "添加动态音效：未完成" in reply.content
        assert "部分修改" in reply.content
        turn = db.get(ConversationTurn, reply.turn_id)
        assert turn is not None and turn.execution_status == "partial_failure"


# 测试场景：验证该正常业务场景从输入准备到结果断言的完整链路；函数名 test_reconciler_repairs_legacy_terminal_run_without_reply 精确标识本用例的具体条件。
def test_reconciler_repairs_legacy_terminal_run_without_reply(
    accepted_run: tuple[str, str],
) -> None:
    run_id, session_id = accepted_run
    with database.SessionLocal() as db:
        run = db.get(Run, run_id)
        assert run is not None
        run.status = "failed"
        run.error_code = "model_timeout"
        run.error_message = "模型服务响应超时。"
        run.finished_at = datetime.now(timezone.utc)
        db.commit()

    # The periodic watchdog checks only linked pending turns; startup performs
    # the one-time legacy adoption scan.
    assert RunCoordinator.reconcile_terminal_deliveries(include_legacy=False) == 0
    assert RunCoordinator.reconcile_terminal_deliveries() == 1
    assert RunCoordinator.reconcile_terminal_deliveries() == 0

    with database.SessionLocal() as db:
        replies = list(db.scalars(select(ChatMessage).where(
            ChatMessage.session_id == session_id,
            ChatMessage.role == "assistant",
        )))
        assert len(replies) == 1
        assert "模型服务响应超时" in replies[0].content


# 测试场景：验证该正常业务场景从输入准备到结果断言的完整链路；函数名 test_watchdog_settles_an_accepted_orphaned_root_run 精确标识本用例的具体条件。
def test_watchdog_settles_an_accepted_orphaned_root_run(
    accepted_run: tuple[str, str],
) -> None:
    run_id, session_id = accepted_run
    coordinator = RunCoordinator()

    assert coordinator.reconcile_orphaned_runs(grace_seconds=0) == [run_id]

    with database.SessionLocal() as db:
        run = db.get(Run, run_id)
        assert run is not None and run.status == "failed"
        assert run.error_code == "coordinator_orphaned"
        reply = db.scalar(select(ChatMessage).where(
            ChatMessage.session_id == session_id,
            ChatMessage.role == "assistant",
        ))
        assert reply is not None and "失去运行进程" in reply.content


# 测试场景：验证状态能够可靠持久化、重放或在重启后恢复，并保持记录之间的关联；函数名 test_restart_recovery_persists_exactly_one_terminal_reply 精确标识本用例的具体条件。
def test_restart_recovery_persists_exactly_one_terminal_reply(
    accepted_run: tuple[str, str],
) -> None:
    run_id, session_id = accepted_run

    assert RunCoordinator.reconcile_interrupted_runs() == []
    assert RunCoordinator.reconcile_interrupted_runs() == []

    with database.SessionLocal() as db:
        run = db.get(Run, run_id)
        assert run is not None and run.status == "stopped"
        assert run.stop_reason == "interrupted_restart"
        turn = db.get(ConversationTurn, run.turn_id)
        assert turn is not None and turn.reply_status == "delivered"
        replies = list(db.scalars(select(ChatMessage).where(
            ChatMessage.session_id == session_id,
            ChatMessage.terminal_for_turn_id == turn.id,
        )))
        assert len(replies) == 1
        assert replies[0].extra["error_code"] == "interrupted_restart"
        assert "执行期间重启" in replies[0].content


@pytest.mark.parametrize(
    ("status", "output", "stop_reason"),
    [
        ("completed", "已完成", None),
        ("failed", None, None),
        ("stopped", None, "user_interrupted"),
    ],
)
# 测试场景：验证该正常业务场景从输入准备到结果断言的完整链路；函数名 test_every_terminal_root_turn_has_one_delivered_terminal_message 精确标识本用例的具体条件。
def test_every_terminal_root_turn_has_one_delivered_terminal_message(
    accepted_run: tuple[str, str],
    status: str,
    output: str | None,
    stop_reason: str | None,
) -> None:
    run_id, _session_id = accepted_run
    outcome = RunOutcome(
        status=status,
        output=output,
        messages=[{"role": "assistant", "content": output}] if output else [],
        transcript_delta=[],
        events=[{"type": f"run_{status}"}],
        steps=1,
        tool_calls=0,
        stop_reason=stop_reason,
        error="terminal test failure" if status != "completed" else None,
    )

    RunCoordinator._persist_outcome(run_id, outcome)
    RunCoordinator._persist_outcome(run_id, outcome)

    with database.SessionLocal() as db:
        run = db.get(Run, run_id)
        assert run is not None and run.turn_id is not None
        turn = db.get(ConversationTurn, run.turn_id)
        assert turn is not None
        assert turn.execution_status == status
        assert turn.reply_status == "delivered"
        assert turn.finished_at is not None
        replies = list(db.scalars(select(ChatMessage).where(
            ChatMessage.terminal_for_turn_id == turn.id,
        )))
        assert len(replies) == 1
        assert replies[0].turn_id == turn.id


# 测试场景：验证该正常业务场景从输入准备到结果断言的完整链路；函数名 test_main_agent_installs_deterministic_gate_without_model_or_task_anchor 精确标识本用例的具体条件。
def test_main_agent_installs_deterministic_gate_without_model_or_task_anchor(tmp_path: Path) -> None:
    runtime = AgentRuntime(
        model_call=lambda **_kwargs: None,
        tool_registry=create_default_registry(str(tmp_path), allowed_tool_names=[]),
    )
    RunCoordinator._install_completion_verifier(runtime, {"runtime_binding": {}})
    assert runtime.completion_verifier is decide_deterministic_completion


# 测试场景：验证非法、越界或不满足前置条件的操作会被明确拒绝，且不会产生错误状态；函数名 test_rejected_candidates_are_not_persisted_as_chat_messages 精确标识本用例的具体条件。
def test_rejected_candidates_are_not_persisted_as_chat_messages(seeded_run: tuple[str, str]) -> None:
    run_id, session_id = seeded_run
    outcome = RunOutcome(
        status="stopped",
        output=None,
        messages=[
            {"role": "assistant", "content": "unsupported candidate"},
            {"role": "user", "content": "[内部验收反馈] missing evidence"},
            {"role": "assistant", "content": "still unsupported"},
        ],
        transcript_delta=[],
        events=[{"type": "run_stopped", "code": "acceptance_failed"}],
        steps=3,
        tool_calls=0,
        stop_reason="acceptance_failed",
        error="candidate did not meet acceptance criteria",
        acceptance_report={"stage": "deterministic", "passed": False},
        completion_verification_attempts=3,
    )
    RunCoordinator._persist_outcome(run_id, outcome)

    with database.SessionLocal() as db:
        assert not list(db.scalars(select(ChatMessage).where(ChatMessage.session_id == session_id)))
        snapshot = db.scalar(select(RunEvent).where(
            RunEvent.run_id == run_id,
            RunEvent.event_type == "runtime_snapshot",
        ))
        assert snapshot is not None
        assert snapshot.payload["acceptance_report"]["passed"] is False
        assert snapshot.payload["completion_verification_attempts"] == 3
        assert snapshot.payload["messages"] == []
        assert "unsupported candidate" not in str(snapshot.payload)


# 测试场景：验证状态能够可靠持久化、重放或在重启后恢复，并保持记录之间的关联；函数名 test_single_compaction_is_persisted_and_transcript_tail_is_loaded 精确标识本用例的具体条件。
def test_single_compaction_is_persisted_and_transcript_tail_is_loaded(
    seeded_run: tuple[str, str],
) -> None:
    run_id, session_id = seeded_run
    with database.SessionLocal() as db:
        session = db.get(Session, session_id)
        run = db.get(Run, run_id)
        assert session is not None and run is not None
        db.add(ChatMessage(session_id=session_id, role="user", content="old request", sequence=1))
        db.commit()

    outcome = RunOutcome(
        status="completed",
        output="new answer",
        messages=[
            {"role": "user", "content": "old request"},
            {"role": "assistant", "content": "new answer"},
        ],
        events=[{
            "type": "context_compaction_finished",
            "reason": "threshold",
            "before_tokens": 10_000,
            "after_tokens": 800,
            "used_model": True,
            "fallback": False,
            "effective": True,
            "attempts": 1,
        }],
        steps=1,
        tool_calls=0,
        compaction_state={
            "schema": "pgagent_nine_section_v2",
            "base_sequence": 1,
            "source_delta_count": 0,
            "source_sequence": 1,
            "active_request": "old request",
            "todo_state": [],
            "summary": "request was answered",
            "messages": [{
                "role": "user",
                "content": '<continuation-summary format="pgagent-nine-section-v1" transcript-artifact="artifact:test">\nrequest was answered\n</continuation-summary>',
            }],
            "transcript_artifact": {},
            "artifact_refs": [],
            "removed_message_count": 1,
        },
    )
    RunCoordinator._persist_outcome(run_id, outcome)

    with database.SessionLocal() as db:
        session = db.get(Session, session_id)
        assert session is not None
        assert db.query(ConversationCompaction).filter_by(session_id=session_id).count() == 1
        assert _prepare_session_history(db, session) == [
            {"role": "user", "content": '<continuation-summary format="pgagent-nine-section-v1" transcript-artifact="artifact:test">\nrequest was answered\n</continuation-summary>'},
            {"role": "assistant", "content": "new answer"},
        ]


# 测试场景：验证该正常业务场景从输入准备到结果断言的完整链路；函数名 test_legacy_compaction_row_still_reconstructs_provider_history 精确标识本用例的具体条件。
def test_legacy_compaction_row_still_reconstructs_provider_history(
    seeded_run: tuple[str, str],
) -> None:
    _run_id, session_id = seeded_run
    legacy = "<compacted-context>legacy continuation</compacted-context>"
    with database.SessionLocal() as db:
        session = db.get(Session, session_id)
        db.add(ConversationCompaction(
            session_id=session_id,
            source_sequence=1,
            active_request="legacy task",
            continuation_messages=[{"role": "user", "content": legacy}],
        ))
        db.add(ChatMessage(
            session_id=session_id,
            role="assistant",
            content="tail answer",
            sequence=2,
        ))
        db.commit()
        assert _prepare_session_history(db, session) == [
            {"role": "user", "content": legacy},
            {"role": "assistant", "content": "tail answer"},
        ]


# 测试场景：验证状态能够可靠持久化、重放或在重启后恢复，并保持记录之间的关联；函数名 test_context_artifact_metadata_is_persisted 精确标识本用例的具体条件。
def test_context_artifact_metadata_is_persisted(seeded_run: tuple[str, str], tmp_path: Path) -> None:
    run_id, session_id = seeded_run
    artifact_path = tmp_path / "artifact.txt"
    artifact_path.write_text("full output", encoding="utf-8")
    RunCoordinator._persist_outcome(run_id, RunOutcome(
        status="completed",
        output="answer",
        messages=[{"role": "assistant", "content": "answer"}],
        events=[{"type": "run_completed"}],
        steps=1,
        tool_calls=0,
        artifact_refs=[{
            "artifact_id": "artifact-test",
            "kind": "tool_output",
            "storage_key": str(artifact_path),
            "sha256": "abc",
            "size": 11,
            "preview": "full output",
        }],
    ))

    with database.SessionLocal() as db:
        artifact = db.scalar(select(Artifact).where(Artifact.session_id == session_id))
        assert artifact is not None
        assert artifact.storage_path == str(artifact_path)
        assert artifact.metadata_json["runtime_artifact_id"] == "artifact-test"


# 测试场景：验证非法、越界或不满足前置条件的操作会被明确拒绝，且不会产生错误状态；函数名 test_compaction_is_rejected_when_transcript_changed_during_model_call 精确标识本用例的具体条件。
def test_compaction_is_rejected_when_transcript_changed_during_model_call(
    seeded_run: tuple[str, str],
) -> None:
    run_id, session_id = seeded_run
    with database.SessionLocal() as db:
        db.add(ChatMessage(session_id=session_id, role="user", content="concurrent", sequence=2))
        db.commit()

    RunCoordinator._persist_outcome(run_id, RunOutcome(
        status="completed",
        output="answer",
        messages=[{"role": "assistant", "content": "answer"}],
        events=[{
            "type": "context_compaction_finished",
            "reason": "threshold",
            "before_tokens": 100,
            "after_tokens": 50,
            "effective": True,
            "attempts": 1,
        }],
        steps=1,
        tool_calls=0,
        compaction_state={
            "schema": "claude_compaction_v1",
            "base_sequence": 1,
            "source_delta_count": 0,
            "source_sequence": 1,
            "active_request": "stale",
            "summary": "stale",
            "messages": [{"role": "user", "content": "<compacted-context>stale</compacted-context>"}],
        },
    ))

    with database.SessionLocal() as db:
        assert db.query(ConversationCompaction).filter_by(session_id=session_id).count() == 0
        assert db.query(RunEvent).filter_by(run_id=run_id, event_type="context_compaction_conflict").count() == 1


# 测试场景：验证状态能够可靠持久化、重放或在重启后恢复，并保持记录之间的关联；函数名 test_transcript_delta_is_persisted_when_provider_messages_were_compacted 精确标识本用例的具体条件。
def test_transcript_delta_is_persisted_when_provider_messages_were_compacted(
    seeded_run: tuple[str, str],
) -> None:
    run_id, session_id = seeded_run
    raw_tool_output = "full tool output " + ("z" * 5_000)
    outcome = RunOutcome(
        status="completed",
        output="final answer",
        # Simulate the provider view after semantic compaction.
        messages=[{"role": "assistant", "content": "final answer"}],
        transcript_delta=[
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{
                    "id": "read-1",
                    "type": "function",
                    "function": {"name": "read_file", "arguments": "{}"},
                }],
            },
            {"role": "tool", "tool_call_id": "read-1", "name": "read_file", "content": raw_tool_output},
            {"role": "assistant", "content": "final answer"},
        ],
        events=[{"type": "run_completed"}],
        steps=2,
        tool_calls=1,
    )
    RunCoordinator._persist_outcome(run_id, outcome)

    with database.SessionLocal() as db:
        rows = list(db.scalars(
            select(ChatMessage)
            .where(ChatMessage.session_id == session_id)
            .order_by(ChatMessage.sequence)
        ))
        assert [row.role for row in rows] == ["assistant", "tool", "assistant"]
        assert rows[1].content == raw_tool_output
        assert [row.sequence for row in rows] == [1, 2, 3]


# 测试场景：验证状态能够可靠持久化、重放或在重启后恢复，并保持记录之间的关联；函数名 test_responses_native_items_are_persisted_without_visible_content_deduplication 精确标识本用例的具体条件。
def test_responses_native_items_are_persisted_without_visible_content_deduplication(
    seeded_run: tuple[str, str],
) -> None:
    run_id, session_id = seeded_run
    first_native = {
        "protocol": "responses",
        "items": [{
            "type": "message",
            "id": "msg-1",
            "role": "assistant",
            "status": "completed",
            "content": [{"type": "output_text", "text": "same", "annotations": []}],
        }],
    }
    second_native = {
        "protocol": "responses",
        "items": [{
            "type": "message",
            "id": "msg-2",
            "role": "assistant",
            "status": "completed",
            "content": [{"type": "output_text", "text": "same", "annotations": []}],
        }],
    }

    RunCoordinator._persist_outcome(run_id, RunOutcome(
        status="completed",
        output="same",
        messages=[{"role": "assistant", "content": "same", "_pgagent_provider": second_native}],
        transcript_delta=[
            {"role": "assistant", "content": "same", "_pgagent_provider": first_native},
            {"role": "assistant", "content": "same", "_pgagent_provider": second_native},
        ],
        events=[{"type": "run_completed"}],
        steps=1,
        tool_calls=0,
    ))

    with database.SessionLocal() as db:
        rows = list(db.scalars(
            select(ChatMessage)
            .where(ChatMessage.session_id == session_id)
            .order_by(ChatMessage.sequence)
        ))
        assert len(rows) == 2
        assert [row.provider_payload["native"]["items"][0]["id"] for row in rows] == ["msg-1", "msg-2"]


# 测试场景：验证该正常业务场景从输入准备到结果断言的完整链路；函数名 test_delegated_terminal_result_appends_revision_without_mutating_placeholder 精确标识本用例的具体条件。
def test_delegated_terminal_result_appends_revision_without_mutating_placeholder(
    seeded_run: tuple[str, str],
) -> None:
    run_id, session_id = seeded_run
    with database.SessionLocal() as db:
        db.add_all([
            ChatMessage(
                session_id=session_id,
                role="assistant",
                content="",
                sequence=1,
                extra={"tool_calls": [{
                    "id": "task-1",
                    "type": "function",
                    "function": {"name": "task", "arguments": "{}"},
                }]},
            ),
            ChatMessage(
                session_id=session_id,
                role="tool",
                tool_name="task",
                tool_call_id="task-1",
                content="awaiting child approval",
                sequence=2,
            ),
        ])
        db.commit()

    RunCoordinator._persist_outcome(run_id, RunOutcome(
        status="completed",
        output="parent complete",
        messages=[{"role": "assistant", "content": "parent complete"}],
        transcript_delta=[
            {"role": "tool", "name": "task", "tool_call_id": "task-1", "content": "child completed"},
            {"role": "assistant", "content": "parent complete"},
        ],
        events=[{"type": "run_completed"}],
        steps=2,
        tool_calls=1,
    ))

    with database.SessionLocal() as db:
        rows = list(db.scalars(
            select(ChatMessage)
            .where(ChatMessage.session_id == session_id)
            .order_by(ChatMessage.sequence)
        ))
        assert rows[1].role == "tool"
        assert rows[1].content == "awaiting child approval"
        assert rows[2].role == "assistant"
        assert "child completed" in rows[2].content
        assert rows[2].extra["supersedes_message_id"] == rows[1].id
        assert [row.sequence for row in rows] == [1, 2, 3, 4]


# 测试场景：验证非法、越界或不满足前置条件的操作会被明确拒绝，且不会产生错误状态；函数名 test_older_compaction_cannot_overwrite_newer_replacement 精确标识本用例的具体条件。
def test_older_compaction_cannot_overwrite_newer_replacement(
    seeded_run: tuple[str, str],
) -> None:
    run_id, session_id = seeded_run
    with database.SessionLocal() as db:
        db.add(ConversationCompaction(
            session_id=session_id,
            source_sequence=5,
            active_request="new",
            summary="new",
            continuation_messages=[{"role": "user", "content": "<compacted-context>new</compacted-context>"}],
        ))
        db.commit()
    RunCoordinator._persist_outcome(run_id, RunOutcome(
        status="completed",
        output="answer",
        messages=[{"role": "assistant", "content": "answer"}],
        events=[{
            "type": "context_compaction_finished",
            "reason": "threshold",
            "before_tokens": 100,
            "after_tokens": 50,
            "effective": True,
            "attempts": 1,
        }],
        steps=1,
        tool_calls=0,
        compaction_state={
            "schema": "claude_compaction_v1",
            "base_sequence": 0,
            "source_delta_count": 1,
            "source_sequence": 1,
            "active_request": "stale",
            "summary": "stale",
            "messages": [{"role": "user", "content": "<compacted-context>stale</compacted-context>"}],
        },
    ))
    with database.SessionLocal() as db:
        rows = list(db.scalars(select(ConversationCompaction).where(
            ConversationCompaction.session_id == session_id,
        )))
        assert len(rows) == 1 and rows[0].summary == "new"
        assert db.query(RunEvent).filter_by(run_id=run_id, event_type="context_compaction_conflict").count() == 1


# 测试场景：验证权限、审批或敏感数据边界在完整调用链路中保持有效；函数名 test_awaiting_outcome_creates_exact_pending_approval 精确标识本用例的具体条件。
def test_awaiting_outcome_creates_exact_pending_approval(seeded_run: tuple[str, str]) -> None:
    run_id, _session_id = seeded_run
    pending = {
        "id": "call-7",
        "tool_name": "write_file",
        "arguments": {"path": "answer.txt", "content": "42"},
        "reason": "写入文件",
    }
    outcome = RunOutcome(
        status="awaiting_approval",
        output=None,
        messages=[{"role": "assistant", "content": "", "tool_calls": []}],
        events=[{"type": "approval_requested", "request": pending}],
        steps=1,
        tool_calls=1,
        pending_approval=pending,
        guard_snapshot={"steps": 1, "calls": 1},
        active_elapsed_seconds=12.5,
        runtime_binding={"workspace_root": "C:/frozen", "model_id": "frozen-model"},
    )
    RunCoordinator._persist_outcome(run_id, outcome)

    with database.SessionLocal() as db:
        approval = db.scalar(select(Approval).where(Approval.run_id == run_id))
        assert approval is not None
        assert approval.tool_name == "write_file"
        assert approval.arguments == pending["arguments"]
        snapshot = db.scalar(select(RunEvent).where(RunEvent.run_id == run_id, RunEvent.event_type == "runtime_snapshot"))
        restored = RunCoordinator._outcome_from_snapshot(snapshot.payload)  # type: ignore[union-attr]
        assert restored.pending_approval == pending
        assert restored.guard_snapshot == {"steps": 1, "calls": 1}
        assert restored.active_elapsed_seconds == pytest.approx(12.5)
        assert restored.runtime_binding == {"workspace_root": "C:/frozen", "model_id": "frozen-model"}


# 测试场景：验证权限、审批或敏感数据边界在完整调用链路中保持有效；函数名 test_mismatched_pending_approval_is_superseded_not_reused 精确标识本用例的具体条件。
def test_mismatched_pending_approval_is_superseded_not_reused(seeded_run: tuple[str, str]) -> None:
    run_id, _session_id = seeded_run
    with database.SessionLocal() as db:
        stale = Approval(run_id=run_id, tool_name="write_file", arguments={"path": "stale.txt"})
        db.add(stale)
        db.commit()
        stale_id = stale.id

    pending = {
        "id": "call-new",
        "tool_name": "write_file",
        "arguments": {"path": "current.txt"},
    }
    RunCoordinator._persist_outcome(run_id, RunOutcome(
        status="awaiting_approval",
        output=None,
        messages=[],
        events=[],
        steps=1,
        tool_calls=1,
        pending_approval=pending,
    ))

    with database.SessionLocal() as db:
        stale = db.get(Approval, stale_id)
        assert stale is not None and stale.status == "superseded"
        pending_rows = list(db.scalars(select(Approval).where(
            Approval.run_id == run_id,
            Approval.status == "pending",
        )))
        assert len(pending_rows) == 1
        assert pending_rows[0].arguments == {"path": "current.txt"}


# 测试场景：验证该正常业务场景从输入准备到结果断言的完整链路；函数名 test_usage_is_upserted_as_absolute_run_aggregate 精确标识本用例的具体条件。
def test_usage_is_upserted_as_absolute_run_aggregate(seeded_run: tuple[str, str]) -> None:
    run_id, _session_id = seeded_run
    with database.SessionLocal() as db:
        connection = ModelConnection(
            name="Usage connection",
            provider="openai_compatible",
            base_url="https://example.test/v1",
            secret_ref="usage-secret",
            default_model="demo",
            status="connected",
        )
        db.add(connection)
        db.flush()
        connection_id = connection.id
        mutable_connection = ModelConnection(
            name="Mutable session connection",
            provider="openai_compatible",
            base_url="https://mutable.test/v1",
            secret_ref="mutable-secret",
            default_model="other",
            status="connected",
        )
        db.add(mutable_connection)
        db.flush()
        run = db.get(Run, run_id)
        assert run is not None
        session = db.get(Session, run.session_id)
        assert session is not None
        session.model_connection_id = mutable_connection.id
        db.commit()
    first = RunOutcome(
        status="awaiting_approval",
        output=None,
        messages=[],
        events=[],
        steps=1,
        tool_calls=1,
        pending_approval={"id": "write", "tool_name": "write_file", "arguments": {}},
        usage={
            "request_count": 1,
            "input_tokens": 100,
            "output_tokens": 20,
            "cache_creation_tokens": 5,
            "cache_read_tokens": 10,
            "total_tokens": 135,
            "cost_usd": 0.01,
            "model_connection_id": connection_id,
            "model_id": "demo",
            "provider": "openai_compatible",
        },
    )
    RunCoordinator._persist_outcome(run_id, first)
    resumed = RunOutcome(
        status="completed",
        output="done",
        messages=[],
        events=[],
        steps=2,
        tool_calls=1,
        usage={**first.usage, "request_count": 2, "output_tokens": 30, "total_tokens": 145, "cost_usd": 0.02},
    )
    RunCoordinator._persist_outcome(run_id, resumed)

    with database.SessionLocal() as db:
        records = list(db.scalars(select(UsageRecord).where(UsageRecord.run_id == run_id)))
        assert len(records) == 1
        record = records[0]
        assert record.request_count == 2
        assert record.input_tokens == 100
        assert record.output_tokens == 30
        assert record.total_tokens == 145
        assert record.cost_usd == pytest.approx(0.02)
        assert record.model_connection_id == connection_id


# 测试场景：验证该正常业务场景从输入准备到结果断言的完整链路；函数名 test_history_loader_never_compacts_or_drops_messages 精确标识本用例的具体条件。
def test_history_loader_never_compacts_or_drops_messages(seeded_run: tuple[str, str]) -> None:
    _run_id, session_id = seeded_run
    with database.SessionLocal() as db:
        session = db.get(Session, session_id)
        assert session is not None
        old_time = datetime.now(timezone.utc)
        old = ChatMessage(session_id=session_id, role="user", content="界" * 90_100, created_at=old_time)
        latest = ChatMessage(
            session_id=session_id,
            role="user",
            content="keep latest",
            created_at=old_time + timedelta(microseconds=1),
        )
        db.add_all([old, latest])
        db.flush()
        recent = _prepare_session_history(db, session)
        db.commit()

        assert recent == [
            {"role": "user", "content": "界" * 90_100},
            {"role": "user", "content": "keep latest"},
        ]
        assert session.context_tokens >= 180_000
        assert db.query(ConversationCompaction).filter_by(session_id=session_id).count() == 0


# 测试场景：验证该正常业务场景从输入准备到结果断言的完整链路；函数名 test_history_loader_orders_same_timestamp_messages_without_splitting 精确标识本用例的具体条件。
def test_history_loader_orders_same_timestamp_messages_without_splitting(
    seeded_run: tuple[str, str],
) -> None:
    _run_id, session_id = seeded_run
    shared_time = datetime.now(timezone.utc)
    with database.SessionLocal() as db:
        session = db.get(Session, session_id)
        assert session is not None
        db.add_all([
            ChatMessage(
                session_id=session_id,
                role="user",
                content="界" * 45_100,
                created_at=shared_time,
            ),
            ChatMessage(
                session_id=session_id,
                role="user",
                content="same timestamp",
                created_at=shared_time,
            ),
        ])
        db.flush()
        recent = _prepare_session_history(db, session)
        assert len(recent) == 2
        assert db.query(ConversationCompaction).filter_by(session_id=session_id).count() == 0


# 测试场景：验证该正常业务场景从输入准备到结果断言的完整链路；函数名 test_history_uses_sequence_when_tool_messages_share_a_timestamp 精确标识本用例的具体条件。
def test_history_uses_sequence_when_tool_messages_share_a_timestamp(
    seeded_run: tuple[str, str],
) -> None:
    _run_id, session_id = seeded_run
    shared_time = datetime.now(timezone.utc)
    with database.SessionLocal() as db:
        session = db.get(Session, session_id)
        assert session is not None
        db.add_all([
            ChatMessage(
                id="ffffffff-ffff-ffff-ffff-ffffffffffff",
                session_id=session_id,
                role="assistant",
                content="",
                extra={"tool_calls": [{"id": "call-1", "function": {"name": "read_file", "arguments": "{}"}}]},
                sequence=1,
                created_at=shared_time,
            ),
            ChatMessage(
                id="00000000-0000-0000-0000-000000000000",
                session_id=session_id,
                role="tool",
                content="result",
                tool_call_id="call-1",
                sequence=2,
                created_at=shared_time,
            ),
        ])
        db.flush()

        history = _prepare_session_history(db, session)

    assert [item["role"] for item in history] == ["assistant", "tool"]
    assert history[1]["tool_call_id"] == "call-1"


# 测试场景：验证该正常业务场景从输入准备到结果断言的完整链路；函数名 test_runtime_uses_enabled_fallback_connection_and_ignores_child_agent_settings 精确标识本用例的具体条件。
def test_runtime_uses_enabled_fallback_connection_and_ignores_child_agent_settings(
    seeded_run: tuple[str, str],
) -> None:
    run_id, session_id = seeded_run
    with database.SessionLocal() as db:
        connection = ModelConnection(
            name="Fallback",
            provider="openai_compatible",
            base_url="https://example.test/v1",
            secret_ref="fallback-secret",
            default_model="fallback-model",
            thinking_level="low",
            status="connected",
        )
        db.add(connection)
        session = db.get(Session, session_id)
        run = db.get(Run, run_id)
        assert session is not None and run is not None
        workspace = db.get(Workspace, run.workspace_id)
        assert workspace is not None
        workspace.validation_runtime = {
            "kind": "docker",
            "image": "swebench/frozen-runtime:latest",
        }
        session.thinking_level = "auto"
        agent = db.get(Agent, run.agent_id)
        assert agent is not None
        agent.description = "UI metadata, not a model prompt"
        agent.thinking_level = "high"
        db.add(RunEvent(
            run_id=run_id,
            event_type="runtime_snapshot",
            payload={"runtime_binding": {"todo_state": [
                {"id": "old", "content": "previous task", "status": "in_progress"},
            ]}},
        ))
        db.commit()
        connection_id = connection.id

    _runtime, context = RunCoordinator._resolve_runtime(run_id)
    assert context["model_connection_id"] == connection_id
    assert context["provider"].model_id == "fallback-model"
    # Conversations run through the fixed PGAgent coordinator. A user-created
    # child profile can no longer override its model/thinking configuration;
    # it is only surfaced as safe capability metadata for the task tool.
    assert context["provider"].thinking_level == "low"
    assert context["todo_state"] == []
    assert context["runtime_binding"]["validation_runtime"] == {
        "kind": "docker",
        "image": "swebench/frozen-runtime:latest",
    }
    assert context["runtime_binding"]["validation_runtime"] == (
        _runtime.tool_registry.runtime_state()["validation_runtime"]
    )
    assert agent.id in context["agent_instructions"]
    assert "Builder" in context["agent_instructions"]

    binding = dict(context["runtime_binding"])
    with database.SessionLocal() as db:
        connection = db.get(ModelConnection, connection_id)
        run = db.get(Run, run_id)
        assert connection is not None and run is not None
        workspace = db.get(Workspace, run.workspace_id)
        assert workspace is not None
        connection.default_model = "changed-model"
        workspace.root_path = "C:/changed-workspace"
        workspace.validation_runtime = {"kind": "local"}
        db.commit()

    _runtime, resumed_context = RunCoordinator._resolve_runtime(
        run_id,
        runtime_binding=binding,
    )
    assert resumed_context["workspace_root"] == binding["workspace_root"]
    assert resumed_context["provider"].provider == "openai_compatible"
    assert resumed_context["provider"].base_url == "https://example.test/v1"
    assert resumed_context["provider"].model_id == "fallback-model"
    assert resumed_context["provider"].model_connection_id == connection_id
    assert resumed_context["runtime_binding"]["validation_runtime"] == {
        "kind": "docker",
        "image": "swebench/frozen-runtime:latest",
    }

    with database.SessionLocal() as db:
        connection = db.get(ModelConnection, connection_id)
        assert connection is not None
        connection.provider = "openrouter"
        db.commit()

    with pytest.raises(RuntimeError, match="模型连接配置在审批等待期间已改变"):
        RunCoordinator._resolve_runtime(run_id, runtime_binding=binding)


# 测试场景：验证该正常业务场景从输入准备到结果断言的完整链路；函数名 test_runtime_freezes_global_and_chat_memory_preferences 精确标识本用例的具体条件。
def test_runtime_freezes_global_and_chat_memory_preferences(
    seeded_run: tuple[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id, session_id = seeded_run
    with database.SessionLocal() as db:
        db.add(ModelConnection(
            name="Memory preference connection",
            provider="openai_compatible",
            base_url="https://example.test/v1",
            secret_ref="memory-preference-secret",
            default_model="memory-preference-model",
            status="connected",
        ))
        settings_row = db.get(MemorySettings, "global")
        session = db.get(Session, session_id)
        assert settings_row is not None and session is not None
        settings_row.enabled = True
        session.use_memories = True
        session.mcp_server_names = ["filesystem", "github"]
        db.commit()

    calls: list[tuple[str | None, str | None]] = []

    # 局部测试函数：fake_load_memory_index 模拟该步骤的返回结果或异常。
    def fake_load_memory_index(*, workspace_id, session_id=None):  # type: ignore[no-untyped-def]
        calls.append((workspace_id, session_id))
        return "MEMORY INDEX"

    monkeypatch.setattr(lifecycle_service, "load_memory_index", fake_load_memory_index)
    runtime, initial = RunCoordinator._resolve_runtime(run_id)
    binding = dict(initial["runtime_binding"])
    assert calls
    assert initial["memory_index"] == "MEMORY INDEX"
    assert binding["memories_enabled"] is True
    assert binding["use_memories"] is True
    assert binding["mcp_server_names"] == ["filesystem", "github"]
    assert "MemorySearch" in runtime.tool_registry.enabled_tool_names

    with database.SessionLocal() as db:
        settings_row = db.get(MemorySettings, "global")
        session = db.get(Session, session_id)
        assert settings_row is not None and session is not None
        settings_row.enabled = False
        session.use_memories = False
        session.mcp_server_names = []
        db.commit()

    resumed_runtime, resumed = RunCoordinator._resolve_runtime(run_id, runtime_binding=binding)
    assert resumed["memory_index"] == "MEMORY INDEX"
    assert resumed["runtime_binding"]["mcp_server_names"] == ["filesystem", "github"]
    assert "MemorySearch" in resumed_runtime.tool_registry.enabled_tool_names
    assert len(calls) == 1

    with database.SessionLocal() as db:
        settings_row = db.get(MemorySettings, "global")
        session = db.get(Session, session_id)
        assert settings_row is not None and session is not None
        settings_row.enabled = True
        session.use_memories = False
        db.commit()

    chat_disabled_runtime, chat_disabled = RunCoordinator._resolve_runtime(run_id)
    assert chat_disabled["memory_index"] == ""
    assert chat_disabled["runtime_binding"]["memories_enabled"] is True
    assert chat_disabled["runtime_binding"]["use_memories"] is False
    assert "MemorySearch" not in chat_disabled_runtime.tool_registry.enabled_tool_names
    assert len(calls) == 1

    with database.SessionLocal() as db:
        settings_row = db.get(MemorySettings, "global")
        session = db.get(Session, session_id)
        assert settings_row is not None and session is not None
        settings_row.enabled = False
        session.use_memories = True
        db.commit()

    globally_disabled_runtime, globally_disabled = RunCoordinator._resolve_runtime(run_id)
    assert globally_disabled["memory_index"] == ""
    assert globally_disabled["runtime_binding"]["memories_enabled"] is False
    assert globally_disabled["runtime_binding"]["use_memories"] is True
    assert "MemorySearch" not in globally_disabled_runtime.tool_registry.enabled_tool_names
    assert len(calls) == 1


@pytest.mark.parametrize("global_enabled, expected_jobs", [(False, 0), (True, 1)])
# 测试场景：验证非法、越界或不满足前置条件的操作会被明确拒绝，且不会产生错误状态；函数名 test_global_memory_preference_controls_extraction_but_chat_preference_does_not 精确标识本用例的具体条件。
def test_global_memory_preference_controls_extraction_but_chat_preference_does_not(
    accepted_run: tuple[str, str], global_enabled: bool, expected_jobs: int
) -> None:
    run_id, _session_id = accepted_run
    with database.SessionLocal() as db:
        settings_row = db.get(MemorySettings, "global")
        assert settings_row is not None
        settings_row.enabled = global_enabled
        db.commit()

    RunCoordinator._persist_outcome(run_id, RunOutcome(
        status="completed",
        output="accepted response",
        messages=[],
        events=[],
        steps=1,
        tool_calls=0,
        mode="auto",
        runtime_binding={
            "memories_enabled": global_enabled,
            "use_memories": False,
        },
    ))

    with database.SessionLocal() as db:
        assert db.query(MemoryJob).count() == expected_jobs


# 测试场景：验证状态能够可靠持久化、重放或在重启后恢复，并保持记录之间的关联；函数名 test_durable_todo_state_wins_over_conflicting_frozen_binding 精确标识本用例的具体条件。
def test_durable_todo_state_wins_over_conflicting_frozen_binding(
    seeded_run: tuple[str, str],
) -> None:
    run_id, _session_id = seeded_run
    with database.SessionLocal() as db:
        db.add(ModelConnection(
            name="Durable state connection",
            provider="openai_compatible",
            base_url="https://example.test/v1",
            secret_ref="durable-state-secret",
            default_model="durable-state-model",
            status="connected",
        ))
        db.commit()
    sync_todos_for_run(run_id, [
        {"id": "canonical", "content": "Canonical step", "status": "in_progress"},
    ])
    _runtime, initial = RunCoordinator._resolve_runtime(run_id)
    binding = dict(initial["runtime_binding"])
    binding["todo_state"] = [
        {"id": "stale", "content": "Stale frozen step", "status": "pending"},
    ]

    _runtime, resumed = RunCoordinator._resolve_runtime(run_id, runtime_binding=binding)

    assert [item["id"] for item in resumed["todo_state"]] == ["canonical"]


# 测试场景：验证该正常业务场景从输入准备到结果断言的完整链路；函数名 test_runtime_freezes_global_and_project_agents_instructions 精确标识本用例的具体条件。
def test_runtime_freezes_global_and_project_agents_instructions(
    seeded_run: tuple[str, str],
) -> None:
    run_id, _session_id = seeded_run
    with database.SessionLocal() as db:
        run = db.get(Run, run_id)
        assert run is not None
        workspace = db.get(Workspace, run.workspace_id)
        assert workspace is not None
        db.add(ModelConnection(
            name="Instruction snapshot connection",
            provider="openai_compatible",
            base_url="https://example.test/v1",
            secret_ref="instruction-test-secret",
            default_model="instruction-test-model",
            status="connected",
        ))
        db.commit()
        workspace_root = Path(workspace.root_path)
    workspace_root.mkdir(parents=True)
    instruction_service.write_personal_instructions("personal version one")
    project_agents = workspace_root / "AGENTS.md"
    project_agents.write_text("project version one", encoding="utf-8")

    _runtime, context = RunCoordinator._resolve_runtime(run_id)
    rules = context["workspace_rules"]
    binding = dict(context["runtime_binding"])

    assert rules.index("personal version one") < rules.index("project version one")
    assert binding["agents_instructions"] in rules
    assert binding["agents_instruction_sources"] == [
        str(instruction_service.personal_agents_path().resolve()),
        str(project_agents.resolve()),
    ]

    instruction_service.write_personal_instructions("personal version two")
    project_agents.write_text("project version two", encoding="utf-8")
    _runtime, resumed = RunCoordinator._resolve_runtime(run_id, runtime_binding=binding)

    assert "personal version one" in resumed["workspace_rules"]
    assert "project version one" in resumed["workspace_rules"]
    assert "version two" not in resumed["workspace_rules"]


# 测试场景：验证该正常业务场景从输入准备到结果断言的完整链路；函数名 test_resolved_runtime_can_read_its_session_artifacts 精确标识本用例的具体条件。
def test_resolved_runtime_can_read_its_session_artifacts(
    seeded_run: tuple[str, str],
) -> None:
    run_id, _session_id = seeded_run
    with database.SessionLocal() as db:
        db.add(ModelConnection(
            name="Artifact reader connection",
            provider="openai_compatible",
            base_url="https://example.test/v1",
            secret_ref="artifact-reader-secret",
            default_model="artifact-reader-model",
            status="connected",
        ))
        db.commit()

    runtime, _context = RunCoordinator._resolve_runtime(run_id)
    ref = runtime.context_assembler.artifact_store.put("full persisted output")
    result = asyncio.run(runtime.tool_registry.execute_async(
        "read_artifact",
        {"artifact_id": ref.artifact_id},
    ))

    assert "read_artifact" in runtime.tool_registry.enabled_tool_names
    assert result.ok and result.content == "full persisted output"


# 测试场景：验证非法、越界或不满足前置条件的操作会被明确拒绝，且不会产生错误状态；函数名 test_fallback_connection_does_not_reuse_model_from_disabled_connection 精确标识本用例的具体条件。
def test_fallback_connection_does_not_reuse_model_from_disabled_connection(
    seeded_run: tuple[str, str],
) -> None:
    run_id, session_id = seeded_run
    with database.SessionLocal() as db:
        disabled = ModelConnection(
            name="Disabled OpenRouter",
            provider="openrouter",
            base_url="https://openrouter.ai/api/v1",
            secret_ref="disabled-secret",
            default_model="anthropic/disabled",
            enabled=False,
        )
        fallback = ModelConnection(
            name="Enabled fallback",
            provider="openai_compatible",
            base_url="https://example.test/v1",
            secret_ref="enabled-secret",
            default_model="fallback-model",
            status="connected",
        )
        db.add_all([disabled, fallback])
        db.flush()
        session = db.get(Session, session_id)
        assert session is not None
        session.model_connection_id = disabled.id
        session.model_id = "anthropic/session-model"
        db.commit()
        fallback_id = fallback.id

    _runtime, context = RunCoordinator._resolve_runtime(run_id)
    assert context["model_connection_id"] == fallback_id
    assert context["provider"].provider == "openai_compatible"
    assert context["provider"].model_id == "fallback-model"
