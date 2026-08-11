from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import database
from app.api.usage import router
from app.database import Base, UsageRecord, configure_database, init_db


@pytest.fixture()
def client(tmp_path: Path) -> TestClient:
    configure_database(f"sqlite:///{(tmp_path / 'usage.db').as_posix()}")
    init_db()
    app = FastAPI()
    app.include_router(router)
    with TestClient(app) as test_client:
        yield test_client
    Base.metadata.drop_all(bind=database.engine)


def test_empty_usage_aggregates_are_zero(client: TestClient) -> None:
    summary = client.get("/api/usage/summary")
    assert summary.status_code == 200
    assert summary.json() == {
        "total_requests": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_creation_tokens": 0,
        "cache_read_tokens": 0,
        "total_tokens": 0,
        "total_cost_usd": 0.0,
        "cache_hit_rate": 0.0,
    }
    assert client.get("/api/usage/models").json() == []


def test_usage_summary_and_model_groups(client: TestClient) -> None:
    with database.SessionLocal() as db:
        db.add_all([
            UsageRecord(
                model_id="model-a", provider="openrouter", request_count=2,
                input_tokens=100, output_tokens=40, cache_creation_tokens=20,
                cache_read_tokens=80, total_tokens=240, cost_usd=0.12,
            ),
            UsageRecord(
                model_id="model-a", provider="openrouter", request_count=1,
                input_tokens=50, output_tokens=10, cache_creation_tokens=0,
                cache_read_tokens=50, total_tokens=110, cost_usd=0.03,
            ),
            UsageRecord(
                model_id="model-b", provider="deepseek", request_count=1,
                input_tokens=25, output_tokens=5, cache_creation_tokens=0,
                cache_read_tokens=0, total_tokens=30, cost_usd=0.01,
            ),
        ])
        db.commit()

    summary = client.get("/api/usage/summary").json()
    assert summary["total_requests"] == 4
    assert summary["input_tokens"] == 175
    assert summary["output_tokens"] == 55
    assert summary["cache_creation_tokens"] == 20
    assert summary["cache_read_tokens"] == 130
    assert summary["total_tokens"] == 380
    assert summary["total_cost_usd"] == pytest.approx(0.16)
    assert summary["cache_hit_rate"] == pytest.approx(130 / 325)

    models = client.get("/api/usage/models").json()
    assert models[0]["provider"] == "openrouter"
    assert models[0]["model_id"] == "model-a"
    assert models[0]["requests"] == 3
    assert models[0]["tokens"] == 350
    assert models[0]["total_cost_usd"] == pytest.approx(0.15)
    assert models[0]["avg_cost_usd"] == pytest.approx(0.05)
