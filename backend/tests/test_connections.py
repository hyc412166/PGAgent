from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.persistence import database
from src.model import credentials as secrets
from src.api import connections
from src.api.connections import router as connections_router
from src.persistence.database import Base, configure_database, init_db


class FakeHttpClient:
    def __init__(self, response: httpx.Response, protocol_response: httpx.Response):
        self.response = response
        self.protocol_response = protocol_response

    def __enter__(self):  # type: ignore[no-untyped-def]
        return self

    def __exit__(self, *_args):  # type: ignore[no-untyped-def]
        return None

    def get(self, url: str, headers: dict[str, str]) -> httpx.Response:
        assert url.endswith("/models")
        assert headers["Authorization"].startswith("Bearer ")
        return self.response

    def post(self, url: str, headers: dict[str, str], json: dict) -> httpx.Response:  # type: ignore[no-untyped-def]
        assert url.endswith(("/responses", "/chat/completions"))
        assert headers["Authorization"].startswith("Bearer ")
        if url.endswith("/responses"):
            assert json["store"] is False
            assert json["max_output_tokens"] == 16
        return self.protocol_response

    class _Stream:
        def __init__(self, response: httpx.Response):
            self.response = response

        def __enter__(self):  # type: ignore[no-untyped-def]
            return self.response

        def __exit__(self, *_args):  # type: ignore[no-untyped-def]
            return None

    def stream(self, method: str, url: str, headers: dict[str, str], json: dict) -> "FakeHttpClient._Stream":  # type: ignore[no-untyped-def]
        assert method == "POST"
        assert json["stream"] is True
        return self._Stream(self.post(url, headers=headers, json=json))


@pytest.fixture()
def secret_backend(monkeypatch: pytest.MonkeyPatch) -> dict[tuple[str, str], str]:
    stored: dict[tuple[str, str], str] = {}
    monkeypatch.setattr(
        secrets.keyring, "set_password", lambda service, ref, value: stored.__setitem__((service, ref), value)
    )
    monkeypatch.setattr(secrets.keyring, "get_password", lambda service, ref: stored.get((service, ref)))
    monkeypatch.setattr(secrets.keyring, "delete_password", lambda service, ref: stored.pop((service, ref)))
    return stored


@pytest.fixture()
def client(tmp_path: Path, secret_backend: dict[tuple[str, str], str]) -> TestClient:
    configure_database(f"sqlite:///{(tmp_path / 'connections.db').as_posix()}")
    init_db()
    app = FastAPI()
    app.include_router(connections_router)
    with TestClient(app) as test_client:
        yield test_client
    Base.metadata.drop_all(bind=database.engine)


def mock_response(
    monkeypatch: pytest.MonkeyPatch,
    status: int,
    payload=None,  # type: ignore[no-untyped-def]
    *,
    protocol_status: int = 200,
) -> None:
    request = httpx.Request("GET", "https://provider.test/v1/models")
    response = httpx.Response(status, json=payload, request=request)
    protocol_response = httpx.Response(
        protocol_status,
        json={"id": "response-test"},
        request=httpx.Request("POST", "https://provider.test/v1/responses"),
    )
    monkeypatch.setattr(
        connections.httpx,
        "Client",
        lambda **_kwargs: FakeHttpClient(response, protocol_response),
    )


def test_create_discovers_models_and_keeps_key_out_of_database_and_response(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    secret_backend: dict[tuple[str, str], str],
) -> None:
    mock_response(monkeypatch, 200, {"data": [{"id": "model-b"}, {"id": "model-a"}]})
    response = client.post(
        "/api/connections",
        json={"name": "Relay", "base_url": "https://provider.test/v1/", "api_key": "secret-key"},
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["discovered_models"] == ["model-a", "model-b"]
    assert body["default_model"] == "model-a"
    assert body["api_protocol"] == "responses"
    assert body["thinking_level"] == "auto"
    assert "api_key" not in body
    assert "secret_ref" not in body
    assert list(secret_backend.values()) == ["secret-key"]

    test_response = client.post(f"/api/connections/{body['id']}/test")
    assert test_response.status_code == 200
    assert test_response.json()["category"] == "ok"


def test_manual_models_allow_missing_models_endpoint(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    mock_response(monkeypatch, 404, {"error": "not found"})
    response = client.post(
        "/api/connections",
        json={
            "name": "Manual relay",
            "base_url": "https://relay.test/v1",
            "api_key": "key",
            "manual_models": ["custom-model"],
        },
    )
    assert response.status_code == 201
    assert response.json()["status"] == "manual"
    assert response.json()["manual_models"] == ["custom-model"]


@pytest.mark.parametrize(
    ("http_status", "category", "retryable"),
    [(401, "invalid_credentials", False), (403, "invalid_credentials", False), (429, "rate_limited", True), (503, "provider_error", True)],
)
def test_connection_errors_are_classified(
    monkeypatch: pytest.MonkeyPatch,
    http_status: int,
    category: str,
    retryable: bool,
) -> None:
    mock_response(monkeypatch, http_status, {"error": "failure"})
    result = connections.discover_models("https://provider.test/v1", "bad-key")
    assert result.success is False
    assert result.category == category
    assert result.retryable is retryable
    assert result.http_status == http_status


def test_create_rejects_discovery_failure_without_manual_models(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, secret_backend: dict[tuple[str, str], str]
) -> None:
    mock_response(monkeypatch, 401, {"error": "unauthorized"})
    response = client.post(
        "/api/connections",
        json={"name": "Bad", "base_url": "https://provider.test/v1", "api_key": "wrong"},
    )
    assert response.status_code == 400
    assert response.json()["detail"]["category"] == "invalid_credentials"
    assert secret_backend == {}


def test_manual_models_do_not_hide_invalid_credentials(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, secret_backend: dict[tuple[str, str], str]
) -> None:
    mock_response(monkeypatch, 403, {"error": "forbidden"})
    response = client.post(
        "/api/connections",
        json={
            "name": "Still bad",
            "base_url": "https://provider.test/v1",
            "api_key": "wrong",
            "manual_models": ["manual-id"],
        },
    )
    assert response.status_code == 400
    assert response.json()["detail"]["category"] == "invalid_credentials"
    assert secret_backend == {}


def test_connection_thinking_level_can_be_updated(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    mock_response(monkeypatch, 200, {"data": [{"id": "model-a"}]})
    created = client.post(
        "/api/connections",
        json={
            "name": "Thinking",
            "base_url": "https://provider.test/v1",
            "api_key": "key",
            "thinking_level": "medium",
        },
    ).json()
    assert created["thinking_level"] == "medium"
    updated = client.patch(
        f"/api/connections/{created['id']}", json={"thinking_level": "high"}
    )
    assert updated.status_code == 200
    assert updated.json()["thinking_level"] == "high"


def test_secret_headers_cannot_be_persisted(client: TestClient) -> None:
    response = client.post(
        "/api/connections",
        json={
            "name": "Unsafe",
            "base_url": "https://provider.test/v1",
            "api_key": "key",
            "manual_models": ["m"],
            "custom_headers": {"X-API-Key": "another-secret"},
        },
    )
    assert response.status_code == 422
