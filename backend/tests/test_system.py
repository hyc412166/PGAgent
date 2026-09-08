"""验证系统级目录选择与个性化指令 API，包括本地调用限制和配置覆盖。

测试通过 fixture 或辅助函数准备隔离环境，再调用真实服务、路由或运行时，并检查返回值、持久化状态与可观察副作用。
变量约定：tmp_path/monkeypatch 提供隔离环境，client/store/runtime 驱动被测链路，各类 *_id 串联持久化实体，payload 表示输入，response/result 表示实际输出，expected 表示期望值。
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.api import system
from src.context import instructions as instruction_service


# 辅助函数：_client 封装本组测试重复使用的输入准备、状态查询或测试替身行为。
def _client() -> TestClient:
    app = FastAPI()
    app.include_router(system.router)
    return TestClient(app, client=("127.0.0.1", 50000))


# 测试场景：验证该正常业务场景从输入准备到结果断言的完整链路；函数名 test_select_folder_returns_mocked_native_result 精确标识本用例的具体条件。
def test_select_folder_returns_mocked_native_result(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(system.platform, "system", lambda: "Windows")
    selected_titles: list[str] = []
    monkeypatch.setattr(
        system,
        "show_folder_picker",
        lambda title: selected_titles.append(title) or r"C:\Users\demo\workspace",
    )
    with _client() as client:
        response = client.post("/api/system/select-folder", json={"title": "Select Project Root"})
    assert response.status_code == 200
    assert response.json() == {"path": r"C:\Users\demo\workspace", "cancelled": False}
    assert selected_titles == ["Select Project Root"]


# 测试场景：验证取消或终止请求会收敛相关运行状态，并正确清理或保留应有资源；函数名 test_select_folder_reports_cancel_and_non_windows 精确标识本用例的具体条件。
def test_select_folder_reports_cancel_and_non_windows(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(system.platform, "system", lambda: "Windows")
    monkeypatch.setattr(system, "show_folder_picker", lambda _title: None)
    with _client() as client:
        cancelled = client.post("/api/system/select-folder")
    assert cancelled.json() == {"path": None, "cancelled": True}

    monkeypatch.setattr(system.platform, "system", lambda: "Linux")
    with _client() as client:
        unsupported = client.post("/api/system/select-folder")
    assert unsupported.status_code == 501


# 测试场景：验证接口或资源生命周期操作会返回正确结果并同步持久化状态；函数名 test_personalization_api_updates_global_agents_md 精确标识本用例的具体条件。
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


# 测试场景：验证接口或资源生命周期操作会返回正确结果并同步持久化状态；函数名 test_personalization_api_reports_active_override 精确标识本用例的具体条件。
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


# 测试场景：验证非法、越界或不满足前置条件的操作会被明确拒绝，且不会产生错误状态；函数名 test_personalization_endpoints_reject_remote_client 精确标识本用例的具体条件。
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
