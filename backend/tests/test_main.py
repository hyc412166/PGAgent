from __future__ import annotations

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.main import app, frontend


def test_unknown_api_path_is_not_served_as_spa() -> None:
    with pytest.raises(HTTPException) as captured:
        frontend("api/does-not-exist")
    assert captured.value.status_code == 404


def test_trusted_host_rejects_dns_rebinding_host() -> None:
    client = TestClient(app)
    try:
        response = client.get("/api/health", headers={"host": "attacker.example:8765"})
    finally:
        client.close()
    assert response.status_code == 400


@pytest.mark.parametrize("host", ["testserver", "localhost:8765", "127.0.0.1:8765"])
def test_trusted_host_allows_local_and_test_hosts(host: str) -> None:
    client = TestClient(app)
    try:
        response = client.get("/api/health", headers={"host": host})
    finally:
        client.close()
    assert response.status_code == 200


@pytest.mark.parametrize(
    "origin",
    [
        "http://attacker.example",
        "https://localhost.attacker.example",
        "null",
        "http://localhost:not-a-port",
    ],
)
def test_unsafe_browser_request_rejects_non_local_origin(origin: str) -> None:
    client = TestClient(app)
    try:
        response = client.post("/api/health", headers={"origin": origin})
    finally:
        client.close()
    assert response.status_code == 403


@pytest.mark.parametrize("origin", ["http://localhost:5173", "http://127.0.0.1:5173"])
def test_unsafe_browser_request_allows_local_origin(origin: str) -> None:
    client = TestClient(app)
    try:
        response = client.post("/api/health", headers={"origin": origin})
    finally:
        client.close()
    assert response.status_code == 405


def test_unsafe_cli_request_without_origin_is_allowed_through() -> None:
    client = TestClient(app)
    try:
        response = client.post("/api/health")
    finally:
        client.close()
    assert response.status_code == 405
