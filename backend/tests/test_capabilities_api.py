from __future__ import annotations

import io
import zipfile
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import database
from app.api.capabilities import router as capabilities_router
from app.api.resources import router as resources_router
from app.api.runtime import router as runtime_router
from app.capabilities import BUILTIN_TOOL_IDS
from app.database import (
    DEFAULT_AGENT_ID,
    Agent,
    Base,
    ChatMessage,
    DraftLaunch,
    Run,
    Session,
    Skill,
    configure_database,
    init_db,
)
from app.services import skill_service
from app.services.run_service import coordinator


@pytest.fixture()
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[TestClient, dict[str, list]]:
    configure_database(f"sqlite:///{(tmp_path / 'capabilities.db').as_posix()}")
    init_db()
    managed_root = tmp_path / "managed-skills"
    monkeypatch.setattr(skill_service, "_skills_root", lambda: managed_root)
    launched: dict[str, list] = {"calls": []}
    monkeypatch.setattr(
        coordinator,
        "launch",
        lambda run_id, resume=False: launched["calls"].append((run_id, resume)) or True,
    )
    app = FastAPI()
    app.include_router(resources_router)
    app.include_router(capabilities_router)
    app.include_router(runtime_router)
    with TestClient(app) as test_client:
        yield test_client, launched
    Base.metadata.drop_all(bind=database.engine)


def _write_local_skill(root: Path, *, name: str = "Demo Local Skill") -> Path:
    source = root / "source-skill"
    (source / "scripts").mkdir(parents=True)
    (source / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: Imported without executing scripts.\nversion: 1.2.3\n---\n\n# {name}\n\nSafe instructions.\n",
        encoding="utf-8",
    )
    (source / "scripts" / "never_run.py").write_text("raise RuntimeError('must not run')\n", encoding="utf-8")
    return source


def _import_skill(test_client: TestClient, tmp_path: Path) -> dict:
    source = _write_local_skill(tmp_path)
    response = test_client.post("/api/skills/import", json={"source_path": str(source)})
    assert response.status_code == 201, response.text
    return response.json()


def test_tool_catalog_and_fixed_master_advertise_stable_tool_ids(
    client: tuple[TestClient, dict[str, list]],
) -> None:
    test_client, _launched = client
    response = test_client.get("/api/tools")
    assert response.status_code == 200
    tools = response.json()
    by_id = {item["id"]: item for item in tools}
    assert {"bash", "read", "write", "edit", "glob", "grep", "webfetch", "websearch", "task", "todowrite", "question", "skill"}.issubset(by_id)
    assert by_id["bash"]["runtime_tool_id"] == "bash"
    assert by_id["read"]["risk_level"] == "low"
    assert {"availability", "enabled", "is_builtin"}.issubset(by_id["skill"])

    default_agent = test_client.get(f"/api/agents/{DEFAULT_AGENT_ID}")
    assert default_agent.status_code == 200
    assert default_agent.json()["tool_ids"] == sorted(BUILTIN_TOOL_IDS)
    assert default_agent.json()["skill_ids"] == []
    locked = test_client.patch(f"/api/agents/{DEFAULT_AGENT_ID}", json={"tool_ids": ["read"]})
    assert locked.status_code == 409


def test_local_skill_import_copies_files_without_execution_and_lists_metadata(
    client: tuple[TestClient, dict[str, list]], tmp_path: Path
) -> None:
    test_client, _launched = client
    source = _write_local_skill(tmp_path)
    response = test_client.post("/api/skills/import", json={"source_path": str(source)})
    assert response.status_code == 201, response.text
    skill = response.json()
    assert skill["slug"] == "demo-local-skill"
    assert skill["source"] == "local"
    assert skill["version"] == "1.2.3"
    installed_root = Path(skill["root_path"])
    assert installed_root != source
    assert (installed_root / "SKILL.md").read_text(encoding="utf-8").startswith("---")
    assert (installed_root / "scripts" / "never_run.py").exists()

    listed = test_client.get("/api/skills")
    assert listed.status_code == 200
    assert [item["id"] for item in listed.json()] == [skill["id"]]
    duplicate = test_client.post("/api/skills/import", json={"source_path": str(source)})
    assert duplicate.status_code == 409


def test_agent_and_session_capability_relations_persist_through_api(
    client: tuple[TestClient, dict[str, list]], tmp_path: Path
) -> None:
    test_client, _launched = client
    skill = _import_skill(test_client, tmp_path)
    agent_response = test_client.post(
        "/api/agents",
        json={
            "name": "Research helper",
            "tool_ids": ["read", "bash", "read"],
            "skill_ids": [skill["id"]],
        },
    )
    assert agent_response.status_code == 201, agent_response.text
    agent = agent_response.json()
    assert agent["tool_ids"] == ["bash", "read"]
    assert agent["skill_ids"] == [skill["id"]]

    updated_agent = test_client.patch(
        f"/api/agents/{agent['id']}", json={"tool_ids": ["write"], "skill_ids": []}
    )
    assert updated_agent.status_code == 200
    assert updated_agent.json()["tool_ids"] == ["write"]
    assert updated_agent.json()["skill_ids"] == []

    session_response = test_client.post(
        "/api/sessions",
        json={"title": "Permission test", "permission_mode": "ask", "skill_ids": [skill["id"]]},
    )
    assert session_response.status_code == 201, session_response.text
    chat_session = session_response.json()
    assert chat_session["agent_id"] == DEFAULT_AGENT_ID
    assert chat_session["permission_mode"] == "ask"
    assert chat_session["skill_ids"] == [skill["id"]]

    updated_session = test_client.patch(
        f"/api/sessions/{chat_session['id']}", json={"permission_mode": "full", "skill_ids": []}
    )
    assert updated_session.status_code == 200
    assert updated_session.json()["permission_mode"] == "full"
    assert updated_session.json()["skill_ids"] == []


def test_draft_launch_persists_permission_and_skills_and_binds_them_to_idempotency(
    client: tuple[TestClient, dict[str, list]], tmp_path: Path
) -> None:
    test_client, launched = client
    skill = _import_skill(test_client, tmp_path)
    payload = {
        "idempotency_key": "draft-permission-and-skills",
        "title": "Use configured Skill",
        "content": "Please use the configured skill safely.",
        "thinking_level": "high",
        "permission_mode": "ask",
        "skill_ids": [skill["id"]],
    }
    first = test_client.post("/api/drafts/launch", json=payload)
    assert first.status_code == 202, first.text
    body = first.json()
    assert body["session"]["permission_mode"] == "ask"
    assert body["session"]["skill_ids"] == [skill["id"]]
    assert launched["calls"] == [(body["run"]["id"], False)]

    changed_permission = test_client.post(
        "/api/drafts/launch", json={**payload, "permission_mode": "full"}
    )
    assert changed_permission.status_code == 409
    changed_skills = test_client.post(
        "/api/drafts/launch", json={**payload, "skill_ids": []}
    )
    assert changed_skills.status_code == 409
    with database.SessionLocal() as db:
        assert db.query(DraftLaunch).count() == 1
        assert db.query(Session).count() == 1
        assert db.query(ChatMessage).count() == 1
        assert db.query(Run).count() == 1


def test_market_status_does_not_pretend_an_unauthenticated_skills_sh_search_works(
    client: tuple[TestClient, dict[str, list]], monkeypatch: pytest.MonkeyPatch
) -> None:
    test_client, _launched = client
    monkeypatch.delenv("SKILLS_SH_API_TOKEN", raising=False)
    monkeypatch.delenv("PGAGENT_SKILLS_SH_API_TOKEN", raising=False)
    monkeypatch.delenv("VERCEL_OIDC_TOKEN", raising=False)
    status_response = test_client.get("/api/skills/market/status")
    assert status_response.status_code == 200
    assert status_response.json()["available"] is False
    search_response = test_client.post("/api/skills/market/search", json={"query": "react"})
    assert search_response.status_code == 200
    assert search_response.json()["available"] is False
    assert search_response.json()["items"] == []


def test_github_zip_is_previewed_before_confirmed_install(
    client: tuple[TestClient, dict[str, list]], monkeypatch: pytest.MonkeyPatch
) -> None:
    test_client, _launched = client
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        archive.writestr(
            "demo-repo-main/skills/demo/SKILL.md",
            "---\nname: GitHub Demo\ndescription: Preview first.\n---\n# GitHub Demo\n",
        )
        archive.writestr("demo-repo-main/skills/demo/examples/example.txt", "safe example")
    monkeypatch.setattr(skill_service, "_archive_url_for_source", lambda _url: "https://github.com/demo/repo/archive.zip")
    monkeypatch.setattr(skill_service, "_download_github_archive", lambda _url: stream.getvalue())

    preview = test_client.post(
        "/api/skills/market/install",
        json={"source_url": "https://github.com/demo/repo", "confirm": False},
    )
    assert preview.status_code == 200, preview.text
    assert preview.json()["installed"] is False
    assert preview.json()["candidates"] == ["demo-repo-main/skills/demo"]
    assert {item["path"] for item in preview.json()["files"]} == {"SKILL.md", "examples/example.txt"}

    install = test_client.post(
        "/api/skills/market/install",
        json={
            "source_url": "https://github.com/demo/repo",
            "skill_path": "demo-repo-main/skills/demo",
            "confirm": True,
        },
    )
    assert install.status_code == 200, install.text
    assert install.json()["installed"] is True
    assert install.json()["skill"]["source"] == "github"


def test_skill_relations_are_real_database_rows(
    client: tuple[TestClient, dict[str, list]], tmp_path: Path
) -> None:
    test_client, _launched = client
    skill = _import_skill(test_client, tmp_path)
    session = test_client.post(
        "/api/sessions", json={"title": "Rows", "skill_ids": [skill["id"]]}
    ).json()
    with database.SessionLocal() as db:
        persisted_skill = db.get(Skill, skill["id"])
        persisted_session = db.get(Session, session["id"])
        assert persisted_skill is not None
        assert persisted_session is not None
        assert persisted_session.skill_ids == [skill["id"]]
        assert db.scalar(select(Agent).where(Agent.id == DEFAULT_AGENT_ID)) is not None
