from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import select

from app import database
from app.database import (
    Agent,
    Approval,
    Artifact,
    ContextCheckpoint,
    ContextEpoch,
    CompactionAttempt,
    Base,
    ChatMessage,
    ModelConnection,
    Run,
    RunEvent,
    Session,
    UsageRecord,
    Workspace,
)
from app.runtime import RunOutcome
from app.services.run_service import RunCoordinator, _prepare_session_history
from app.services.model_gateway import ModelConfigurationError
from app.runtime import AgentRuntime
from app.tools import create_default_registry


@pytest.mark.asyncio
async def test_resume_is_queued_until_the_original_run_task_finishes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    coordinator = RunCoordinator()
    execute_started = asyncio.Event()
    release_execute = asyncio.Event()
    resume_started = asyncio.Event()

    async def fake_execute(_run_id: str) -> None:
        execute_started.set()
        await release_execute.wait()

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
def seeded_run(tmp_path: Path) -> tuple[str, str]:
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


def test_completed_outcome_persists_snapshot_and_assistant_message(seeded_run: tuple[str, str]) -> None:
    run_id, session_id = seeded_run
    outcome = RunOutcome(
        status="completed",
        output="任务完成",
        messages=[{"role": "assistant", "content": "任务完成"}],
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
        message = db.scalar(select(ChatMessage).where(ChatMessage.session_id == session_id))
        assert message is not None and message.content == "任务完成"
        snapshot = db.scalar(select(RunEvent).where(RunEvent.run_id == run_id, RunEvent.event_type == "runtime_snapshot"))
        assert snapshot is not None and snapshot.payload["guard_snapshot"]["calls"] == 1


def test_main_agent_completion_gate_fails_closed_without_task_anchor(tmp_path: Path) -> None:
    runtime = AgentRuntime(
        model_call=lambda **_kwargs: None,
        tool_registry=create_default_registry(str(tmp_path), allowed_tool_names=[]),
    )
    with pytest.raises(ModelConfigurationError, match="任务锚点"):
        RunCoordinator._install_completion_verifier(runtime, {"runtime_binding": {}})


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
        acceptance_report={"stage": "complete", "semantic": {"passed": False}},
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
        assert snapshot.payload["acceptance_report"]["semantic"]["passed"] is False
        assert snapshot.payload["completion_verification_attempts"] == 3
        assert snapshot.payload["messages"] == []
        assert "unsupported candidate" not in str(snapshot.payload)


def test_context_epoch_is_promoted_and_transcript_tail_is_loaded(
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
        context_snapshot={
            "epoch_id": "runtime-epoch",
            "source_sequence": 1,
            "sequence": 1,
            "summary": {"objective": "old request", "completed_work": ["answered"]},
            "task_state": {"status": "done"},
            "retained_messages": [{"role": "user", "content": "old request"}],
            "artifact_refs": [],
            "version": 1,
        },
    )
    RunCoordinator._persist_outcome(run_id, outcome)

    with database.SessionLocal() as db:
        session = db.get(Session, session_id)
        assert session is not None
        assert db.query(ContextEpoch).filter_by(session_id=session_id, status="active").count() == 1
        assert db.query(ContextCheckpoint).filter_by(session_id=session_id, status="completed").count() == 1
        assert db.query(CompactionAttempt).filter_by(session_id=session_id).count() == 1
        assert _prepare_session_history(db, session) == [{"role": "assistant", "content": "new answer"}]


def test_context_snapshot_persists_artifact_metadata(seeded_run: tuple[str, str], tmp_path: Path) -> None:
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


def test_snapshot_is_rejected_when_transcript_changed_during_compaction(
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
        context_snapshot={
            "epoch_id": "stale-runtime",
            "source_sequence": 1,
            "sequence": 1,
            "version": 1,
            "summary": {"objective": "stale"},
        },
    ))

    with database.SessionLocal() as db:
        assert db.query(ContextEpoch).filter_by(session_id=session_id, status="active").count() == 0
        assert db.query(CompactionAttempt).filter_by(session_id=session_id, status="conflict").count() == 1


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


def test_older_context_snapshot_cannot_overwrite_newer_epoch(
    seeded_run: tuple[str, str],
) -> None:
    run_id, session_id = seeded_run
    with database.SessionLocal() as db:
        db.add(ContextEpoch(
            session_id=session_id,
            epoch_number=1,
            status="active",
            end_sequence=5,
            version=3,
            summary_json={"objective": "new"},
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
        context_snapshot={
            "epoch_id": "stale",
            "sequence": 2,
            "version": 2,
            "summary": {"objective": "stale"},
        },
    ))
    with database.SessionLocal() as db:
        active = db.scalar(select(ContextEpoch).where(
            ContextEpoch.session_id == session_id,
            ContextEpoch.status == "active",
        ))
        assert active is not None and active.summary_json["objective"] == "new"
        assert db.query(CompactionAttempt).filter_by(session_id=session_id, status="conflict").count() == 1


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


def test_history_compacts_at_90k_and_updates_session_metadata(seeded_run: tuple[str, str]) -> None:
    _run_id, session_id = seeded_run
    with database.SessionLocal() as db:
        session = db.get(Session, session_id)
        assert session is not None
        old_time = datetime.now(timezone.utc)
        old = ChatMessage(session_id=session_id, role="user", content="界" * 45_100, created_at=old_time)
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

        assert recent == [{"role": "user", "content": "keep latest"}]
        assert session.context_summary
        assert session.context_tokens < 90_000
        assert session.last_compacted_at == old.created_at


def test_compaction_never_splits_messages_with_the_same_timestamp(
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
        assert session.last_compacted_at is None


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
        session.thinking_level = "auto"
        agent = db.get(Agent, run.agent_id)
        assert agent is not None
        agent.description = "UI metadata, not a model prompt"
        agent.thinking_level = "high"
        db.commit()
        connection_id = connection.id

    _runtime, context = RunCoordinator._resolve_runtime(run_id)
    assert context["model_connection_id"] == connection_id
    assert context["provider"].model_id == "fallback-model"
    # Conversations run through the fixed PGAgent coordinator. A user-created
    # child profile can no longer override its model/thinking configuration;
    # it is only surfaced as safe capability metadata for the task tool.
    assert context["provider"].thinking_level == "low"
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

    with database.SessionLocal() as db:
        connection = db.get(ModelConnection, connection_id)
        assert connection is not None
        connection.provider = "openrouter"
        db.commit()

    with pytest.raises(RuntimeError, match="模型连接配置在审批等待期间已改变"):
        RunCoordinator._resolve_runtime(run_id, runtime_binding=binding)


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
