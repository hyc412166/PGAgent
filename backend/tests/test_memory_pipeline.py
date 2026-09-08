"""验证长期记忆提取流水线的候选生成、去重、合并、删除和运行结束触发。

测试通过 fixture 或辅助函数准备隔离环境，再调用真实服务、路由或运行时，并检查返回值、持久化状态与可观察副作用。
变量约定：tmp_path/monkeypatch 提供隔离环境，client/store/runtime 驱动被测链路，各类 *_id 串联持久化实体，payload 表示输入，response/result 表示实际输出，expected 表示期望值。
"""

from __future__ import annotations

import json
import asyncio
from pathlib import Path

import pytest
from sqlalchemy import select

from src.memory.pipeline import activate_deferred_memory_jobs, memory_pipeline
from src.memory.extraction import (
    bounded_rollout_messages,
    parse_extraction,
    redact_secrets,
    rollout_messages,
)
from src.memory.protocol import memory_system_message, split_memory_citation
from src.memory.repository import load_memory_index, record_memory_citations
from src.memory.service import export_memory_markdown, store_memory
from src.persistence import database
from src.persistence.database import (
    Base,
    ChatMessage,
    Memory,
    MemoryCitation,
    MemoryJob,
    MemoryRollout,
    MemorySettings,
    ModelConnection,
    Run,
    Session,
    Workspace,
)
from src.runs.service import RunCoordinator


@pytest.fixture()
# 测试夹具：pipeline_db 创建本组用例共享的隔离资源，并在测试结束后恢复数据库、配置或进程状态。
def pipeline_db(tmp_path: Path):
    # 临时数据库串联运行、消息与记忆候选；夹具退出时清表，避免不同提取场景相互污染。
    database.configure_database(f"sqlite:///{(tmp_path / 'memory-pipeline.db').as_posix()}")
    database.init_db()
    with database.SessionLocal() as db:
        workspace = Workspace(name="Project", root_path=str(tmp_path / "project"))
        db.add(workspace); db.flush()
        session = Session(title="Conversation", workspace_id=workspace.id)
        connection = ModelConnection(
            name="Memory model",
            provider="openai_compatible",
            base_url="http://example.invalid",
            secret_ref="secret-ref",
            default_model="demo-model",
            enabled=True,
        )
        db.add_all([session, connection]); db.flush()
        ids = workspace.id, session.id, connection.id
        db.commit()
    yield tmp_path, *ids
    Base.metadata.drop_all(bind=database.engine)


# 辅助函数：_binding 封装本组测试重复使用的输入准备、状态查询或测试替身行为。
def _binding(connection: ModelConnection) -> dict[str, str]:
    return {
        "model_connection_id": connection.id,
        "provider": connection.provider,
        "base_url": connection.base_url,
        "secret_ref": connection.secret_ref,
        "model_id": str(connection.default_model),
    }


# 辅助函数：_phase1_response 封装本组测试重复使用的输入准备、状态查询或测试替身行为。
def _phase1_response(content: str) -> dict:
    return {
        "choices": [{"message": {"content": json.dumps({
            "worth_remembering": True,
            "rollout_slug": "worker-fence",
            "rollout_summary": {"outcome": "success"},
            "raw_memories": [{
                "kind": "procedure", "task": "build", "task_group": "build",
                "cwd": "", "keywords": ["build"], "content": content,
                "evidence": [], "confidence": "verified",
            }],
        })}}],
        "usage": {"total_tokens": 1},
    }


# 测试场景：验证该正常业务场景从输入准备到结果断言的完整链路；函数名 test_memory_citation_protocol_is_hidden_and_machine_readable 精确标识本用例的具体条件。
def test_memory_citation_protocol_is_hidden_and_machine_readable() -> None:
    visible, citation = split_memory_citation(
        'Use pytest.\n<pgagent-memory-citation>{"memory_ids":["m1"],'
        '"rollout_ids":["r1"],"note":"test command"}</pgagent-memory-citation>'
    )

    assert visible == "Use pytest."
    assert citation == {
        "memory_ids": ["m1"], "rollout_ids": ["r1"], "skill_ids": [], "note": "test command"
    }
    message = memory_system_message("- backend testing: pytest")
    assert message["role"] == "system"
    assert "MemorySearch" in message["content"]
    assert "backend testing" in message["content"]


# 测试场景：验证权限、审批或敏感数据边界在完整调用链路中保持有效；函数名 test_phase1_redacts_quoted_json_credentials_from_inputs_and_outputs 精确标识本用例的具体条件。
def test_phase1_redacts_quoted_json_credentials_from_inputs_and_outputs() -> None:
    raw = json.dumps({
        "api_key": "sk-abcdefghijklmnop",
        "api_token": "api-token-value",
        "refresh_token": "refresh-token-value",
        "password": "hunter2",
        "Authorization": "Bearer abcdefghijklmnop",
    })

    redacted = redact_secrets(raw)
    parsed = parse_extraction(json.dumps({
        "worth_remembering": True,
        "rollout_slug": "api_token=slug-secret-value",
        "rollout_summary": {"outcome": "success", "verification": [raw]},
        "raw_memories": [{
            "kind": "project_knowledge",
            "task": "configure provider",
            "task_group": "provider-config",
            "cwd": "C:/project",
            "keywords": ["provider"],
            "content": f"The durable provider setup was {raw}",
            "evidence": [{"type": "tool", "sequence": 1, "summary": raw}],
            "confidence": "verified",
        }],
    }))

    assert parsed is not None
    for secret in (
        "sk-abcdefghijklmnop",
        "api-token-value",
        "refresh-token-value",
        "hunter2",
        "Bearer abcdefghijklmnop",
        "slug-secret-value",
    ):
        assert secret not in redacted
        assert secret not in json.dumps(parsed, ensure_ascii=False)
    assert redacted.count("[REDACTED]") == 5


# 测试场景：验证该正常业务场景从输入准备到结果断言的完整链路；函数名 test_rollout_filter_keeps_public_request_terminal_reply_and_tool_evidence 精确标识本用例的具体条件。
def test_rollout_filter_keeps_public_request_terminal_reply_and_tool_evidence(pipeline_db) -> None:
    _root, _workspace_id, session_id, _connection_id = pipeline_db
    with database.SessionLocal() as db:
        rows = [
            ChatMessage(session_id=session_id, role="user", content="request", message_kind="user_request", sequence=1),
            ChatMessage(session_id=session_id, role="assistant", content="calling", message_kind="transcript", sequence=2),
            ChatMessage(session_id=session_id, role="tool", content="verified", message_kind="transcript", sequence=3),
            ChatMessage(session_id=session_id, role="assistant", content="done", message_kind="terminal", sequence=4),
        ]
        db.add_all(rows); db.flush()
        filtered = rollout_messages(rows)

    assert [item["content"] for item in filtered] == ["request", "calling", "verified", "done"]


# 测试场景：验证权限、审批或敏感数据边界在完整调用链路中保持有效；函数名 test_rollout_filter_bounds_messages_and_redacts_tool_call_secrets 精确标识本用例的具体条件。
def test_rollout_filter_bounds_messages_and_redacts_tool_call_secrets(pipeline_db) -> None:
    _root, _workspace_id, session_id, _connection_id = pipeline_db
    with database.SessionLocal() as db:
        row = ChatMessage(
            session_id=session_id,
            role="assistant",
            content="working",
            sequence=1,
            extra={"tool_calls": [{
                "id": "t1",
                "function": {"name": "bash", "arguments": "password=super-secret " + "x" * 500},
            }]},
        )
        db.add(row); db.flush()
        filtered = bounded_rollout_messages([row], max_chars=300, max_message_chars=20)

    serialized = json.dumps(filtered, ensure_ascii=False)
    assert "super-secret" not in serialized
    assert "[REDACTED]" in serialized


# 测试场景：验证该正常业务场景从输入准备到结果断言的完整链路；函数名 test_deferred_extraction_activates_for_a_later_root_turn 精确标识本用例的具体条件。
def test_deferred_extraction_activates_for_a_later_root_turn(pipeline_db) -> None:
    _root, workspace_id, session_id, _connection_id = pipeline_db
    with database.SessionLocal() as db:
        previous_run = Run(session_id=session_id, workspace_id=workspace_id, status="completed")
        current_run = Run(session_id=session_id, workspace_id=workspace_id, status="completed")
        db.add_all([previous_run, current_run]); db.flush()
        previous = MemoryJob(
            run_id=previous_run.id,
            workspace_id=workspace_id,
            session_id=session_id,
            kind="extract",
            status="deferred",
        )
        current = MemoryJob(
            run_id=current_run.id,
            workspace_id=workspace_id,
            session_id=session_id,
            kind="extract",
            status="deferred",
        )
        db.add_all([previous, current]); db.commit()
        previous_id = previous.id
        current_run_id = current_run.id

    activated = activate_deferred_memory_jobs(
        session_id=session_id,
        exclude_run_id=current_run_id,
        idle_seconds=0,
    )

    assert activated == [previous_id]
    with database.SessionLocal() as db:
        assert db.get(MemoryJob, previous_id).status == "pending"


# 测试场景：验证该正常业务场景从输入准备到结果断言的完整链路；函数名 test_global_disable_pauses_deferred_and_pending_memory_jobs 精确标识本用例的具体条件。
def test_global_disable_pauses_deferred_and_pending_memory_jobs(pipeline_db) -> None:
    _tmp_path, workspace_id, session_id, _connection_id = pipeline_db
    with database.SessionLocal() as db:
        settings_row = db.get(MemorySettings, "global")
        assert settings_row is not None
        settings_row.enabled = False
        run = Run(session_id=session_id, workspace_id=workspace_id, status="completed")
        db.add(run); db.flush()
        job = MemoryJob(
            run_id=run.id,
            session_id=session_id,
            workspace_id=workspace_id,
            kind="extract",
            status="deferred",
        )
        db.add(job); db.commit()
        job_id = job.id

    assert activate_deferred_memory_jobs(idle_seconds=0) == []
    assert RunCoordinator.pending_memory_job_ids() == []
    with database.SessionLocal() as db:
        assert db.get(MemoryJob, job_id).status == "deferred"


# 测试场景：验证该正常业务场景从输入准备到结果断言的完整链路；函数名 test_memory_summary_indexes_every_active_memory 精确标识本用例的具体条件。
def test_memory_summary_indexes_every_active_memory(pipeline_db) -> None:
    root, workspace_id, session_id, _connection_id = pipeline_db
    with database.SessionLocal() as db:
        for index in range(25):
            store_memory(
                db,
                name=f"Memory {index}",
                content=f"Durable content {index}",
                description=f"Routing description {index}",
                tags=[f"keyword-{index}"],
                workspace_id=workspace_id,
                session_id=session_id,
            )
        db.commit()

    export_memory_markdown(output_dir=root / "projection")
    summary = (root / "projection" / "memory_summary.md").read_text(encoding="utf-8")

    assert summary.count("## Memory ") == 25
    assert "Memory 0" in summary and "Memory 24" in summary
    assert "Search hint" in summary


# 测试场景：验证接口或资源生命周期操作会返回正确结果并同步持久化状态；函数名 test_run_memory_index_contains_all_visible_routes_but_not_other_workspace 精确标识本用例的具体条件。
def test_run_memory_index_contains_all_visible_routes_but_not_other_workspace(pipeline_db) -> None:
    _root, workspace_id, session_id, _connection_id = pipeline_db
    with database.SessionLocal() as db:
        other = Workspace(name="Other", root_path="C:/other")
        db.add(other); db.flush()
        store_memory(db, name="Global preference", content="Explain failures first.", scope="global")
        store_memory(db, name="Current project", content="Use uv.", scope="workspace", workspace_id=workspace_id)
        store_memory(db, name="Other project", content="Use npm.", scope="workspace", workspace_id=other.id)
        db.commit()

    index = load_memory_index(workspace_id=workspace_id, session_id=session_id)

    assert "Global preference" in index and "Current project" in index
    assert "Other project" not in index


@pytest.mark.asyncio
# 测试场景：验证该正常业务场景从输入准备到结果断言的完整链路；函数名 test_extract_job_claim_allows_only_one_model_call 精确标识本用例的具体条件。
async def test_extract_job_claim_allows_only_one_model_call(pipeline_db, monkeypatch: pytest.MonkeyPatch) -> None:
    _root, workspace_id, session_id, connection_id = pipeline_db
    with database.SessionLocal() as db:
        connection = db.get(ModelConnection, connection_id)
        assert connection is not None
        job = MemoryJob(
            workspace_id=workspace_id, session_id=session_id, kind="extract",
            payload={"runtime_binding": _binding(connection)},
        )
        db.add(job); db.commit(); job_id = job.id
    calls = 0

    # 局部测试函数：fake_call 模拟该步骤的返回结果或异常。
    async def fake_call(**_kwargs):
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.02)
        return _phase1_response("Only one worker may commit this durable value.")

    monkeypatch.setattr("src.memory.pipeline.build_model_call", lambda _config: fake_call)
    await asyncio.gather(memory_pipeline.process(job_id), memory_pipeline.process(job_id))

    assert calls == 1
    with database.SessionLocal() as db:
        assert db.get(MemoryJob, job_id).status == "completed"
        assert len(list(db.scalars(select(MemoryRollout)))) == 1


@pytest.mark.asyncio
# 测试场景：验证该正常业务场景从输入准备到结果断言的完整链路；函数名 test_malformed_extraction_is_retryable_instead_of_a_false_noop 精确标识本用例的具体条件。
async def test_malformed_extraction_is_retryable_instead_of_a_false_noop(pipeline_db, monkeypatch: pytest.MonkeyPatch) -> None:
    _root, workspace_id, session_id, connection_id = pipeline_db
    with database.SessionLocal() as db:
        connection = db.get(ModelConnection, connection_id)
        assert connection is not None
        job = MemoryJob(
            workspace_id=workspace_id, session_id=session_id, kind="extract",
            payload={"runtime_binding": _binding(connection)},
        )
        db.add(job); db.commit(); job_id = job.id

    # 局部测试函数：fake_call 模拟该步骤的返回结果或异常。
    async def fake_call(**_kwargs):
        return {"choices": [{"message": {"content": "{truncated"}}]}

    monkeypatch.setattr("src.memory.pipeline.build_model_call", lambda _config: fake_call)
    await memory_pipeline.process(job_id)

    with database.SessionLocal() as db:
        job = db.get(MemoryJob, job_id)
        assert job is not None and job.status == "pending" and job.attempts == 1
        assert db.scalar(select(MemoryRollout)) is None


@pytest.mark.asyncio
# 测试场景：验证该正常业务场景从输入准备到结果断言的完整链路；函数名 test_legacy_extraction_payload_is_used_when_no_chat_rows_exist 精确标识本用例的具体条件。
async def test_legacy_extraction_payload_is_used_when_no_chat_rows_exist(
    pipeline_db, monkeypatch: pytest.MonkeyPatch
) -> None:
    _root, workspace_id, session_id, connection_id = pipeline_db
    with database.SessionLocal() as db:
        connection = db.get(ModelConnection, connection_id)
        assert connection is not None
        job = MemoryJob(
            workspace_id=workspace_id,
            session_id=session_id,
            kind="extract",
            payload={
                "user_request": "Always run the legacy verification command.",
                "assistant_response": "The legacy command passed.",
                "tool_observations": ["legacy-check: passed"],
                "runtime_binding": _binding(connection),
            },
        )
        db.add(job); db.commit(); job_id = job.id
    observed: list[dict[str, str]] = []

    # 局部测试函数：fake_call 模拟该步骤的返回结果或异常。
    async def fake_call(**kwargs):
        observed.extend(kwargs["messages"])
        return {"choices": [{"message": {"content": json.dumps({
            "worth_remembering": False,
            "rollout_slug": "",
            "rollout_summary": {},
            "raw_memories": [],
        })}}]}

    monkeypatch.setattr("src.memory.pipeline.build_model_call", lambda _config: fake_call)
    await memory_pipeline.process(job_id)

    rendered = "\n".join(message["content"] for message in observed)
    assert "Always run the legacy verification command." in rendered
    assert "The legacy command passed." in rendered
    assert "legacy-check: passed" in rendered


@pytest.mark.asyncio
# 测试场景：验证该正常业务场景从输入准备到结果断言的完整链路；函数名 test_multiple_legacy_payloads_in_one_session_are_not_deduplicated 精确标识本用例的具体条件。
async def test_multiple_legacy_payloads_in_one_session_are_not_deduplicated(
    pipeline_db, monkeypatch: pytest.MonkeyPatch
) -> None:
    _root, workspace_id, session_id, connection_id = pipeline_db
    with database.SessionLocal() as db:
        connection = db.get(ModelConnection, connection_id)
        assert connection is not None
        jobs = [MemoryJob(
            workspace_id=workspace_id,
            session_id=session_id,
            kind="extract",
            payload={
                "user_request": request,
                "assistant_response": "accepted",
                "runtime_binding": _binding(connection),
            },
        ) for request in ("remember alpha", "remember beta")]
        db.add_all(jobs); db.commit(); job_ids = [job.id for job in jobs]

    # 局部测试函数：fake_call 模拟该步骤的返回结果或异常。
    async def fake_call(**kwargs):
        rendered = "\n".join(message["content"] for message in kwargs["messages"])
        content = "alpha durable value" if "remember alpha" in rendered else "beta durable value"
        return _phase1_response(content)

    monkeypatch.setattr("src.memory.pipeline.build_model_call", lambda _config: fake_call)
    for job_id in job_ids:
        await memory_pipeline.process(job_id)

    with database.SessionLocal() as db:
        rollouts = list(db.scalars(select(MemoryRollout).order_by(MemoryRollout.source_end_sequence)))
        assert len(rollouts) == 2
        assert len({rollout.source_end_sequence for rollout in rollouts}) == 2
        assert {rollout.raw_memories[0]["content"] for rollout in rollouts} == {
            "alpha durable value", "beta durable value",
        }


@pytest.mark.asyncio
# 测试场景：验证权限、审批或敏感数据边界在完整调用链路中保持有效；函数名 test_memory_job_error_redacts_provider_credentials 精确标识本用例的具体条件。
async def test_memory_job_error_redacts_provider_credentials(pipeline_db, monkeypatch: pytest.MonkeyPatch) -> None:
    _root, workspace_id, session_id, connection_id = pipeline_db
    with database.SessionLocal() as db:
        connection = db.get(ModelConnection, connection_id)
        assert connection is not None
        job = MemoryJob(
            workspace_id=workspace_id,
            session_id=session_id,
            kind="extract",
            payload={"runtime_binding": _binding(connection)},
        )
        db.add(job); db.commit(); job_id = job.id

    # 局部测试函数：fake_call 模拟该步骤的返回结果或异常。
    async def fake_call(**_kwargs):
        raise RuntimeError("request failed: api_token=provider-secret-value")

    monkeypatch.setattr("src.memory.pipeline.build_model_call", lambda _config: fake_call)
    await memory_pipeline.process(job_id)

    with database.SessionLocal() as db:
        job = db.get(MemoryJob, job_id)
        assert job is not None and job.status == "pending"
        assert "provider-secret-value" not in str(job.error)
        assert "[REDACTED]" in str(job.error)


@pytest.mark.asyncio
# 测试场景：验证非法、越界或不满足前置条件的操作会被明确拒绝，且不会产生错误状态；函数名 test_unavailable_connection_releases_consolidation_reservation 精确标识本用例的具体条件。
async def test_unavailable_connection_releases_consolidation_reservation(pipeline_db) -> None:
    _root, workspace_id, session_id, _connection_id = pipeline_db
    with database.SessionLocal() as db:
        extraction = MemoryJob(
            workspace_id=workspace_id,
            session_id=session_id,
            kind="extract",
            status="completed",
        )
        db.add(extraction); db.flush()
        job = MemoryJob(
            workspace_id=workspace_id,
            session_id=session_id,
            kind="consolidate",
            payload={"runtime_binding": {"model_connection_id": "missing"}},
        )
        db.add(job); db.flush()
        rollout = MemoryRollout(
            source_session_id=session_id,
            workspace_id=workspace_id,
            extraction_job_id=extraction.id,
            source_end_sequence=1,
            rollout_slug="reserved",
            rollout_summary="{}",
            raw_memories=[{"content": "durable"}],
            keywords=["durable"],
            task_groups=["test"],
            outcome="success",
            status="consolidating",
            consolidation_job_id=job.id,
        )
        db.add(rollout); db.commit(); job_id = job.id; rollout_id = rollout.id

    await memory_pipeline.process(job_id)

    with database.SessionLocal() as db:
        job = db.get(MemoryJob, job_id)
        rollout = db.get(MemoryRollout, rollout_id)
        assert job is not None and job.status == "failed"
        assert rollout is not None and rollout.status == "active"
        assert rollout.consolidation_job_id is None


# 测试场景：验证状态能够可靠持久化、重放或在重启后恢复，并保持记录之间的关联；函数名 test_recovery_marks_exhausted_running_memory_job_failed 精确标识本用例的具体条件。
def test_recovery_marks_exhausted_running_memory_job_failed(pipeline_db) -> None:
    _root, workspace_id, session_id, _connection_id = pipeline_db
    with database.SessionLocal() as db:
        job = MemoryJob(
            workspace_id=workspace_id,
            session_id=session_id,
            kind="extract",
            status="running",
            attempts=3,
            lease_expires_at=None,
        )
        db.add(job); db.commit(); job_id = job.id

    assert RunCoordinator.pending_memory_job_ids(recover_running=True) == []
    with database.SessionLocal() as db:
        assert db.get(MemoryJob, job_id).status == "failed"


@pytest.mark.asyncio
# 测试场景：验证该正常业务场景从输入准备到结果断言的完整链路；函数名 test_expired_worker_is_fenced_from_overwriting_new_attempt 精确标识本用例的具体条件。
async def test_expired_worker_is_fenced_from_overwriting_new_attempt(pipeline_db, monkeypatch: pytest.MonkeyPatch) -> None:
    _root, workspace_id, session_id, connection_id = pipeline_db
    with database.SessionLocal() as db:
        connection = db.get(ModelConnection, connection_id)
        assert connection is not None
        job = MemoryJob(
            workspace_id=workspace_id, session_id=session_id, kind="extract",
            payload={"runtime_binding": _binding(connection)},
        )
        db.add(job); db.commit(); job_id = job.id

    first_started = asyncio.Event()
    release_first = asyncio.Event()
    calls = 0

    # 局部测试函数：fake_call 模拟该步骤的返回结果或异常。
    async def fake_call(**_kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            first_started.set()
            await release_first.wait()
            return _phase1_response("Late stale worker value must not win.")
        return _phase1_response("Winning second worker value is durable.")

    monkeypatch.setattr("src.memory.pipeline.build_model_call", lambda _config: fake_call)
    stale = asyncio.create_task(memory_pipeline.process(job_id))
    await first_started.wait()
    with database.SessionLocal() as db:
        job = db.get(MemoryJob, job_id)
        assert job is not None
        job.status = "pending"
        job.lease_expires_at = None
        db.commit()
    await memory_pipeline.process(job_id)
    release_first.set()
    await stale

    with database.SessionLocal() as db:
        job = db.get(MemoryJob, job_id)
        rollouts = list(db.scalars(select(MemoryRollout)))
        assert job is not None and job.status == "completed" and job.attempts == 2
        assert [row.raw_memories[0]["content"] for row in rollouts] == [
            "Winning second worker value is durable."
        ]


@pytest.mark.asyncio
# 测试场景：验证该正常业务场景从输入准备到结果断言的完整链路；函数名 test_two_phase_pipeline_consolidates_and_citations_feed_usage 精确标识本用例的具体条件。
async def test_two_phase_pipeline_consolidates_and_citations_feed_usage(pipeline_db, monkeypatch: pytest.MonkeyPatch) -> None:
    root, workspace_id, session_id, connection_id = pipeline_db
    with database.SessionLocal() as db:
        connection = db.get(ModelConnection, connection_id)
        assert connection is not None
        run = Run(session_id=session_id, workspace_id=workspace_id, status="completed")
        db.add(run); db.flush()
        db.add_all([
            ChatMessage(session_id=session_id, role="user", content="Always use uv run pytest.", message_kind="user_request", sequence=1),
            ChatMessage(session_id=session_id, role="assistant", content="Verified with uv run pytest.", message_kind="terminal", sequence=2),
        ])
        extract_job = MemoryJob(
            run_id=run.id,
            workspace_id=workspace_id,
            session_id=session_id,
            kind="extract",
            payload={"source_end_sequence": 2, "runtime_binding": _binding(connection)},
        )
        db.add(extract_job); db.commit()
        extract_job_id = extract_job.id
        run_id = run.id

    phase1 = {
        "worth_remembering": True,
        "rollout_slug": "backend-test-command",
        "rollout_summary": {
            "primary_request": "Run backend tests",
            "user_corrections": [],
            "completed_work": ["Tests passed"],
            "files_and_code": [],
            "decisions": ["Use uv"],
            "errors_and_fixes": [],
            "verification": ["uv run pytest passed"],
            "outcome": "success",
            "pending_or_followup": [],
        },
        "raw_memories": [{
            "kind": "procedure",
            "task": "Run backend tests",
            "task_group": "backend-testing",
            "cwd": str(root / "project"),
            "keywords": ["uv run pytest", "backend-testing"],
            "content": "Run backend tests with uv run pytest.",
            "evidence": [{"type": "user", "sequence": 1, "summary": "explicit command"}],
            "confidence": "explicit_user",
        }],
    }

    responses: list[dict] = [{
        "choices": [{"message": {"content": json.dumps(phase1)}}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    }]

    # 局部测试函数：fake_call 模拟该步骤的返回结果或异常。
    async def fake_call(**_kwargs):
        return responses.pop(0)

    monkeypatch.setattr("src.memory.pipeline.build_model_call", lambda _config: fake_call)
    await memory_pipeline.process(extract_job_id)

    with database.SessionLocal() as db:
        rollout = db.scalar(select(MemoryRollout))
        consolidation = db.scalar(select(MemoryJob).where(MemoryJob.kind == "consolidate"))
        assert rollout is not None and consolidation is not None
        consolidation_id = consolidation.id
        responses.append({
            "choices": [{"message": {"content": json.dumps({
                "upserts": [{
                    "memory_id": "",
                    "name": "Backend test command",
                    "memory_type": "project",
                    "description": "How to run backend tests",
                    "content": "Use `uv run pytest` for backend tests.",
                    "tags": ["uv run pytest", "backend-testing"],
                    "scope": "workspace",
                    "workspace_id": workspace_id,
                    "source_rollout_ids": [rollout.id],
                }],
                "archive_memory_ids": [],
                "selected_rollout_ids": [rollout.id],
            })}}],
            "usage": {"total_tokens": 20},
        })

    await memory_pipeline.process(consolidation_id)

    with database.SessionLocal() as db:
        memory = db.scalar(select(Memory).where(Memory.status == "active"))
        rollout = db.scalar(select(MemoryRollout))
        run = db.get(Run, run_id)
        assert memory is not None and rollout is not None and run is not None
        assert memory.extra["source_rollout_ids"] == [rollout.id]
        assert rollout.selected_for_phase2_at is not None
        record_memory_citations(
            db,
            run=run,
            citation={"memory_ids": [memory.id], "rollout_ids": [rollout.id], "note": "test command"},
        )
        # Exactly-once usage credit for a repeated persistence attempt.
        record_memory_citations(
            db,
            run=run,
            citation={"memory_ids": [memory.id], "rollout_ids": [rollout.id], "note": "test command"},
        )
        db.commit()
        assert memory.usage_count == 1
        assert rollout.usage_count == 1
        assert len(list(db.scalars(select(MemoryCitation)))) == 2

        other = Workspace(name="Other", root_path="C:/other")
        db.add(other); db.flush()
        foreign = store_memory(
            db, name="Foreign", content="Other workspace only.", scope="workspace", workspace_id=other.id
        )
        record_memory_citations(
            db,
            run=run,
            citation={"memory_ids": [foreign.id], "rollout_ids": [], "skill_ids": [], "note": "invalid"},
        )
        db.flush()
        assert foreign.usage_count == 0

    summary = (root / "memories" / "memory_summary.md").read_text(encoding="utf-8")
    assert "Backend test command" in summary
    assert (root / "memories" / "rollout_summaries" / f"{rollout.id}.md").is_file()
