"""验证 FastAPI 应用入口、健康检查、静态资源与异常处理等基础服务行为。

测试通过 fixture 或辅助函数准备隔离环境，再调用真实服务、路由或运行时，并检查返回值、持久化状态与可观察副作用。
变量约定：tmp_path/monkeypatch 提供隔离环境，client/store/runtime 驱动被测链路，各类 *_id 串联持久化实体，payload 表示输入，response/result 表示实际输出，expected 表示期望值。
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from src.main import app, frontend


# 测试场景：验证接口或资源生命周期操作会返回正确结果并同步持久化状态；函数名 test_unknown_api_path_is_not_served_as_spa 精确标识本用例的具体条件。
def test_unknown_api_path_is_not_served_as_spa() -> None:
    with pytest.raises(HTTPException) as captured:
        frontend("api/does-not-exist")
    assert captured.value.status_code == 404


# 测试场景：验证非法、越界或不满足前置条件的操作会被明确拒绝，且不会产生错误状态；函数名 test_trusted_host_rejects_dns_rebinding_host 精确标识本用例的具体条件。
def test_trusted_host_rejects_dns_rebinding_host() -> None:
    client = TestClient(app)
    try:
        response = client.get("/api/health", headers={"host": "attacker.example:8765"})
    finally:
        client.close()
    assert response.status_code == 400


@pytest.mark.parametrize("host", ["testserver", "localhost:8765", "127.0.0.1:8765"])
# 测试场景：验证该正常业务场景从输入准备到结果断言的完整链路；函数名 test_trusted_host_allows_local_and_test_hosts 精确标识本用例的具体条件。
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
# 测试场景：验证非法、越界或不满足前置条件的操作会被明确拒绝，且不会产生错误状态；函数名 test_unsafe_browser_request_rejects_non_local_origin 精确标识本用例的具体条件。
def test_unsafe_browser_request_rejects_non_local_origin(origin: str) -> None:
    client = TestClient(app)
    try:
        response = client.post("/api/health", headers={"origin": origin})
    finally:
        client.close()
    assert response.status_code == 403


@pytest.mark.parametrize("origin", ["http://localhost:5173", "http://127.0.0.1:5173"])
# 测试场景：验证该正常业务场景从输入准备到结果断言的完整链路；函数名 test_unsafe_browser_request_allows_local_origin 精确标识本用例的具体条件。
def test_unsafe_browser_request_allows_local_origin(origin: str) -> None:
    client = TestClient(app)
    try:
        response = client.post("/api/health", headers={"origin": origin})
    finally:
        client.close()
    assert response.status_code == 405


# 测试场景：验证该正常业务场景从输入准备到结果断言的完整链路；函数名 test_unsafe_cli_request_without_origin_is_allowed_through 精确标识本用例的具体条件。
def test_unsafe_cli_request_without_origin_is_allowed_through() -> None:
    client = TestClient(app)
    try:
        response = client.post("/api/health")
    finally:
        client.close()
    assert response.status_code == 405
