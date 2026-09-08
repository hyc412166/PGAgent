"""验证 MCP 服务器配置 API 的增删改查、密钥处理和运行时同步。

测试通过 fixture 或辅助函数准备隔离环境，再调用真实服务、路由或运行时，并检查返回值、持久化状态与可观察副作用。
变量约定：tmp_path/monkeypatch 提供隔离环境，client/store/runtime 驱动被测链路，各类 *_id 串联持久化实体，payload 表示输入，response/result 表示实际输出，expected 表示期望值。
"""

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
# 测试夹具：client 创建本组用例共享的隔离资源，并在测试结束后恢复数据库、配置或进程状态。
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[TestClient, Path]:
    # config_file 是隔离的 MCP 配置文件；test_client 通过 API 修改它并观察运行时配置同步。
    config_file = tmp_path / "mcp.json"
    monkeypatch.setattr(settings, "mcp_config_path", str(config_file))
    configure_database(f"sqlite:///{(tmp_path / 'mcp-api.db').as_posix()}")
    init_db()
    app = FastAPI()
    app.include_router(router)
    with TestClient(app) as test_client:
        yield test_client, config_file
    Base.metadata.drop_all(bind=database.engine)


# 测试场景：验证状态能够可靠持久化、重放或在重启后恢复，并保持记录之间的关联；函数名 test_stdio_server_crud_persists_workbench_fields 精确标识本用例的具体条件。
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


# 测试场景：验证非法、越界或不满足前置条件的操作会被明确拒绝，且不会产生错误状态；函数名 test_duplicate_name_and_live_runtime_changes_are_rejected 精确标识本用例的具体条件。
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


# 测试场景：验证接口或资源生命周期操作会返回正确结果并同步持久化状态；函数名 test_remote_server_can_be_listed_toggled_and_deleted 精确标识本用例的具体条件。
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


# 测试场景：验证该正常业务场景从输入准备到结果断言的完整链路；函数名 test_server_name_with_slash_round_trips_through_crud 精确标识本用例的具体条件。
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
