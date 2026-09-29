"""验证持久化协作者、数据库邮箱与共享目录协作。

测试通过 fixture 或辅助函数准备隔离环境，再调用真实服务、路由或运行时，并检查返回值、持久化状态与可观察副作用。
变量约定：tmp_path/monkeypatch 提供隔离环境，client/store/runtime 驱动被测链路，各类 *_id 串联持久化实体，payload 表示输入，response/result 表示实际输出，expected 表示期望值。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.persistence import database
from src.agents import collaboration as team_service
from src.api.routes import (
    delete_session,
    list_session_collaboration_events,
    list_session_collaboration_messages,
    list_session_teammates,
)
from src.persistence.database import (
    Agent,
    Base,
    CollaborationMessage,
    DEFAULT_AGENT_ID,
    DEFAULT_WORKSPACE_ID,
    DelegatedTask,
    DurableTask,
    Run,
    RunEvent,
    Session,
    TeammateWorker,
    Workspace,
)
from src.agents.collaboration import TeamToolStore, teammate_context
from src.tools import create_default_registry


@pytest.fixture()
# 测试夹具：team_db 创建本组用例共享的隔离资源，并在测试结束后恢复数据库、配置或进程状态。
def team_db(tmp_path: Path):
    # 临时数据库中的 child/session/run 标识组成协作上下文，供邮箱与工作树用例复用同一持久关系。
    database.configure_database(f"sqlite:///{(tmp_path / 'teams.db').as_posix()}")
    database.init_db()
    with database.SessionLocal() as db:
        child = Agent(
            name="Persistent researcher",
            description="Reusable teammate",
            system_prompt="Work as a durable teammate.",
            enabled=True,
        )
        session = Session(
            title="Team conversation",
            workspace_id=DEFAULT_WORKSPACE_ID,
            agent_id=DEFAULT_AGENT_ID,
        )
        db.add_all([child, session])
        db.flush()
        task = DurableTask(
            session_id=session.id,
            goal="Complete the collaboration task",
            status="running",
        )
        db.add(task)
        db.flush()
        run = Run(
            session_id=session.id,
            workspace_id=DEFAULT_WORKSPACE_ID,
            agent_id=DEFAULT_AGENT_ID,
            task_id=task.id,
            status="received",
        )
        db.add(run)
        db.commit()
        yield {
            "run_id": run.id,
            "session_id": session.id,
            "child_id": child.id,
            "task_id": task.id,
        }
    Base.metadata.drop_all(bind=database.engine)


# 测试场景：验证状态能够可靠持久化、重放或在重启后恢复，并保持记录之间的关联；函数名 test_persistent_teammate_reuse_and_database_mailbox 精确标识本用例的具体条件。
def test_persistent_teammate_reuse_and_database_mailbox(team_db, tmp_path: Path) -> None:
    lead = TeamToolStore(
        run_id=team_db["run_id"],
        session_id=team_db["session_id"],
        workspace_root=str(tmp_path),
    )
    created = lead.spawn(
        name="researcher",
        role="evidence analyst",
        prompt="Remember prior evidence between assignments.",
        agent_id=team_db["child_id"],
    )
    reused = lead.spawn(
        name="RESEARCHER",
        role="ignored on reuse",
        prompt="ignored on reuse",
        agent_id=team_db["child_id"],
    )
    worker_id = json.loads(created.content)["id"]

    assert created.ok and reused.ok
    assert json.loads(reused.content)["id"] == worker_id
    assert reused.metadata["reused"] is True

    assert lead.send_message(to=worker_id, content="Inspect the scheduler.").ok
    teammate_store = TeamToolStore(
        run_id=team_db["run_id"],
        session_id=team_db["session_id"],
        workspace_root=str(tmp_path),
        actor_worker_id=worker_id,
    )
    inbox = teammate_store.read_inbox()
    assert inbox.ok
    assert [message["content"] for message in json.loads(inbox.content)] == ["Inspect the scheduler."]

    registry = create_default_registry(
        str(tmp_path),
        allowed_tool_names=["send_message"],
        permission_mode="full",
        team_store=teammate_store,
    )
    sent = registry.execute("send_message", {"to": "lead", "content": "Scheduler evidence ready."})
    lead_inbox = lead.read_inbox()
    assert sent.ok
    assert json.loads(lead_inbox.content)[0]["from"] == worker_id

    with database.SessionLocal() as db:
        worker = db.get(TeammateWorker, worker_id)
        db.add(DelegatedTask(
            parent_run_id=team_db["run_id"],
            parent_session_id=team_db["session_id"],
            child_agent_id=team_db["child_id"],
            teammate_id=worker_id,
            title="Inspect scheduler",
            description="Inspect scheduler",
            status="completed",
            idempotency_key="team-history",
            result={"output": "parallel waves confirmed"},
        ))
        db.add(CollaborationMessage(
            team_id=worker.team_id,
            recipient_worker_id=worker_id,
            message_type="follow_up",
            content="Now inspect recovery.",
        ))
        db.commit()
        context = teammate_context(db, worker)
        db.commit()
    assert "parallel waves confirmed" in context
    assert "Now inspect recovery." in context

    with database.SessionLocal() as db:
        recovery_run = Run(
            session_id=team_db["session_id"],
            workspace_id=DEFAULT_WORKSPACE_ID,
            agent_id=DEFAULT_AGENT_ID,
            task_id=team_db["task_id"],
            run_kind="recovery",
            status="received",
        )
        db.add(recovery_run)
        db.commit()
        recovery_run_id = recovery_run.id
    recovery_store = TeamToolStore(
        run_id=recovery_run_id,
        session_id=team_db["session_id"],
        workspace_root=str(tmp_path),
    )
    recovered = recovery_store.spawn(
        name="researcher",
        role="evidence analyst",
        prompt="Keep prior context.",
        agent_id=team_db["child_id"],
    )
    assert recovered.ok
    assert json.loads(recovered.content)["id"] == worker_id

    with database.SessionLocal() as db:
        assert [item.id for item in list_session_teammates(team_db["session_id"], db)] == [worker_id]
        assert list_session_collaboration_messages(team_db["session_id"], 200, db)
        assert list_session_collaboration_events(team_db["session_id"], 200, db)
