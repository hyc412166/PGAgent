"""验证模型连接的创建、模型发现、凭据保密、错误分类和推理等级更新。

测试通过 fixture 或辅助函数准备隔离环境，再调用真实服务、路由或运行时，并检查返回值、持久化状态与可观察副作用。
变量约定：tmp_path/monkeypatch 提供隔离环境，client/store/runtime 驱动被测链路，各类 *_id 串联持久化实体，payload 表示输入，response/result 表示实际输出，expected 表示期望值。
"""

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


# 测试替身类：FakeHttpClient 模拟外部依赖的响应与调用记录，使连接或协议测试无需访问真实服务。
class FakeHttpClient:
    # 辅助方法：__init__ 实现测试替身在此调用阶段需要的最小行为。
    def __init__(self, response: httpx.Response, protocol_response: httpx.Response):
        self.response = response
        self.protocol_response = protocol_response

    # 辅助方法：__enter__ 实现测试替身在此调用阶段需要的最小行为。
    def __enter__(self):  # type: ignore[no-untyped-def]
        return self

    # 辅助方法：__exit__ 实现测试替身在此调用阶段需要的最小行为。
    def __exit__(self, *_args):  # type: ignore[no-untyped-def]
        return None

    # 辅助方法：get 实现测试替身在此调用阶段需要的最小行为。
    def get(self, url: str, headers: dict[str, str]) -> httpx.Response:
        assert url.endswith("/models")
        assert headers["Authorization"].startswith("Bearer ")
        return self.response

    # 辅助方法：post 实现测试替身在此调用阶段需要的最小行为。
    def post(self, url: str, headers: dict[str, str], json: dict) -> httpx.Response:  # type: ignore[no-untyped-def]
        assert url.endswith(("/responses", "/chat/completions"))
        assert headers["Authorization"].startswith("Bearer ")
        if url.endswith("/responses"):
            assert json["store"] is False
            assert json["max_output_tokens"] == 16
        return self.protocol_response

    # 测试替身类：_Stream 保存该局部场景的可控状态。
    class _Stream:
        # 辅助方法：__init__ 实现测试替身在此调用阶段需要的最小行为。
        def __init__(self, response: httpx.Response):
            self.response = response

        # 辅助方法：__enter__ 实现测试替身在此调用阶段需要的最小行为。
        def __enter__(self):  # type: ignore[no-untyped-def]
            return self.response

        # 辅助方法：__exit__ 实现测试替身在此调用阶段需要的最小行为。
        def __exit__(self, *_args):  # type: ignore[no-untyped-def]
            return None

    # 辅助方法：stream 实现测试替身在此调用阶段需要的最小行为。
    def stream(self, method: str, url: str, headers: dict[str, str], json: dict) -> "FakeHttpClient._Stream":  # type: ignore[no-untyped-def]
        assert method == "POST"
        assert json["stream"] is True
        return self._Stream(self.post(url, headers=headers, json=json))


@pytest.fixture()
# 测试夹具：secret_backend 创建本组用例共享的隔离资源，并在测试结束后恢复数据库、配置或进程状态。
def secret_backend(monkeypatch: pytest.MonkeyPatch) -> dict[tuple[str, str], str]:
    # stored 以“服务名、连接标识”为键模拟系统密钥环，确保凭据不会落入业务数据库。
    stored: dict[tuple[str, str], str] = {}
    monkeypatch.setattr(
        secrets.keyring, "set_password", lambda service, ref, value: stored.__setitem__((service, ref), value)
    )
    monkeypatch.setattr(secrets.keyring, "get_password", lambda service, ref: stored.get((service, ref)))
    monkeypatch.setattr(secrets.keyring, "delete_password", lambda service, ref: stored.pop((service, ref)))
    return stored


@pytest.fixture()
# 测试夹具：client 创建本组用例共享的隔离资源，并在测试结束后恢复数据库、配置或进程状态。
def client(tmp_path: Path, secret_backend: dict[tuple[str, str], str]) -> TestClient:
    # 临时数据库保存连接元数据；secret_backend 单独保存密钥，test_client 用于调用连接管理 API。
    configure_database(f"sqlite:///{(tmp_path / 'connections.db').as_posix()}")
    init_db()
    app = FastAPI()
    app.include_router(connections_router)
    with TestClient(app) as test_client:
        yield test_client
    Base.metadata.drop_all(bind=database.engine)


# 辅助函数：mock_response 封装本组测试重复使用的输入准备、状态查询或测试替身行为。
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


# 测试场景：验证接口或资源生命周期操作会返回正确结果并同步持久化状态；函数名 test_create_discovers_models_and_keeps_key_out_of_database_and_response 精确标识本用例的具体条件。
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
    assert body["thinking_level"] == "medium"
    assert "api_key" not in body
    assert "secret_ref" not in body
    assert list(secret_backend.values()) == ["secret-key"]

    test_response = client.post(f"/api/connections/{body['id']}/test")
    assert test_response.status_code == 200
    assert test_response.json()["category"] == "ok"


# 测试场景：验证接口或资源生命周期操作会返回正确结果并同步持久化状态；函数名 test_manual_models_allow_missing_models_endpoint 精确标识本用例的具体条件。
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
# 测试场景：验证失败会保留可诊断信息并收敛为一致、可恢复的状态；函数名 test_connection_errors_are_classified 精确标识本用例的具体条件。
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


# 测试场景：验证非法、越界或不满足前置条件的操作会被明确拒绝，且不会产生错误状态；函数名 test_create_rejects_discovery_failure_without_manual_models 精确标识本用例的具体条件。
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


# 测试场景：验证非法、越界或不满足前置条件的操作会被明确拒绝，且不会产生错误状态；函数名 test_manual_models_do_not_hide_invalid_credentials 精确标识本用例的具体条件。
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


# 测试场景：验证接口或资源生命周期操作会返回正确结果并同步持久化状态；函数名 test_connection_thinking_level_can_be_updated 精确标识本用例的具体条件。
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


# 测试场景：重新发现只刷新 /models 目录，不发送可能产生计费或权限错误的模型推理请求。
def test_rediscover_models_does_not_probe_chat_completions(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    mock_response(monkeypatch, 200, {"data": [{"id": "model-a"}]})
    created = client.post(
        "/api/connections",
        json={
            "name": "Chat relay",
            "base_url": "https://provider.test/v1",
            "api_key": "key",
            "api_protocol": "chat_completions",
        },
    ).json()

    mock_response(
        monkeypatch,
        200,
        {"data": [{"id": "model-b"}, {"id": "model-a"}]},
        protocol_status=403,
    )
    response = client.post(f"/api/connections/{created['id']}/discover")

    assert response.status_code == 200, response.text
    assert response.json()["discovered_models"] == ["model-a", "model-b"]
    assert response.json()["status"] == "connected"
    assert response.json()["last_error"] is None


# 测试场景：禁用模型后连接默认值及现有会话、Agent 选择同步移除，后端不能继续引用已禁用模型。
def test_disabling_model_clears_persisted_model_selections(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    mock_response(monkeypatch, 200, {"data": [{"id": "model-a"}, {"id": "model-b"}]})
    created = client.post(
        "/api/connections",
        json={
            "name": "Selectable relay",
            "base_url": "https://provider.test/v1",
            "api_key": "key",
        },
    ).json()
    with database.SessionLocal() as db:
        agent = database.Agent(
            name="Selected model agent",
            model_connection_id=created["id"],
            model_id="model-a",
        )
        session = database.Session(
            title="Selected model session",
            model_connection_id=created["id"],
            model_id="model-a",
        )
        db.add_all([agent, session])
        db.commit()
        agent_id, session_id = agent.id, session.id

    response = client.patch(
        f"/api/connections/{created['id']}",
        json={"disabled_models": ["model-a"]},
    )

    assert response.status_code == 200, response.text
    assert response.json()["disabled_models"] == ["model-a"]
    assert response.json()["default_model"] == "model-b"
    with database.SessionLocal() as db:
        assert db.get(database.Agent, agent_id).model_id is None
        assert db.get(database.Session, session_id).model_id is None


# 测试场景：模型目录暂时缺项时保留用户停用记录，避免模型恢复后被静默重新启用。
def test_rediscover_preserves_disabled_models_missing_from_current_catalog(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    mock_response(monkeypatch, 200, {"data": [{"id": "model-a"}, {"id": "model-b"}]})
    created = client.post(
        "/api/connections",
        json={
            "name": "Changing catalog relay",
            "base_url": "https://provider.test/v1",
            "api_key": "key",
        },
    ).json()
    disabled = client.patch(
        f"/api/connections/{created['id']}",
        json={"disabled_models": ["model-b"]},
    )
    assert disabled.status_code == 200, disabled.text

    mock_response(monkeypatch, 200, {"data": [{"id": "model-a"}]})
    rediscovered = client.post(f"/api/connections/{created['id']}/discover")

    assert rediscovered.status_code == 200, rediscovered.text
    assert rediscovered.json()["discovered_models"] == ["model-a"]
    assert rediscovered.json()["disabled_models"] == ["model-b"]


# 测试场景：验证非法、越界或不满足前置条件的操作会被明确拒绝，且不会产生错误状态；函数名 test_secret_headers_cannot_be_persisted 精确标识本用例的具体条件。
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
