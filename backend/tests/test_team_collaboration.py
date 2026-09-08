"""验证持久化协作者、数据库邮箱、Git 工作树创建、变更集成与会话清理。

测试通过 fixture 或辅助函数准备隔离环境，再调用真实服务、路由或运行时，并检查返回值、持久化状态与可观察副作用。
变量约定：tmp_path/monkeypatch 提供隔离环境，client/store/runtime 驱动被测链路，各类 *_id 串联持久化实体，payload 表示输入，response/result 表示实际输出，expected 表示期望值。
"""

from __future__ import annotations

import json
from pathlib import Path
import subprocess

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


# 辅助函数：_git 封装本组测试重复使用的输入准备、状态查询或测试替身行为。
def _git(repo: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo), *arguments],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=True,
    )


# 测试场景：验证非法、越界或不满足前置条件的操作会被明确拒绝，且不会产生错误状态；函数名 test_worktree_provisioning_does_not_hold_sqlite_writer_lock 精确标识本用例的具体条件。
def test_worktree_provisioning_does_not_hold_sqlite_writer_lock(
    team_db, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    writes_during_git: list[str] = []

    # 局部测试函数：fake_create 模拟该步骤的返回结果或异常。
    def fake_create(_workspace_root: str, worker_id: str, _worktree_root=None):  # type: ignore[no-untyped-def]
        with database.SessionLocal() as db:
            db.add(RunEvent(
                run_id=team_db["run_id"],
                event_type="concurrent_write_during_worktree",
                payload={"worker_id": worker_id},
            ))
            db.commit()
            writes_during_git.append(worker_id)
        return str(tmp_path / "fake-worktree" / worker_id), f"pgagent/{worker_id[:12]}"

    monkeypatch.setattr(team_service, "_create_worktree", fake_create)
    store = TeamToolStore(
        run_id=team_db["run_id"],
        session_id=team_db["session_id"],
        workspace_root=str(tmp_path),
    )

    result = store.spawn(
        name="lock-free-worker",
        role="implementation",
        prompt="Provision without blocking SQLite.",
        agent_id=team_db["child_id"],
        workspace_mode="worktree",
    )

    assert result.ok
    assert writes_during_git == [result.metadata["teammate_id"]]
    with database.SessionLocal() as db:
        assert db.query(RunEvent).filter_by(event_type="concurrent_write_during_worktree").count() == 1


# 测试场景：验证该正常业务场景从输入准备到结果断言的完整链路；函数名 test_worktree_teammate_changes_are_explicitly_integrated 精确标识本用例的具体条件。
def test_worktree_teammate_changes_are_explicitly_integrated(team_db, tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    _git(repository, "init")
    (repository / "README.md").write_text("base\n", encoding="utf-8")
    _git(repository, "add", "README.md")
    _git(
        repository,
        "-c", "user.name=PGAgent Test",
        "-c", "user.email=pgagent-test@local",
        "commit", "-m", "baseline",
    )

    store = TeamToolStore(
        run_id=team_db["run_id"],
        session_id=team_db["session_id"],
        workspace_root=str(repository),
        worktree_root=tmp_path / "worktrees",
    )
    spawned = store.spawn(
        name="isolated-worker",
        role="implementation",
        prompt="Implement in the isolated branch.",
        agent_id=team_db["child_id"],
        workspace_mode="worktree",
    )
    worker = json.loads(spawned.content)
    worktree = Path(worker["worktree_path"])
    (worktree / "parallel.txt").write_text("implemented independently\n", encoding="utf-8")
    (worktree / "local-secret.txt").write_text("must remain isolated\n", encoding="utf-8")
    _git(worktree, "add", "local-secret.txt")

    integrated = store.integrate(
        teammate=worker["id"],
        commit_message="parallel teammate result",
        paths=["parallel.txt"],
    )

    assert spawned.ok and integrated.ok
    assert (repository / "parallel.txt").read_text(encoding="utf-8") == "implemented independently\n"
    assert not (repository / "local-secret.txt").exists()
    assert "?? local-secret.txt" in _git(worktree, "status", "--porcelain").stdout
    assert json.loads(integrated.content)["status"] == "integrated"
    assert "parallel teammate result" in _git(repository, "log", "--oneline", "--all").stdout


# 测试场景：验证接口或资源生命周期操作会返回正确结果并同步持久化状态；函数名 test_session_delete_removes_teammate_worktree_and_branch 精确标识本用例的具体条件。
def test_session_delete_removes_teammate_worktree_and_branch(team_db, tmp_path: Path) -> None:
    repository = tmp_path / "delete-repository"
    repository.mkdir()
    _git(repository, "init")
    (repository / "README.md").write_text("base\n", encoding="utf-8")
    _git(repository, "add", "README.md")
    _git(
        repository,
        "-c", "user.name=PGAgent Test",
        "-c", "user.email=pgagent-test@local",
        "commit", "-m", "baseline",
    )
    with database.SessionLocal() as db:
        db.get(Workspace, DEFAULT_WORKSPACE_ID).root_path = str(repository)
        db.commit()

    store = TeamToolStore(
        run_id=team_db["run_id"],
        session_id=team_db["session_id"],
        workspace_root=str(repository),
        worktree_root=tmp_path / "delete-worktrees",
    )
    spawned = store.spawn(
        name="disposable-worker",
        role="implementation",
        prompt="Create an isolated branch.",
        agent_id=team_db["child_id"],
        workspace_mode="worktree",
    )
    worker = json.loads(spawned.content)
    worktree = Path(worker["worktree_path"])
    branch = worker["branch_name"]
    assert spawned.ok and worktree.exists()

    with database.SessionLocal() as db:
        db.get(Run, team_db["run_id"]).status = "completed"
        db.commit()
        response = delete_session(team_db["session_id"], db)

    assert response.status_code == 204
    assert not worktree.exists()
    assert not _git(repository, "branch", "--list", branch).stdout.strip()
    assert str(worktree.resolve()) not in _git(repository, "worktree", "list", "--porcelain").stdout


# 测试场景：验证时间、容量或上下文预算边界以及达到边界后的可观察处理结果；函数名 test_worktree_merge_timeout_aborts_merge 精确标识本用例的具体条件。
def test_worktree_merge_timeout_aborts_merge(
    team_db, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = tmp_path / "timeout-repository"
    repository.mkdir()
    _git(repository, "init")
    (repository / "README.md").write_text("base\n", encoding="utf-8")
    _git(repository, "add", "README.md")
    _git(
        repository,
        "-c", "user.name=PGAgent Test",
        "-c", "user.email=pgagent-test@local",
        "commit", "-m", "baseline",
    )
    store = TeamToolStore(
        run_id=team_db["run_id"],
        session_id=team_db["session_id"],
        workspace_root=str(repository),
        worktree_root=tmp_path / "timeout-worktrees",
    )
    spawned = store.spawn(
        name="timeout-worker",
        role="implementation",
        prompt="Create a commit that will time out during merge.",
        agent_id=team_db["child_id"],
        workspace_mode="worktree",
    )
    worker = json.loads(spawned.content)
    (Path(worker["worktree_path"]) / "timeout.txt").write_text("pending merge\n", encoding="utf-8")

    original_run = team_service.subprocess.run
    abort_calls: list[list[str]] = []

    # 辅助方法：timed_merge 实现测试替身在此调用阶段需要的最小行为。
    def timed_merge(arguments, *args, **kwargs):  # type: ignore[no-untyped-def]
        command = [str(item) for item in arguments]
        if "merge" in command and "--abort" not in command:
            raise subprocess.TimeoutExpired(command, 120)
        if "merge" in command and "--abort" in command:
            abort_calls.append(command)
        return original_run(arguments, *args, **kwargs)

    monkeypatch.setattr(team_service.subprocess, "run", timed_merge)
    result = store.integrate(teammate=worker["id"], paths=["timeout.txt"])

    assert not result.ok
    assert result.error_code == "worktree_merge_timeout"
    assert len(abort_calls) == 1
