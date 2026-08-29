from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.api.mcp import router
from src.config import settings
from src.persistence import database
from src.persistence.database import Base, Run, Session, configure_database, init_db


@pytest.fixture()
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[TestClient, Path]:
    config_file = tmp_path / "mcp.json"
    monkeypatch.setattr(settings, "mcp_config_path", str(config_file))
    configure_database(f"sqlite:///{(tmp_path / 'mcp-api.db').as_posix()}")
    init_db()
    app = FastAPI()
    app.include_router(router)
    with TestClient(app) as test_client:
        yield test_client, config_file
    Base.metadata.drop_all(bind=database.engine)


def test_stdio_server_crud_persists_workbench_fields(client: tuple[TestClient, Path]) -> None:
    test_client, config_file = client

    created = test_client.post("/api/mcp/servers", json={
        "name": "docs",
        "command": "npx",
        "args": ["-y", "@example/docs-mcp"],
        "enabled": True,
    })
    assert created.status_code == 201
    assert created.json() == {
        "name": "docs",
        "command": "npx",
        "args": ["-y", "@example/docs-mcp"],
        "enabled": True,
        "transport": "stdio",
        "runtime_status": "inactive",
        "tool_count": 0,
    }
    with database.SessionLocal() as db:
        chat_session = Session(mcp_server_names=["docs"])
        db.add(chat_session)
        db.commit()
        session_id = chat_session.id

    updated = test_client.put("/api/mcp/servers/docs", json={
        "name": "docs-local",
        "command": "uvx",
        "args": ["docs-mcp"],
        "enabled": True,
    })
    assert updated.status_code == 200
    assert updated.json()["name"] == "docs-local"
    with database.SessionLocal() as db:
        assert db.get(Session, session_id).mcp_server_names == ["docs-local"]

    disabled = test_client.patch("/api/mcp/servers/docs-local", json={"enabled": False})
    assert disabled.status_code == 200
    assert disabled.json()["enabled"] is False
    with database.SessionLocal() as db:
        assert db.get(Session, session_id).mcp_server_names == []
    assert test_client.get("/api/mcp/servers").json()[0]["args"] == ["docs-mcp"]

    persisted = json.loads(config_file.read_text(encoding="utf-8"))
    assert persisted["mcpServers"]["docs-local"]["command"] == "uvx"
    assert persisted["mcpServers"]["docs-local"]["enabled"] is False

    deleted = test_client.delete("/api/mcp/servers/docs-local")
    assert deleted.status_code == 204
    assert test_client.get("/api/mcp/servers").json() == []


def test_duplicate_name_and_live_runtime_changes_are_rejected(
    client: tuple[TestClient, Path],
) -> None:
    test_client, _ = client
    payload = {"name": "filesystem", "command": "npx", "args": [], "enabled": True}
    assert test_client.post("/api/mcp/servers", json=payload).status_code == 201
    assert test_client.post("/api/mcp/servers", json=payload).status_code == 409

    with database.SessionLocal() as db:
        db.add(Run(status="acting"))
        db.commit()
    response = test_client.patch("/api/mcp/servers/filesystem", json={"enabled": False})
    assert response.status_code == 409
    assert "运行尚未结束" in response.json()["detail"]


def test_remote_server_can_be_listed_toggled_and_deleted(
    client: tuple[TestClient, Path],
) -> None:
    test_client, config_file = client
    config_file.write_text(json.dumps({"mcpServers": {
        "remote": {"url": "${REMOTE_MCP_URL}", "enabled": True}
    }}), encoding="utf-8")

    listed = test_client.get("/api/mcp/servers")
    assert listed.status_code == 200
    assert listed.json()[0]["transport"] == "streamable_http"
    assert listed.json()[0]["command"] == ""
    assert test_client.patch("/api/mcp/servers/remote", json={"enabled": False}).status_code == 200
    assert test_client.delete("/api/mcp/servers/remote").status_code == 204


def test_server_name_with_slash_round_trips_through_crud(client: tuple[TestClient, Path]) -> None:
    test_client, _ = client
    created = test_client.post("/api/mcp/servers", json={
        "name": "team/docs",
        "command": "npx",
        "args": [""],
        "enabled": True,
    })
    assert created.status_code == 201
    toggled = test_client.patch("/api/mcp/servers/team%2Fdocs", json={"enabled": False})
    assert toggled.status_code == 200
    assert toggled.json()["args"] == [""]
    assert test_client.delete("/api/mcp/servers/team%2Fdocs").status_code == 204
