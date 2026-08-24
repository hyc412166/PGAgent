from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import system
from app.services import instruction_service


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(system.router)
    return TestClient(app, client=("127.0.0.1", 50000))


def test_select_folder_returns_mocked_native_result(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(system.platform, "system", lambda: "Windows")
    monkeypatch.setattr(
        system,
        "_run_folder_dialog",
        lambda: {"path": r"C:\Users\demo\workspace", "cancelled": False},
    )
    with _client() as client:
        response = client.post("/api/system/select-folder")
    assert response.status_code == 200
    assert response.json() == {"path": r"C:\Users\demo\workspace", "cancelled": False}


def test_select_folder_reports_cancel_and_non_windows(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(system.platform, "system", lambda: "Windows")
    monkeypatch.setattr(system, "_run_folder_dialog", lambda: {"path": None, "cancelled": True})
    with _client() as client:
        cancelled = client.post("/api/system/select-folder")
    assert cancelled.json() == {"path": None, "cancelled": True}

    monkeypatch.setattr(system.platform, "system", lambda: "Linux")
    with _client() as client:
        unsupported = client.post("/api/system/select-folder")
    assert unsupported.status_code == 501


def test_personalization_api_updates_global_agents_md(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    monkeypatch.setattr(instruction_service, "settings", type("Settings", (), {"data_dir": data_dir})())

    with _client() as client:
        initial = client.get("/api/system/personalization")
        saved = client.put(
            "/api/system/personalization",
            json={"custom_instructions": "Always explain cache behavior."},
        )

    assert initial.status_code == 200 and initial.json()["custom_instructions"] == ""
    assert saved.status_code == 200
    assert saved.json()["custom_instructions"] == "Always explain cache behavior."
    assert saved.json()["effective_instructions"] == "Always explain cache behavior."
    assert (data_dir / "AGENTS.md").read_text(encoding="utf-8") == "Always explain cache behavior."


def test_personalization_api_reports_active_override(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "AGENTS.md").write_text("base", encoding="utf-8")
    override = data_dir / "AGENTS.override.md"
    override.write_text("temporary override", encoding="utf-8")
    monkeypatch.setattr(instruction_service, "settings", type("Settings", (), {"data_dir": data_dir})())

    with _client() as client:
        response = client.get("/api/system/personalization")

    assert response.status_code == 200
    assert response.json()["override_active"] is True
    assert response.json()["effective_instructions"] == "temporary override"
    assert response.json()["effective_path"] == str(override.resolve())


def test_personalization_endpoints_reject_remote_client(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    monkeypatch.setattr(instruction_service, "settings", type("Settings", (), {"data_dir": data_dir})())
    app = FastAPI()
    app.include_router(system.router)

    with TestClient(app, client=("203.0.113.20", 50000)) as client:
        read_response = client.get("/api/system/personalization")
        write_response = client.put(
            "/api/system/personalization",
            json={"custom_instructions": "unsafe remote write"},
        )

    assert read_response.status_code == 403
    assert write_response.status_code == 403
    assert not (data_dir / "AGENTS.md").exists()
