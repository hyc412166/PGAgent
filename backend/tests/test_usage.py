"""验证用量统计 API 的总计、分组、时间范围和孤立记录过滤。

测试通过 fixture 或辅助函数准备隔离环境，再调用真实服务、路由或运行时，并检查返回值、持久化状态与可观察副作用。
变量约定：tmp_path/monkeypatch 提供隔离环境，client/store/runtime 驱动被测链路，各类 *_id 串联持久化实体，payload 表示输入，response/result 表示实际输出，expected 表示期望值。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.persistence import database
from src.api.usage import router
from src.persistence.database import Base, Run, Session as ChatSession, UsageRecord, Workspace, configure_database, init_db


@pytest.fixture()
# 测试夹具：client 创建本组用例共享的隔离资源，并在测试结束后恢复数据库、配置或进程状态。
def client(tmp_path: Path) -> TestClient:
    # 临时数据库保存运行用量明细；test_client 通过统计路由验证汇总、分组和范围过滤。
    configure_database(f"sqlite:///{(tmp_path / 'usage.db').as_posix()}")
    init_db()
    app = FastAPI()
    app.include_router(router)
    with TestClient(app) as test_client:
        yield test_client
    Base.metadata.drop_all(bind=database.engine)


# 测试场景：验证该正常业务场景从输入准备到结果断言的完整链路；函数名 test_empty_usage_aggregates_are_zero 精确标识本用例的具体条件。
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
    assert client.get("/api/usage/runs/missing-run").json() is None


# 测试场景：验证该正常业务场景从输入准备到结果断言的完整链路；函数名 test_usage_summary_and_model_groups 精确标识本用例的具体条件。
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
    assert models[0]["cache_hit_rate"] == pytest.approx(130 / 300)


# 测试场景：验证该正常业务场景从输入准备到结果断言的完整链路；函数名 test_usage_range_filters_apply_to_summary_and_model_groups 精确标识本用例的具体条件。
def test_usage_range_filters_apply_to_summary_and_model_groups(client: TestClient) -> None:
    earlier = datetime(2026, 1, 10, tzinfo=timezone.utc)
    later = earlier + timedelta(days=1)
    with database.SessionLocal() as db:
        db.add_all([
            UsageRecord(
                model_id="model-a", provider="openrouter", request_count=2,
                input_tokens=30, output_tokens=10, cache_creation_tokens=0,
                cache_read_tokens=10, total_tokens=50, cost_usd=0.02, created_at=earlier,
            ),
            UsageRecord(
                model_id="model-b", provider="deepseek", request_count=1,
                input_tokens=10, output_tokens=5, cache_creation_tokens=5,
                cache_read_tokens=0, total_tokens=20, cost_usd=0.03, created_at=later,
            ),
        ])
        db.commit()

    response = client.get("/api/usage/summary", params={"start_at": later.isoformat()})
    assert response.status_code == 200
    assert response.json()["total_requests"] == 1
    assert response.json()["total_tokens"] == 20

    models = client.get("/api/usage/models", params={"end_at": earlier.isoformat()})
    assert models.status_code == 200
    assert models.json() == [{
        "provider": "openrouter",
        "model_id": "model-a",
        "requests": 2,
        "tokens": 50,
        "total_cost_usd": pytest.approx(0.02),
        "avg_cost_usd": pytest.approx(0.01),
        "cache_hit_rate": pytest.approx(0.25),
    }]


# 测试场景：验证该正常业务场景从输入准备到结果断言的完整链路；函数名 test_usage_session_and_workspace_groups 精确标识本用例的具体条件。
def test_usage_session_and_workspace_groups(client: TestClient) -> None:
    with database.SessionLocal() as db:
        workspace = Workspace(id="workspace-a", name="Alpha", root_path="C:/work/alpha")
        session = ChatSession(id="session-a", title="Build Alpha", workspace_id=workspace.id)
        run = Run(id="run-a", session_id=session.id, workspace_id=workspace.id)
        db.add(workspace)
        db.flush()
        db.add(session)
        db.flush()
        db.add(run)
        db.flush()
        fallback_run = Run(id="run-b", session_id=session.id, workspace_id=workspace.id)
        db.add(fallback_run)
        db.flush()
        db.add_all([
            UsageRecord(
                run_id=run.id, session_id=session.id, model_id="model-a", provider="openrouter",
                request_count=2, input_tokens=60, output_tokens=20, cache_creation_tokens=20,
                cache_read_tokens=20, total_tokens=120, cost_usd=0.06,
            ),
            UsageRecord(
                run_id=fallback_run.id, model_id="model-b", provider="deepseek",
                request_count=1, input_tokens=20, output_tokens=10, cache_creation_tokens=0,
                cache_read_tokens=20, total_tokens=50, cost_usd=0.02,
            ),
        ])
        db.commit()

    sessions = client.get("/api/usage/sessions")
    assert sessions.status_code == 200
    assert sessions.json() == [{
        "session_id": "session-a",
        "title": "Build Alpha",
        "requests": 3,
        "tokens": 170,
        "total_cost_usd": pytest.approx(0.08),
        "avg_cost_usd": pytest.approx(0.08 / 3),
        "cache_hit_rate": pytest.approx(40 / 140),
    }]

    workspaces = client.get("/api/usage/workspaces")
    assert workspaces.status_code == 200
    assert workspaces.json() == [{
        "workspace_id": "workspace-a",
        "name": "Alpha",
        "path": "C:/work/alpha",
        "requests": 3,
        "tokens": 170,
        "total_cost_usd": pytest.approx(0.08),
        "avg_cost_usd": pytest.approx(0.08 / 3),
        "cache_hit_rate": pytest.approx(40 / 140),
    }]

    run_usage = client.get("/api/usage/runs/run-a")
    assert run_usage.status_code == 200
    assert run_usage.json() == {
        "run_id": "run-a",
        "provider": "openrouter",
        "model_id": "model-a",
        "requests": 2,
        "input_tokens": 60,
        "output_tokens": 20,
        "cache_creation_tokens": 20,
        "cache_read_tokens": 20,
        "total_tokens": 120,
        "total_cost_usd": pytest.approx(0.06),
        "cache_hit_rate": pytest.approx(20 / 100),
    }


# 测试场景：验证该正常业务场景从输入准备到结果断言的完整链路；函数名 test_usage_group_ranges_exclude_orphan_records 精确标识本用例的具体条件。
def test_usage_group_ranges_exclude_orphan_records(client: TestClient) -> None:
    earlier = datetime(2026, 1, 10, tzinfo=timezone.utc)
    later = earlier + timedelta(days=1)
    with database.SessionLocal() as db:
        db.add_all([
            UsageRecord(
                model_id="model-a", provider="openrouter", request_count=1,
                input_tokens=4, output_tokens=1, cache_creation_tokens=0,
                cache_read_tokens=0, total_tokens=5, cost_usd=0.01, created_at=earlier,
            ),
            UsageRecord(
                model_id="model-a", provider="openrouter", request_count=1,
                input_tokens=4, output_tokens=1, cache_creation_tokens=0,
                cache_read_tokens=4, total_tokens=9, cost_usd=0.02, created_at=later,
            ),
        ])
        db.commit()

    sessions = client.get("/api/usage/sessions", params={"start_at": later.isoformat()})
    assert sessions.status_code == 200
    assert sessions.json() == []

    workspaces = client.get("/api/usage/workspaces", params={"start_at": later.isoformat()})
    assert workspaces.status_code == 200
    assert workspaces.json() == []


# 测试场景：验证该正常业务场景从输入准备到结果断言的完整链路；函数名 test_orphan_placeholder_usage_is_excluded_from_every_user_facing_aggregate 精确标识本用例的具体条件。
def test_orphan_placeholder_usage_is_excluded_from_every_user_facing_aggregate(client: TestClient) -> None:
    with database.SessionLocal() as db:
        workspace = Workspace(id="workspace-a", name="Alpha", root_path="C:/work/alpha")
        session = ChatSession(id="session-a", title="Build Alpha", workspace_id=workspace.id)
        run = Run(id="run-a", session_id=session.id, workspace_id=workspace.id)
        db.add(workspace)
        db.flush()
        db.add(session)
        db.flush()
        db.add(run)
        db.flush()
        db.add_all([
            UsageRecord(
                run_id=run.id, session_id=session.id, model_id="real-model", provider="real-provider", request_count=1,
                input_tokens=10, output_tokens=5, total_tokens=15, cost_usd=0.01,
            ),
            UsageRecord(
                model_id="m", provider="p", request_count=1,
                input_tokens=0, output_tokens=0, total_tokens=0, cost_usd=0.0,
            ),
        ])
        db.commit()

    summary = client.get("/api/usage/summary")
    assert summary.status_code == 200
    assert summary.json()["total_requests"] == 1
    assert summary.json()["total_tokens"] == 15
    assert [item["model_id"] for item in client.get("/api/usage/models").json()] == ["real-model"]
