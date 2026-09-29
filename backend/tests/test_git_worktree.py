from __future__ import annotations

import subprocess

import pytest
from pathlib import Path

from src.coding.git import (
    collect_git_info,
    create_managed_worktree,
    current_branch_name,
    list_managed_worktrees,
    remove_managed_worktree,
)
from src.config import settings


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, check=True)
    return result.stdout.strip()


def _repo(root: Path) -> None:
    _git(root, "init", "--initial-branch=master")
    _git(root, "config", "user.name", "PGAgent tests")
    _git(root, "config", "user.email", "pgagent-tests@example.invalid")
    (root / "README.md").write_text("initial\n", encoding="utf-8")
    _git(root, "add", "README.md")
    _git(root, "commit", "-m", "initial")


def test_collect_git_info_reports_current_master_and_detached_head(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    _repo(root)

    info = collect_git_info(root)
    assert info is not None
    assert info.branch == "master"
    assert info.commit_hash == _git(root, "rev-parse", "HEAD")

    _git(root, "checkout", "--detach", "HEAD")
    assert current_branch_name(root) is None
    assert collect_git_info(root).branch is None  # type: ignore[union-attr]


def test_managed_worktree_is_detached_and_can_be_removed(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    _repo(root)
    data_dir = tmp_path / "data"
    monkeypatch.setattr(type(settings), "data_dir", property(lambda _settings: data_dir))

    checkout = create_managed_worktree(root)
    assert checkout.branch is None
    assert Path(checkout.cwd).is_dir()
    assert list_managed_worktrees(root)[0].root == checkout.root

    remove_managed_worktree(root, checkout.root)
    assert not Path(checkout.root).exists()


def test_non_git_and_missing_directory_are_distinct(tmp_path):
    from src.coding.git import git_state
    assert git_state(tmp_path)["git_info"] is None
    assert git_state(tmp_path)["git_error"] is None
    assert git_state(tmp_path / "missing")["git_error"]


def test_default_branch_and_owner_conflict(tmp_path):
    from src.coding.git import default_worktree_base, bind_worktree_owner, read_worktree_owner, GitOperationError
    _repo(tmp_path)
    _git(tmp_path, "checkout", "-b", "feature")
    assert default_worktree_base(tmp_path) == "refs/heads/master"
    checkout = create_managed_worktree(tmp_path)
    bind_worktree_owner(checkout.root, "owner-one")
    bind_worktree_owner(checkout.root, "owner-one")
    with pytest.raises(GitOperationError, match="belongs"):
        bind_worktree_owner(checkout.root, "owner-two")
    assert read_worktree_owner(checkout.root) == "owner-one"


def test_worktree_dirty_ignored_and_current_deletion_is_refused(tmp_path):
    from src.coding.git import GitOperationError
    _repo(tmp_path)
    checkout = create_managed_worktree(tmp_path)
    root = Path(checkout.root)
    with pytest.raises(GitOperationError, match="current worktree"):
        remove_managed_worktree(root, root)
    (root / "README.md").write_text("dirty", encoding="utf-8")
    with pytest.raises(GitOperationError):
        remove_managed_worktree(tmp_path, root)
    assert root.exists()
    _git(root, "checkout", "--", "README.md")
    _git(root, "config", "core.excludesFile", str(tmp_path / "ignore"))
    (tmp_path / "ignore").write_text("private.secret\n", encoding="utf-8")
    (root / "private.secret").write_text("local", encoding="utf-8")
    with pytest.raises(GitOperationError, match="ignored local"):
        remove_managed_worktree(tmp_path, root)


def test_creation_preserves_relative_cwd_and_rejects_other_repo(tmp_path):
    from src.coding.git import require_managed_worktree, GitOperationError
    repo = tmp_path / "repo"
    repo.mkdir()
    _repo(repo)
    (repo / "package").mkdir()
    (repo / "package" / "file").write_text("file")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "subdir")
    checkout = create_managed_worktree(repo / "package")
    assert checkout.cwd == str(Path(checkout.root) / "package")
    assert list_managed_worktrees(repo / "package")[0].cwd == checkout.cwd
    other = tmp_path / "other"
    other.mkdir()
    _repo(other)
    with pytest.raises(GitOperationError, match="not a managed"):
        require_managed_worktree(other, checkout.root)


def test_missing_base_directory_rolls_back_git_registration(tmp_path):
    from src.coding.git import GitOperationError
    _repo(tmp_path)
    initial = _git(tmp_path, "rev-parse", "HEAD")
    (tmp_path / "later").mkdir()
    (tmp_path / "later" / "file").write_text("later")
    _git(tmp_path, "add", "later")
    _git(tmp_path, "commit", "-m", "later")
    with pytest.raises(GitOperationError, match="safe working directory"):
        create_managed_worktree(tmp_path / "later", initial)
    assert list_managed_worktrees(tmp_path) == []


@pytest.fixture
def api_client(tmp_path):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from src.persistence import database
    from src.api.routes import router
    database.configure_database(f"sqlite:///{tmp_path / 'api.db'}")
    database.init_db()
    app = FastAPI()
    app.include_router(router)
    with TestClient(app) as client:
        yield client
    database.engine.dispose()


def test_worktree_session_full_api_chain(api_client, tmp_path):
    from src.persistence import database
    from src.persistence.models import Run
    repo = tmp_path / "project"
    repo.mkdir()
    _repo(repo)
    project = api_client.post("/api/workspaces", json={"root_path": str(repo)}).json()
    assert project["git_info"]["branch"] == "master"
    prefix = f"/api/workspaces/{project['id']}/worktrees"
    response = api_client.post(prefix, json={"use_default_branch": True})
    assert response.status_code == 201, response.text
    checkout = response.json()
    response = api_client.post("/api/sessions", json={"workspace_id": project["id"], "cwd": checkout["cwd"]})
    assert response.status_code == 201, response.text
    session = response.json()
    info = api_client.get(f"/api/sessions/{session['id']}/git").json()
    assert info["cwd"] == checkout["cwd"]
    assert info["git_info"]["branch"] is None
    duplicate = api_client.post("/api/sessions", json={"workspace_id": project["id"], "cwd": checkout["cwd"]})
    assert duplicate.status_code == 409
    assert len(api_client.get("/api/sessions").json()) == 1
    current_delete = api_client.delete(prefix, params={"path": checkout["root"], "session_id": session["id"]})
    assert current_delete.status_code == 409
    patch = api_client.patch(f"/api/sessions/{session['id']}", json={"cwd": str(tmp_path)})
    assert patch.status_code == 409
    with database.SessionLocal() as db:
        run = Run(session_id=session["id"], workspace_id=project["id"], cwd=checkout["cwd"], status="running")
        db.add(run)
        db.commit()
        run_id = run.id
    blocked = api_client.delete(prefix, params={"path": checkout["root"]})
    assert blocked.status_code == 409
    (Path(checkout["cwd"]) / "README.md").write_text("checkout file", encoding="utf-8")
    content = api_client.get(f"/api/runs/{run_id}/file-content", params={"path": "README.md"})
    assert content.status_code == 200, content.text
    assert content.json()["content"] == "checkout file"
    _git(Path(checkout["cwd"]), "checkout", "--", "README.md")
    with database.SessionLocal() as db:
        run = db.get(Run, run_id)
        run.status = "completed"
        db.commit()
    assert api_client.delete(prefix, params={"path": checkout["root"]}).status_code == 204
    assert api_client.get(f"/api/sessions/{session['id']}/git").json()["git_error"]
    assert api_client.get(f"/api/sessions/{session['id']}").status_code == 200


def test_bind_refuses_populated_session_and_preserves_cwd(api_client, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _repo(repo)
    project = api_client.post("/api/workspaces", json={"root_path": str(repo)}).json()
    prefix = f"/api/workspaces/{project['id']}/worktrees"
    checkout = api_client.post(prefix, json={}).json()
    session = api_client.post("/api/sessions", json={"workspace_id": project["id"]}).json()
    api_client.post(f"/api/sessions/{session['id']}/messages", json={"role": "user", "content": "hi"})
    response = api_client.post(prefix + "/bind", json={"path": checkout["root"], "session_id": session["id"]})
    assert response.status_code == 409
    assert api_client.get(f"/api/sessions/{session['id']}").json()["cwd"] == str(repo)
    empty = api_client.post("/api/sessions", json={"workspace_id": project["id"]}).json()
    response = api_client.post(prefix + "/bind", json={"path": checkout["root"], "session_id": empty["id"]})
    assert response.status_code == 200, response.text
    assert response.json()["cwd"] == checkout["cwd"]


def test_retired_not_null_column_migration_preserves_history(tmp_path):
    from src.persistence import database
    database.configure_database(f"sqlite:///{tmp_path / 'old.db'}")
    # 原结构含无 DEFAULT 的 NOT NULL 列；数据必须保留，新增 ORM insert 不提供它。
    with database.engine.begin() as conn:
        conn.exec_driver_sql("CREATE TABLE plan_steps (id TEXT PRIMARY KEY, workspace_mode VARCHAR(24) NOT NULL, worktree_path TEXT)")
        conn.exec_driver_sql("INSERT INTO plan_steps VALUES ('old', 'worktree', '/historic')")
        conn.exec_driver_sql("CREATE INDEX example_index ON plan_steps(id)")
    database._migrate_retired_workspace_mode_defaults()
    database._migrate_retired_workspace_mode_defaults()
    with database.engine.begin() as conn:
        conn.exec_driver_sql("INSERT INTO plan_steps (id) VALUES ('new')")
        assert conn.exec_driver_sql("SELECT workspace_mode, worktree_path FROM plan_steps WHERE id='old'").one() == ("worktree", "/historic")
        assert conn.exec_driver_sql("SELECT workspace_mode FROM plan_steps WHERE id='new'").scalar() == "shared"
        assert conn.exec_driver_sql("PRAGMA foreign_key_check").fetchall() == []
    database.engine.dispose()


def test_runtime_uses_session_checkout(api_client, tmp_path):
    from src.persistence import database
    from src.persistence.models import ModelConnection, Run, Agent
    from src.persistence.database import DEFAULT_AGENT_ID
    from src.runs.service import RunCoordinator
    repo = tmp_path / "repo"
    repo.mkdir()
    _repo(repo)
    project = api_client.post("/api/workspaces", json={"root_path": str(repo)}).json()
    checkout = api_client.post(f"/api/workspaces/{project['id']}/worktrees", json={}).json()
    session = api_client.post("/api/sessions", json={"workspace_id": project["id"], "cwd": checkout["cwd"]}).json()
    with database.SessionLocal() as db:
        connection = ModelConnection(name="test", base_url="https://example.invalid/v1", secret_ref="test", default_model="test-model")
        db.add(connection)
        db.flush()
        db_session = db.get(database.Session, session["id"])
        db_session.model_connection_id = connection.id
        db_session.model_id = "test-model"
        run = Run(session_id=session["id"], workspace_id=project["id"], agent_id=DEFAULT_AGENT_ID, status="received")
        db.add(run)
        db.commit()
        run_id = run.id
    runtime, context = RunCoordinator._resolve_runtime(run_id)
    assert context["workspace_root"] == checkout["cwd"]
    assert context["runtime_binding"]["workspace_root"] == checkout["cwd"]
    with database.SessionLocal() as db:
        assert db.get(Run, run_id).cwd == checkout["cwd"]
        from src.runs.delegation import _SubagentTaskDelegate
        child = Agent(name="Shared child", enabled=True)
        db.add(child)
        db.flush()
        delegate = _SubagentTaskDelegate(parent_run_id=run_id, parent_agent_id=DEFAULT_AGENT_ID,
            parent_binding=context["runtime_binding"], parent_allowed_tool_names=context["allowed_tool_names"], permission_mode="smart")
        _, child_binding, _ = delegate._freeze_child_binding(db, child)
        assert child_binding["workspace_root"] == checkout["cwd"]
        assert "workspace_mode" not in child_binding
    remove_managed_worktree(repo, checkout["root"])
    from src.runs.configuration import ModelConfigurationError
    with pytest.raises(ModelConfigurationError, match="工作目录不存在"):
        RunCoordinator._resolve_runtime(run_id)
    assert not Path(checkout["root"]).exists()


def test_session_commit_failure_releases_only_new_owner(api_client, tmp_path, monkeypatch):
    from src.api.routes import sessions as routes
    from src.coding.git import read_worktree_owner
    repo = tmp_path / "repo"
    repo.mkdir()
    _repo(repo)
    project = api_client.post("/api/workspaces", json={"root_path": str(repo)}).json()
    checkout = api_client.post(f"/api/workspaces/{project['id']}/worktrees", json={}).json()
    def fail(db):
        db.rollback()
        raise ValueError("simulated transaction failure")
    monkeypatch.setattr(routes, "_commit", fail)
    result = api_client.post("/api/sessions", json={"workspace_id": project["id"], "cwd": checkout["cwd"]})
    assert result.status_code == 409
    assert read_worktree_owner(checkout["root"]) is None
    assert api_client.get("/api/sessions").json() == []


@pytest.mark.parametrize("delete_project", [False, True])
def test_delete_owner_preserves_dirty_checkout_for_new_session(api_client, tmp_path, delete_project):
    from src.coding.git import read_worktree_owner
    repo = tmp_path / "repo"
    repo.mkdir()
    _repo(repo)
    project = api_client.post("/api/workspaces", json={"root_path": str(repo)}).json()
    prefix = f"/api/workspaces/{project['id']}/worktrees"
    checkout = api_client.post(prefix, json={}).json()
    session = api_client.post("/api/sessions", json={"workspace_id": project["id"], "cwd": checkout["cwd"]}).json()
    file = Path(checkout["cwd"]) / "README.md"
    file.write_text("keep my edits", encoding="utf-8")
    endpoint = f"/api/workspaces/{project['id']}" if delete_project else f"/api/sessions/{session['id']}"
    response = api_client.delete(endpoint)
    assert response.status_code == 204, response.text
    assert file.read_text(encoding="utf-8") == "keep my edits"
    assert read_worktree_owner(checkout["root"]) is None
    if delete_project:
        project = api_client.post("/api/workspaces", json={"root_path": str(repo)}).json()
    replacement = api_client.post("/api/sessions", json={"workspace_id": project["id"], "cwd": checkout["cwd"]})
    assert replacement.status_code == 201, replacement.text
    assert read_worktree_owner(checkout["root"]) == replacement.json()["id"]
