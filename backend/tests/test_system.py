from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import system


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
