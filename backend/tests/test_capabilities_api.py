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
    assert by_id["write"]["risk_level"] == "adaptive"
    assert by_id["write"]["requires_approval"] is False
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
    browse_response = test_client.get("/api/skills/market/browse?view=trending")
    assert browse_response.status_code == 200
    assert browse_response.json()["available"] is False
    assert browse_response.json()["items"] == []


def test_market_browse_normalizes_leaderboard_views_without_exposing_file_contents(
    client: tuple[TestClient, dict[str, list]], monkeypatch: pytest.MonkeyPatch
) -> None:
    test_client, _launched = client
    monkeypatch.setenv("PGAGENT_SKILLS_SH_API_TOKEN", "test-market-token")
    calls: list[tuple[str, dict | None]] = []

    def fake_skills_sh(path: str, *, params: dict | None = None) -> dict:
        calls.append((path, params))
        return {
            "data": [
                {
                    "id": "demo/repo/secure-skill",
                    "slug": "secure-skill",
                    "name": "Secure Skill",
                    "source": "demo/repo",
                    "installs": 321,
                    "change": 29,
                    "installsYesterday": 2,
                    "installUrl": "https://github.com/demo/repo",
                    "url": "https://skills.sh/demo/repo/secure-skill",
                    "isDuplicate": True,
                    "files": [{"path": "SKILL.md", "contents": "must never appear here"}],
                }
            ],
            "pagination": {"page": 2, "perPage": 7, "total": 99, "hasMore": True},
        }

    monkeypatch.setattr(skill_service, "_skills_sh_json", fake_skills_sh)
    response = test_client.get("/api/skills/market/browse?view=hot&page=2&per_page=7")

    assert response.status_code == 200, response.text
    body = response.json()
    assert calls == [("/api/v1/skills", {"view": "hot", "page": 2, "per_page": 7})]
    assert body["view"] == "hot"
    assert body["page"] == 2
    assert body["has_more"] is True
    assert body["total"] == 99
    assert body["items"] == [{
        "id": "demo/repo/secure-skill",
        "slug": "secure-skill",
        "name": "Secure Skill",
        "source": "demo/repo",
        "source_url": "https://github.com/demo/repo",
        "market_url": "https://skills.sh/demo/repo/secure-skill",
        "installs": 321,
        "change": 29,
        "installs_yesterday": 2,
        "is_official": False,
        "official_owner": None,
        "is_duplicate": True,
    }]
    assert "must never appear here" not in response.text


def test_market_browse_flattens_curated_owners_and_marks_official_skills(
    client: tuple[TestClient, dict[str, list]], monkeypatch: pytest.MonkeyPatch
) -> None:
    test_client, _launched = client
    monkeypatch.setenv("PGAGENT_SKILLS_SH_API_TOKEN", "test-market-token")
    monkeypatch.setattr(
        skill_service,
        "_skills_sh_json",
        lambda path, *, params=None: {
            "data": [
                {
                    "owner": "official-maker",
                    "skills": [
                        {"id": "official-maker/docs/product", "slug": "product", "name": "Product", "source": "official-maker/docs", "installs": 10},
                        {"id": "official-maker/docs/second", "slug": "second", "name": "Second", "source": "official-maker/docs", "installs": 9},
                    ],
                }
            ]
        },
    )

    response = test_client.get("/api/skills/market/browse?view=curated&page=0&per_page=1")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["view"] == "curated"
    assert body["total"] == 2
    assert body["has_more"] is True
    assert body["items"][0]["is_official"] is True
    assert body["items"][0]["official_owner"] == "official-maker"


def test_market_leaderboards_cache_five_topic_rankings_and_keep_sensitive_fields_private(
    client: tuple[TestClient, dict[str, list]], monkeypatch: pytest.MonkeyPatch
) -> None:
    test_client, _launched = client
    monkeypatch.setenv("PGAGENT_SKILLS_SH_API_TOKEN", "test-market-token")
    monkeypatch.setattr(skill_service, "_market_leaderboard_cache", None)
    calls: list[dict[str, object]] = []
    installs = [21, 90, 8, 72, 55, 31, 49, 7]

    def fake_skills_sh(path: str, *, params: dict | None = None) -> dict:
        assert path == "/api/v1/skills/search"
        assert params is not None
        calls.append(dict(params))
        category = next(item for item in skill_service.MARKET_LEADERBOARD_CATEGORIES if item.query == params["q"])
        return {
            "data": [
                {
                    "id": f"demo/{category.id}/skill-{index}",
                    "slug": f"skill-{index}",
                    "name": f"{category.id} skill {index}",
                    "source": "demo/skills",
                    "installs": install_count,
                    "files": [{"path": "SKILL.md", "contents": "private skill body"}],
                    "token": "private-token",
                }
                for index, install_count in enumerate(installs)
            ]
        }

    monkeypatch.setattr(skill_service, "_skills_sh_json", fake_skills_sh)
    first = test_client.get("/api/skills/market/leaderboards")

    assert first.status_code == 200, first.text
    body = first.json()
    assert body["available"] is True
    assert body["cached"] is False
    assert body["ttl_seconds"] == skill_service.MARKET_LEADERBOARD_TTL_SECONDS
    assert [category["id"] for category in body["categories"]] == [
        "frontend",
        "programming",
        "research",
        "writing",
        "data-ai",
    ]
    assert len(calls) == 5
    assert all(call["limit"] == skill_service.MARKET_LEADERBOARD_SEARCH_LIMIT for call in calls)
    assert all(len(category["items"]) == 6 for category in body["categories"])
    assert all(category["items"][0]["installs"] == 90 for category in body["categories"])
    assert "private skill body" not in first.text
    assert "private-token" not in first.text

    cached = test_client.get("/api/skills/market/leaderboards")
    assert cached.status_code == 200, cached.text
    assert cached.json()["cached"] is True
    assert len(calls) == 5


def test_market_leaderboards_expire_after_ttl_and_support_manual_refresh(
    client: tuple[TestClient, dict[str, list]], monkeypatch: pytest.MonkeyPatch
) -> None:
    test_client, _launched = client
    monkeypatch.setenv("PGAGENT_SKILLS_SH_API_TOKEN", "test-market-token")
    monkeypatch.setattr(skill_service, "_market_leaderboard_cache", None)
    clock = {"value": 100.0}
    monkeypatch.setattr(skill_service.time, "monotonic", lambda: clock["value"])
    calls: list[dict[str, object]] = []

    def fake_skills_sh(path: str, *, params: dict | None = None) -> dict:
        assert path == "/api/v1/skills/search"
        assert params is not None
        calls.append(dict(params))
        return {
            "data": [
                {
                    "id": f"demo/skills/{len(calls)}",
                    "name": "Cached Skill",
                    "installs": 1,
                }
            ]
        }

    monkeypatch.setattr(skill_service, "_skills_sh_json", fake_skills_sh)
    initial = test_client.get("/api/skills/market/leaderboards")
    assert initial.status_code == 200, initial.text
    assert initial.json()["cached"] is False
    assert len(calls) == 5

    clock["value"] += skill_service.MARKET_LEADERBOARD_TTL_SECONDS - 1
    still_cached = test_client.get("/api/skills/market/leaderboards")
    assert still_cached.status_code == 200, still_cached.text
    assert still_cached.json()["cached"] is True
    assert len(calls) == 5

    clock["value"] += 2
    expired = test_client.get("/api/skills/market/leaderboards")
    assert expired.status_code == 200, expired.text
    assert expired.json()["cached"] is False
    assert len(calls) == 10

    query_refreshed = test_client.get("/api/skills/market/leaderboards?refresh=true")
    assert query_refreshed.status_code == 200, query_refreshed.text
    assert query_refreshed.json()["cached"] is False
    assert len(calls) == 15

    refreshed = test_client.post("/api/skills/market/leaderboards/refresh")
    assert refreshed.status_code == 200, refreshed.text
    assert refreshed.json()["cached"] is False
    assert len(calls) == 20


def test_market_leaderboards_are_explicitly_unavailable_without_a_market_token(
    client: tuple[TestClient, dict[str, list]], monkeypatch: pytest.MonkeyPatch
) -> None:
    test_client, _launched = client
    monkeypatch.delenv("SKILLS_SH_API_TOKEN", raising=False)
    monkeypatch.delenv("PGAGENT_SKILLS_SH_API_TOKEN", raising=False)
    monkeypatch.delenv("VERCEL_OIDC_TOKEN", raising=False)
    monkeypatch.setattr(skill_service, "_market_leaderboard_cache", None)
    upstream_called = False

    def fake_skills_sh(path: str, *, params: dict | None = None) -> dict:
        nonlocal upstream_called
        upstream_called = True
        return {"data": []}

    monkeypatch.setattr(skill_service, "_skills_sh_json", fake_skills_sh)
    response = test_client.get("/api/skills/market/leaderboards")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["available"] is False
    assert body["categories"] == []
    assert body["cached"] is False
    assert upstream_called is False


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
