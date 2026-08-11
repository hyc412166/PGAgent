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
    # child profile can no longer override its model/thinking configuration.
    assert context["provider"].thinking_level == "low"
    assert context["agent_instructions"] == ""

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
