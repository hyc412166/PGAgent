"""验证能力目录、技能导入删除、市场访问、OIDC 刷新以及代理和会话能力绑定 API。

测试通过 fixture 或辅助函数准备隔离环境，再调用真实服务、路由或运行时，并检查返回值、持久化状态与可观察副作用。
变量约定：tmp_path/monkeypatch 提供隔离环境，client/store/runtime 驱动被测链路，各类 *_id 串联持久化实体，payload 表示输入，response/result 表示实际输出，expected 表示期望值。
"""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

from src.persistence import database
from src.api.capabilities import router as capabilities_router
from src.api.routes import router as resources_router
from src.api.runtime import router as runtime_router
from src.tools.catalog import BUILTIN_TOOL_IDS
from src.persistence.database import (
    DEFAULT_AGENT_ID,
    Agent,
    Base,
    ChatMessage,
    DraftLaunch,
    Run,
    Session,
    Skill,
    configure_database,
    init_db,
)
from src.skills import registry as skill_service
from src.runs.service import coordinator


@pytest.fixture()
# 测试夹具：client 创建本组用例共享的隔离资源，并在测试结束后恢复数据库、配置或进程状态。
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[TestClient, dict[str, list]]:
    # 临时数据库保存能力绑定；scheduled 记录草稿启动请求，test_client 驱动能力与市场路由。
    configure_database(f"sqlite:///{(tmp_path / 'capabilities.db').as_posix()}")
    init_db()
    managed_root = tmp_path / "managed-skills"
    monkeypatch.setattr(skill_service, "_skills_root", lambda: managed_root)
    launched: dict[str, list] = {"calls": []}
    monkeypatch.setattr(
        coordinator,
        "launch",
        lambda run_id, resume=False: launched["calls"].append((run_id, resume)) or True,
    )
    app = FastAPI()
    app.include_router(resources_router)
    app.include_router(capabilities_router)
    app.include_router(runtime_router)
    with TestClient(app) as test_client:
        yield test_client, launched
    Base.metadata.drop_all(bind=database.engine)


# 辅助函数：_write_local_skill 封装本组测试重复使用的输入准备、状态查询或测试替身行为。
def _write_local_skill(root: Path, *, name: str = "Demo Local Skill") -> Path:
    source = root / "source-skill"
    (source / "scripts").mkdir(parents=True)
    (source / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: Imported without executing scripts.\nversion: 1.2.3\n---\n\n# {name}\n\nSafe instructions.\n",
        encoding="utf-8",
    )
    (source / "scripts" / "never_run.py").write_text("raise RuntimeError('must not run')\n", encoding="utf-8")
    return source


# 辅助函数：_import_skill 封装本组测试重复使用的输入准备、状态查询或测试替身行为。
def _import_skill(test_client: TestClient, tmp_path: Path) -> dict:
    source = _write_local_skill(tmp_path)
    response = test_client.post("/api/skills/import", json={"source_path": str(source)})
    assert response.status_code == 201, response.text
    return response.json()


# 测试场景：验证该正常业务场景从输入准备到结果断言的完整链路；函数名 test_tool_catalog_and_fixed_master_advertise_stable_tool_ids 精确标识本用例的具体条件。
def test_tool_catalog_and_fixed_master_advertise_stable_tool_ids(
    client: tuple[TestClient, dict[str, list]],
) -> None:
    test_client, _launched = client
    response = test_client.get("/api/tools")
    assert response.status_code == 200
    tools = response.json()
    by_id = {item["id"]: item for item in tools}
    assert set(by_id) == {
        "shell",
        "write_stdin",
        "read",
        "read_artifact",
        "glob",
        "rg",
        "web_search",
        "web_open",
        "task",
        "update_plan",
        "tool_search",
        "question",
        "git_status",
        "git_diff",
        "apply_patch",
        "validate",
        "validate_baseline",
        "review_finding",
        "debug_evidence",
        "web_run",
    }
    assert by_id["shell"]["runtime_tool_id"] == "shell"
    assert by_id["read"]["risk_level"] == "low"
    assert by_id["apply_patch"]["risk_level"] == "adaptive"
    assert by_id["git_diff"]["risk_level"] == "low"
    assert "工作区相对路径" in by_id["apply_patch"]["description"]
    assert "每一行都必须以 + 开头" in by_id["apply_patch"]["description"]
    assert "Git 仓库" in by_id["git_status"]["description"]
    assert "工作区相对路径" in by_id["glob"]["description"]
    assert "tool_search" in by_id["rg"]["description"]
    assert "unsafe_url" in by_id["web_run"]["description"]
    assert "后台失败" in by_id["shell"]["description"]
    assert by_id["web_search"]["availability"] == "available"
    assert by_id["web_run"]["runtime_tool_id"] == "web_run"
    assert by_id["web_open"]["runtime_tool_id"] == "web_open"
    assert by_id["review_finding"]["risk_level"] == "low"
    assert by_id["debug_evidence"]["runtime_tool_id"] == "debug_evidence"

    default_agent = test_client.get(f"/api/agents/{DEFAULT_AGENT_ID}")
    assert default_agent.status_code == 200
    assert default_agent.json()["tool_ids"] == sorted(BUILTIN_TOOL_IDS)
    assert default_agent.json()["skill_ids"] == []
    system_prompt = default_agent.json()["system_prompt"]
    assert "Get-Command" in system_prompt
    assert "tar 或 7z" in system_prompt
    assert "apply_patch 只能使用工作区相对路径" in system_prompt
    assert "git_status 和 git_diff" in system_prompt
    assert "select:grep" in system_prompt
    assert "approval_required" in system_prompt
    assert "不要自行拼接 searchN/ref_id" in system_prompt
    locked = test_client.patch(f"/api/agents/{DEFAULT_AGENT_ID}", json={"tool_ids": ["read"]})
    assert locked.status_code == 409


# 测试场景：验证该正常业务场景从输入准备到结果断言的完整链路；函数名 test_local_skill_import_copies_files_without_execution_and_lists_metadata 精确标识本用例的具体条件。
def test_local_skill_import_copies_files_without_execution_and_lists_metadata(
    client: tuple[TestClient, dict[str, list]], tmp_path: Path
) -> None:
    test_client, _launched = client
    source = _write_local_skill(tmp_path)
    response = test_client.post("/api/skills/import", json={"source_path": str(source)})
    assert response.status_code == 201, response.text
    skill = response.json()
    assert skill["slug"] == "demo-local-skill"
    assert skill["source"] == "local"
    assert skill["version"] == "1.2.3"
    installed_root = Path(skill["root_path"])
    assert installed_root != source
    assert (installed_root / "SKILL.md").read_text(encoding="utf-8").startswith("---")
    assert (installed_root / "scripts" / "never_run.py").exists()

    listed = test_client.get("/api/skills")
    assert listed.status_code == 200
    assert [item["id"] for item in listed.json()] == [skill["id"]]
    duplicate = test_client.post("/api/skills/import", json={"source_path": str(source)})
    assert duplicate.status_code == 409


# 测试场景：验证接口或资源生命周期操作会返回正确结果并同步持久化状态；函数名 test_delete_skill_removes_managed_files_and_capability_bindings 精确标识本用例的具体条件。
def test_delete_skill_removes_managed_files_and_capability_bindings(
    client: tuple[TestClient, dict[str, list]], tmp_path: Path
) -> None:
    test_client, _launched = client
    skill = _import_skill(test_client, tmp_path)
    installed_root = Path(skill["root_path"])
    agent = test_client.post(
        "/api/agents",
        json={"name": "Skill owner", "skill_ids": [skill["id"]]},
    ).json()
    chat_session = test_client.post(
        "/api/sessions",
        json={"title": "Skill session", "skill_ids": [skill["id"]]},
    ).json()

    response = test_client.delete(f"/api/skills/{skill['id']}")

    assert response.status_code == 204, response.text
    assert not installed_root.exists()
    assert test_client.get("/api/skills").json() == []
    assert test_client.get(f"/api/agents/{agent['id']}").json()["skill_ids"] == []
    assert test_client.get(f"/api/sessions/{chat_session['id']}").json()["skill_ids"] == []


# 测试场景：验证非法、越界或不满足前置条件的操作会被明确拒绝，且不会产生错误状态；函数名 test_delete_skill_refuses_to_remove_files_outside_managed_storage 精确标识本用例的具体条件。
def test_delete_skill_refuses_to_remove_files_outside_managed_storage(
    client: tuple[TestClient, dict[str, list]], tmp_path: Path
) -> None:
    test_client, _launched = client
    outside_root = tmp_path / "outside-skill"
    outside_root.mkdir()
    with database.SessionLocal() as db:
        item = Skill(
            slug="outside-skill",
            name="Outside Skill",
            description="Not managed by PGAgent",
            root_path=str(outside_root),
        )
        db.add(item)
        db.commit()
        skill_id = item.id

    response = test_client.delete(f"/api/skills/{skill_id}")

    assert response.status_code == 409
    assert outside_root.exists()
    assert [item["id"] for item in test_client.get("/api/skills").json()] == [skill_id]


# 测试场景：验证状态能够可靠持久化、重放或在重启后恢复，并保持记录之间的关联；函数名 test_agent_and_session_capability_relations_persist_through_api 精确标识本用例的具体条件。
def test_agent_and_session_capability_relations_persist_through_api(
    client: tuple[TestClient, dict[str, list]], tmp_path: Path
) -> None:
    test_client, _launched = client
    skill = _import_skill(test_client, tmp_path)
    agent_response = test_client.post(
        "/api/agents",
        json={
            "name": "Research helper",
            "workflow_profile_id": "review",
            "tool_ids": ["read", "shell", "read"],
            "skill_ids": [skill["id"]],
        },
    )
    assert agent_response.status_code == 201, agent_response.text
    agent = agent_response.json()
    assert agent["tool_ids"] == ["read", "shell"]
    assert agent["skill_ids"] == [skill["id"]]
    assert agent["workflow_profile_id"] == "review"

    updated_agent = test_client.patch(
        f"/api/agents/{agent['id']}",
        json={"workflow_profile_id": "debug", "tool_ids": ["apply_patch"], "skill_ids": []},
    )
    assert updated_agent.status_code == 200
    assert updated_agent.json()["tool_ids"] == ["apply_patch"]
    assert updated_agent.json()["skill_ids"] == []
    assert updated_agent.json()["workflow_profile_id"] == "debug"

    session_response = test_client.post(
        "/api/sessions",
        json={"title": "Permission test", "permission_mode": "ask", "skill_ids": [skill["id"]]},
    )
    assert session_response.status_code == 201, session_response.text
    chat_session = session_response.json()
    assert chat_session["agent_id"] == DEFAULT_AGENT_ID
    assert chat_session["permission_mode"] == "ask"
    assert chat_session["skill_ids"] == [skill["id"]]

    updated_session = test_client.patch(
        f"/api/sessions/{chat_session['id']}", json={"permission_mode": "full", "skill_ids": []}
    )
    assert updated_session.status_code == 200
    assert updated_session.json()["permission_mode"] == "full"
    assert updated_session.json()["skill_ids"] == []


# 测试场景：权限模式修改后成为后续新会话和草稿启动的默认值。
def test_permission_mode_preference_is_reused_by_new_sessions_and_drafts(
    client: tuple[TestClient, dict[str, list]],
) -> None:
    test_client, _launched = client
    assert test_client.get("/api/permissions/settings").json() == {"permission_mode": "smart"}

    changed = test_client.put("/api/permissions/settings", json={"permission_mode": "full"})
    assert changed.status_code == 200
    assert changed.json() == {"permission_mode": "full"}

    session = test_client.post("/api/sessions", json={"title": "Inherited permission"})
    assert session.status_code == 201, session.text
    assert session.json()["permission_mode"] == "full"

    changed_session = test_client.patch(
        f"/api/sessions/{session.json()['id']}", json={"permission_mode": "ask"}
    )
    assert changed_session.status_code == 200
    assert changed_session.json()["permission_mode"] == "ask"

    next_session = test_client.post("/api/sessions", json={"title": "Ask inherited"})
    assert next_session.status_code == 201, next_session.text
    assert next_session.json()["permission_mode"] == "ask"

    draft = test_client.post(
        "/api/drafts/launch",
        json={"idempotency_key": "permission-inherited-draft", "title": "Inherited draft", "content": "hello"},
    )
    assert draft.status_code == 202, draft.text
    assert draft.json()["session"]["permission_mode"] == "ask"


# 测试场景：验证状态能够可靠持久化、重放或在重启后恢复，并保持记录之间的关联；函数名 test_draft_launch_persists_permission_and_skills_and_binds_them_to_idempotency 精确标识本用例的具体条件。
def test_draft_launch_persists_permission_and_skills_and_binds_them_to_idempotency(
    client: tuple[TestClient, dict[str, list]], tmp_path: Path
) -> None:
    test_client, launched = client
    skill = _import_skill(test_client, tmp_path)
    payload = {
        "idempotency_key": "draft-permission-and-skills",
        "title": "Use configured Skill",
        "content": "Please use the configured skill safely.",
        "thinking_level": "high",
        "permission_mode": "ask",
        "use_memories": False,
        "skill_ids": [skill["id"]],
    }
    first = test_client.post("/api/drafts/launch", json=payload)
    assert first.status_code == 202, first.text
    body = first.json()
    assert body["session"]["permission_mode"] == "ask"
    assert body["session"]["use_memories"] is False
    assert body["session"]["skill_ids"] == [skill["id"]]
    assert launched["calls"] == [(body["run"]["id"], False)]

    changed_permission = test_client.post(
        "/api/drafts/launch", json={**payload, "permission_mode": "full"}
    )
    assert changed_permission.status_code == 409
    changed_skills = test_client.post(
        "/api/drafts/launch", json={**payload, "skill_ids": []}
    )
    assert changed_skills.status_code == 409
    changed_memories = test_client.post(
        "/api/drafts/launch", json={**payload, "use_memories": True}
    )
    assert changed_memories.status_code == 409
    with database.SessionLocal() as db:
        assert db.query(DraftLaunch).count() == 1
        assert db.query(Session).count() == 1
        assert db.query(ChatMessage).count() == 1
        assert db.query(Run).count() == 1


# 测试场景：验证非法、越界或不满足前置条件的操作会被明确拒绝，且不会产生错误状态；函数名 test_market_status_does_not_pretend_an_unauthenticated_skills_sh_search_works 精确标识本用例的具体条件。
def test_market_status_does_not_pretend_an_unauthenticated_skills_sh_search_works(
    client: tuple[TestClient, dict[str, list]], monkeypatch: pytest.MonkeyPatch
) -> None:
    test_client, _launched = client
    monkeypatch.delenv("SKILLS_SH_API_TOKEN", raising=False)
    monkeypatch.delenv("PGAGENT_SKILLS_SH_API_TOKEN", raising=False)
    monkeypatch.delenv("VERCEL_OIDC_TOKEN", raising=False)
    monkeypatch.delenv("PGAGENT_SKILL_MARKET_URL", raising=False)
    monkeypatch.delenv("PGAGENT_SKILL_MARKET_CLIENT_TOKEN", raising=False)
    status_response = test_client.get("/api/skills/market/status")
    assert status_response.status_code == 200
    assert status_response.json()["available"] is False
    search_response = test_client.post("/api/skills/market/search", json={"query": "react"})
    assert search_response.status_code == 200
    assert search_response.json()["available"] is False
    assert search_response.json()["items"] == []
    browse_response = test_client.get("/api/skills/market/browse?view=trending")
    assert browse_response.status_code == 200
    assert browse_response.json()["available"] is False
    assert browse_response.json()["items"] == []


# 测试场景：验证权限、审批或敏感数据边界在完整调用链路中保持有效；函数名 test_market_gateway_takes_precedence_and_keeps_its_client_token_server_side 精确标识本用例的具体条件。
def test_market_gateway_takes_precedence_and_keeps_its_client_token_server_side(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PGAGENT_SKILL_MARKET_URL", "https://pgagent-skills.vercel.app/")
    monkeypatch.setenv("PGAGENT_SKILL_MARKET_CLIENT_TOKEN", "gateway-client-token")
    monkeypatch.setenv("SKILLS_SH_API_TOKEN", "legacy-direct-token")
    requests: list[dict[str, object]] = []

    # 局部测试函数：fake_get 模拟该步骤的返回结果或异常。
    def fake_get(url: str, **kwargs: object) -> skill_service.httpx.Response:
        requests.append({"url": url, **kwargs})
        return skill_service.httpx.Response(200, json={"data": []})

    monkeypatch.setattr(skill_service.httpx, "get", fake_get)

    assert skill_service.market_status() == (True, None)
    assert skill_service._skills_sh_json(
        "/api/v1/skills/search", params={"q": "react", "limit": 20}
    ) == {"data": []}
    assert requests == [
        {
            "url": "https://pgagent-skills.vercel.app/api/market/skills/search",
            "params": {"q": "react", "limit": 20},
            "headers": {"Authorization": "Bearer gateway-client-token", "Accept": "application/json"},
            "timeout": 25.0,
            "follow_redirects": False,
        }
    ]


# 测试场景：验证权限、审批或敏感数据边界在完整调用链路中保持有效；函数名 test_market_gateway_requires_url_and_client_token_together 精确标识本用例的具体条件。
def test_market_gateway_requires_url_and_client_token_together(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PGAGENT_SKILL_MARKET_URL", "https://pgagent-skills.vercel.app")
    monkeypatch.delenv("PGAGENT_SKILL_MARKET_CLIENT_TOKEN", raising=False)

    available, message = skill_service.market_status()

    assert available is False
    assert message is not None and "must be configured together" in message
    with pytest.raises(skill_service.HTTPException, match="must be configured together"):
        skill_service._skills_sh_json("/api/v1/skills")


# 测试场景：验证非法、越界或不满足前置条件的操作会被明确拒绝，且不会产生错误状态；函数名 test_market_gateway_distinguishes_an_upstream_oidc_rejection 精确标识本用例的具体条件。
def test_market_gateway_distinguishes_an_upstream_oidc_rejection(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PGAGENT_SKILL_MARKET_URL", "https://pgagent-skills.vercel.app")
    monkeypatch.setenv("PGAGENT_SKILL_MARKET_CLIENT_TOKEN", "gateway-client-token")
    monkeypatch.setattr(
        skill_service.httpx,
        "get",
        lambda *_args, **_kwargs: skill_service.httpx.Response(502, json={"error": "oidc_rejected"}),
    )

    with pytest.raises(skill_service.HTTPException, match="rejected the gateway OIDC token"):
        skill_service._skills_sh_json("/api/v1/skills")


# 测试场景：验证权限、审批或敏感数据边界在完整调用链路中保持有效；函数名 test_skills_sh_retries_once_with_a_refreshed_vercel_oidc_token 精确标识本用例的具体条件。
def test_skills_sh_retries_once_with_a_refreshed_vercel_oidc_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("SKILLS_SH_API_TOKEN", raising=False)
    monkeypatch.delenv("PGAGENT_SKILLS_SH_API_TOKEN", raising=False)
    monkeypatch.setenv("VERCEL_OIDC_TOKEN", "expired-token")
    authorizations: list[str] = []

    # 局部测试函数：fake_get 模拟该步骤的返回结果或异常。
    def fake_get(_url: str, **kwargs: object) -> skill_service.httpx.Response:
        headers = kwargs["headers"]
        assert isinstance(headers, dict)
        authorizations.append(str(headers["Authorization"]))
        if len(authorizations) == 1:
            return skill_service.httpx.Response(401)
        return skill_service.httpx.Response(200, json={"data": []})

    # 局部测试函数：fake_refresh 模拟该步骤的返回结果或异常。
    def fake_refresh(*, failed_token: str | None = None) -> str:
        assert failed_token == "expired-token"
        monkeypatch.setenv("VERCEL_OIDC_TOKEN", "fresh-token")
        return "fresh-token"

    monkeypatch.setattr(skill_service.httpx, "get", fake_get)
    monkeypatch.setattr(skill_service, "_refresh_vercel_oidc_token", fake_refresh)

    assert skill_service._skills_sh_json("/api/v1/skills") == {"data": []}
    assert authorizations == ["Bearer expired-token", "Bearer fresh-token"]


# 测试场景：验证时间、容量或上下文预算边界以及达到边界后的可观察处理结果；函数名 test_vercel_oidc_refresh_runs_the_bounded_script_and_updates_process_environment 精确标识本用例的具体条件。
def test_vercel_oidc_refresh_runs_the_bounded_script_and_updates_process_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    environment_file = tmp_path / ".env.local"
    monkeypatch.setattr(skill_service, "LOCAL_ENV_FILE", environment_file)
    monkeypatch.setenv("VERCEL_OIDC_TOKEN", "expired-token")
    calls: list[dict[str, object]] = []

    # 局部测试函数：fake_run 模拟该步骤的返回结果或异常。
    def fake_run(command: list[str], **kwargs: object) -> skill_service.subprocess.CompletedProcess[str]:
        calls.append({"command": command, **kwargs})
        environment_file.write_text("VERCEL_OIDC_TOKEN=fresh-token\n", encoding="utf-8")
        return skill_service.subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(skill_service.subprocess, "run", fake_run)

    assert skill_service._refresh_vercel_oidc_token(failed_token="expired-token") == "fresh-token"
    assert calls[0]["timeout"] == 90
    assert calls[0]["capture_output"] is True
    assert calls[0]["check"] is False
    assert calls[0]["command"][-1] == str(skill_service.SKILL_TOKEN_REFRESH_SCRIPT)
    assert skill_service.os.environ["VERCEL_OIDC_TOKEN"] == "fresh-token"


# 测试场景：验证时间、容量或上下文预算边界以及达到边界后的可观察处理结果；函数名 test_vercel_oidc_refresh_reports_timeout_without_exposing_subprocess_output 精确标识本用例的具体条件。
def test_vercel_oidc_refresh_reports_timeout_without_exposing_subprocess_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("VERCEL_OIDC_TOKEN", "expired-token")

    # 局部测试函数：fake_run 模拟该步骤的返回结果或异常。
    def fake_run(_command: list[str], **_kwargs: object) -> None:
        raise skill_service.subprocess.TimeoutExpired("powershell.exe", 90, output="secret-output")

    monkeypatch.setattr(skill_service.subprocess, "run", fake_run)

    with pytest.raises(skill_service.HTTPException, match="could not be started") as exc_info:
        skill_service._refresh_vercel_oidc_token(failed_token="expired-token")
    assert "secret-output" not in str(exc_info.value.detail)


# 测试场景：验证取消或终止请求会收敛相关运行状态，并正确清理或保留应有资源；函数名 test_skills_sh_stops_after_one_failed_oidc_retry 精确标识本用例的具体条件。
def test_skills_sh_stops_after_one_failed_oidc_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SKILLS_SH_API_TOKEN", raising=False)
    monkeypatch.delenv("PGAGENT_SKILLS_SH_API_TOKEN", raising=False)
    monkeypatch.setenv("VERCEL_OIDC_TOKEN", "expired-token")
    requests = 0
    refreshes = 0

    # 局部测试函数：fake_get 模拟该步骤的返回结果或异常。
    def fake_get(_url: str, **_kwargs: object) -> skill_service.httpx.Response:
        nonlocal requests
        requests += 1
        return skill_service.httpx.Response(401)

    # 局部测试函数：fake_refresh 模拟该步骤的返回结果或异常。
    def fake_refresh(*, failed_token: str | None = None) -> str:
        nonlocal refreshes
        assert failed_token == "expired-token"
        refreshes += 1
        return "fresh-but-rejected-token"

    monkeypatch.setattr(skill_service.httpx, "get", fake_get)
    monkeypatch.setattr(skill_service, "_refresh_vercel_oidc_token", fake_refresh)

    with pytest.raises(skill_service.HTTPException, match="rejected the configured marketplace token"):
        skill_service._skills_sh_json("/api/v1/skills")
    assert requests == 2
    assert refreshes == 1


# 测试场景：验证非法、越界或不满足前置条件的操作会被明确拒绝，且不会产生错误状态；函数名 test_skills_sh_does_not_replace_an_explicit_static_market_token 精确标识本用例的具体条件。
def test_skills_sh_does_not_replace_an_explicit_static_market_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SKILLS_SH_API_TOKEN", "explicit-token")
    refresh_called = False

    # 局部测试函数：fake_get 模拟该步骤的返回结果或异常。
    def fake_get(_url: str, **_kwargs: object) -> skill_service.httpx.Response:
        return skill_service.httpx.Response(401)

    # 局部测试函数：fake_refresh 模拟该步骤的返回结果或异常。
    def fake_refresh(*, failed_token: str | None = None) -> str:
        nonlocal refresh_called
        refresh_called = True
        return "unexpected-token"

    monkeypatch.setattr(skill_service.httpx, "get", fake_get)
    monkeypatch.setattr(skill_service, "_refresh_vercel_oidc_token", fake_refresh)

    with pytest.raises(skill_service.HTTPException, match="rejected the configured marketplace token"):
        skill_service._skills_sh_json("/api/v1/skills")
    assert refresh_called is False


# 测试场景：验证该正常业务场景从输入准备到结果断言的完整链路；函数名 test_market_browse_normalizes_leaderboard_views_without_exposing_file_contents 精确标识本用例的具体条件。
def test_market_browse_normalizes_leaderboard_views_without_exposing_file_contents(
    client: tuple[TestClient, dict[str, list]], monkeypatch: pytest.MonkeyPatch
) -> None:
    test_client, _launched = client
    monkeypatch.setenv("PGAGENT_SKILLS_SH_API_TOKEN", "test-market-token")
    calls: list[tuple[str, dict | None]] = []

    # 局部测试函数：fake_skills_sh 模拟该步骤的返回结果或异常。
    def fake_skills_sh(path: str, *, params: dict | None = None) -> dict:
        calls.append((path, params))
        return {
            "data": [
                {
                    "id": "demo/repo/secure-skill",
                    "slug": "secure-skill",
                    "name": "Secure Skill",
                    "source": "demo/repo",
                    "installs": 321,
                    "change": 29,
                    "installsYesterday": 2,
                    "installUrl": "https://github.com/demo/repo",
                    "url": "https://skills.sh/demo/repo/secure-skill",
                    "isDuplicate": True,
                    "files": [{"path": "SKILL.md", "contents": "must never appear here"}],
                }
            ],
            "pagination": {"page": 2, "perPage": 7, "total": 99, "hasMore": True},
        }

    monkeypatch.setattr(skill_service, "_skills_sh_json", fake_skills_sh)
    response = test_client.get("/api/skills/market/browse?view=hot&page=2&per_page=7")

    assert response.status_code == 200, response.text
    body = response.json()
    assert calls == [("/api/v1/skills", {"view": "hot", "page": 2, "per_page": 7})]
    assert body["view"] == "hot"
    assert body["page"] == 2
    assert body["has_more"] is True
    assert body["total"] == 99
    assert body["items"] == [{
        "id": "demo/repo/secure-skill",
        "slug": "secure-skill",
        "name": "Secure Skill",
        "source": "demo/repo",
        "source_url": "https://github.com/demo/repo",
        "market_url": "https://skills.sh/demo/repo/secure-skill",
        "installs": 321,
        "change": 29,
        "installs_yesterday": 2,
        "is_official": False,
        "official_owner": None,
        "is_duplicate": True,
    }]
    assert "must never appear here" not in response.text


# 测试场景：验证该正常业务场景从输入准备到结果断言的完整链路；函数名 test_market_browse_flattens_curated_owners_and_marks_official_skills 精确标识本用例的具体条件。
def test_market_browse_flattens_curated_owners_and_marks_official_skills(
    client: tuple[TestClient, dict[str, list]], monkeypatch: pytest.MonkeyPatch
) -> None:
    test_client, _launched = client
    monkeypatch.setenv("PGAGENT_SKILLS_SH_API_TOKEN", "test-market-token")
    monkeypatch.setattr(
        skill_service,
        "_skills_sh_json",
        lambda path, *, params=None: {
            "data": [
                {
                    "owner": "official-maker",
                    "skills": [
                        {"id": "official-maker/docs/product", "slug": "product", "name": "Product", "source": "official-maker/docs", "installs": 10},
                        {"id": "official-maker/docs/second", "slug": "second", "name": "Second", "source": "official-maker/docs", "installs": 9},
                    ],
                }
            ]
        },
    )

    response = test_client.get("/api/skills/market/browse?view=curated&page=0&per_page=1")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["view"] == "curated"
    assert body["total"] == 2
    assert body["has_more"] is True
    assert body["items"][0]["is_official"] is True
    assert body["items"][0]["official_owner"] == "official-maker"


# 测试场景：验证权限、审批或敏感数据边界在完整调用链路中保持有效；函数名 test_market_leaderboards_cache_five_topic_rankings_and_keep_sensitive_fields_private 精确标识本用例的具体条件。
def test_market_leaderboards_cache_five_topic_rankings_and_keep_sensitive_fields_private(
    client: tuple[TestClient, dict[str, list]], monkeypatch: pytest.MonkeyPatch
) -> None:
    test_client, _launched = client
    monkeypatch.setenv("PGAGENT_SKILLS_SH_API_TOKEN", "test-market-token")
    monkeypatch.setattr(skill_service, "_market_leaderboard_cache", None)
    calls: list[dict[str, object]] = []
    installs = [21, 90, 8, 72, 55, 31, 49, 7]

    # 局部测试函数：fake_skills_sh 模拟该步骤的返回结果或异常。
    def fake_skills_sh(path: str, *, params: dict | None = None) -> dict:
        assert path == "/api/v1/skills/search"
        assert params is not None
        calls.append(dict(params))
        category = next(item for item in skill_service.MARKET_LEADERBOARD_CATEGORIES if item.query == params["q"])
        return {
            "data": [
                {
                    "id": f"demo/{category.id}/skill-{index}",
                    "slug": f"skill-{index}",
                    "name": f"{category.id} skill {index}",
                    "source": "demo/skills",
                    "installs": install_count,
                    "files": [{"path": "SKILL.md", "contents": "private skill body"}],
                    "token": "private-token",
                }
                for index, install_count in enumerate(installs)
            ]
        }

    monkeypatch.setattr(skill_service, "_skills_sh_json", fake_skills_sh)
    first = test_client.get("/api/skills/market/leaderboards")

    assert first.status_code == 200, first.text
    body = first.json()
    assert body["available"] is True
    assert body["cached"] is False
    assert body["ttl_seconds"] == skill_service.MARKET_LEADERBOARD_TTL_SECONDS
    assert [category["id"] for category in body["categories"]] == [
        "frontend",
        "programming",
        "research",
        "writing",
        "data-ai",
    ]
    assert len(calls) == 5
    assert all(call["limit"] == skill_service.MARKET_LEADERBOARD_SEARCH_LIMIT for call in calls)
    assert all(len(category["items"]) == 6 for category in body["categories"])
    assert all(category["items"][0]["installs"] == 90 for category in body["categories"])
    assert "private skill body" not in first.text
    assert "private-token" not in first.text

    cached = test_client.get("/api/skills/market/leaderboards")
    assert cached.status_code == 200, cached.text
    assert cached.json()["cached"] is True
    assert len(calls) == 5


# 测试场景：验证该正常业务场景从输入准备到结果断言的完整链路；函数名 test_market_leaderboards_expire_after_ttl_and_support_manual_refresh 精确标识本用例的具体条件。
def test_market_leaderboards_expire_after_ttl_and_support_manual_refresh(
    client: tuple[TestClient, dict[str, list]], monkeypatch: pytest.MonkeyPatch
) -> None:
    test_client, _launched = client
    monkeypatch.setenv("PGAGENT_SKILLS_SH_API_TOKEN", "test-market-token")
    monkeypatch.setattr(skill_service, "_market_leaderboard_cache", None)
    clock = {"value": 100.0}
    monkeypatch.setattr(skill_service.time, "monotonic", lambda: clock["value"])
    calls: list[dict[str, object]] = []

    # 局部测试函数：fake_skills_sh 模拟该步骤的返回结果或异常。
    def fake_skills_sh(path: str, *, params: dict | None = None) -> dict:
        assert path == "/api/v1/skills/search"
        assert params is not None
        calls.append(dict(params))
        return {
            "data": [
                {
                    "id": f"demo/skills/{len(calls)}",
                    "name": "Cached Skill",
                    "installs": 1,
                }
            ]
        }

    monkeypatch.setattr(skill_service, "_skills_sh_json", fake_skills_sh)
    initial = test_client.get("/api/skills/market/leaderboards")
    assert initial.status_code == 200, initial.text
    assert initial.json()["cached"] is False
    assert len(calls) == 5

    clock["value"] += skill_service.MARKET_LEADERBOARD_TTL_SECONDS - 1
    still_cached = test_client.get("/api/skills/market/leaderboards")
    assert still_cached.status_code == 200, still_cached.text
    assert still_cached.json()["cached"] is True
    assert len(calls) == 5

    clock["value"] += 2
    expired = test_client.get("/api/skills/market/leaderboards")
    assert expired.status_code == 200, expired.text
    assert expired.json()["cached"] is False
    assert len(calls) == 10

    query_refreshed = test_client.get("/api/skills/market/leaderboards?refresh=true")
    assert query_refreshed.status_code == 200, query_refreshed.text
    assert query_refreshed.json()["cached"] is False
    assert len(calls) == 15

    refreshed = test_client.post("/api/skills/market/leaderboards/refresh")
    assert refreshed.status_code == 200, refreshed.text
    assert refreshed.json()["cached"] is False
    assert len(calls) == 20


# 测试场景：验证非法、越界或不满足前置条件的操作会被明确拒绝，且不会产生错误状态；函数名 test_market_leaderboards_are_explicitly_unavailable_without_a_market_token 精确标识本用例的具体条件。
def test_market_leaderboards_are_explicitly_unavailable_without_a_market_token(
    client: tuple[TestClient, dict[str, list]], monkeypatch: pytest.MonkeyPatch
) -> None:
    test_client, _launched = client
    monkeypatch.delenv("SKILLS_SH_API_TOKEN", raising=False)
    monkeypatch.delenv("PGAGENT_SKILLS_SH_API_TOKEN", raising=False)
    monkeypatch.delenv("VERCEL_OIDC_TOKEN", raising=False)
    monkeypatch.setattr(skill_service, "_market_leaderboard_cache", None)
    upstream_called = False

    # 局部测试函数：fake_skills_sh 模拟该步骤的返回结果或异常。
    def fake_skills_sh(path: str, *, params: dict | None = None) -> dict:
        nonlocal upstream_called
        upstream_called = True
        return {"data": []}

    monkeypatch.setattr(skill_service, "_skills_sh_json", fake_skills_sh)
    response = test_client.get("/api/skills/market/leaderboards")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["available"] is False
    assert body["categories"] == []
    assert body["cached"] is False
    assert upstream_called is False


# 测试场景：验证该正常业务场景从输入准备到结果断言的完整链路；函数名 test_github_zip_is_previewed_before_confirmed_install 精确标识本用例的具体条件。
def test_github_zip_is_previewed_before_confirmed_install(
    client: tuple[TestClient, dict[str, list]], monkeypatch: pytest.MonkeyPatch
) -> None:
    test_client, _launched = client
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        archive.writestr(
            "demo-repo-main/skills/demo/SKILL.md",
            "---\nname: GitHub Demo\ndescription: Preview first.\n---\n# GitHub Demo\n",
        )
        archive.writestr("demo-repo-main/skills/demo/examples/example.txt", "safe example")
    monkeypatch.setattr(skill_service, "_archive_url_for_source", lambda _url: "https://github.com/demo/repo/archive.zip")
    monkeypatch.setattr(skill_service, "_download_github_archive", lambda _url: stream.getvalue())

    preview = test_client.post(
        "/api/skills/market/install",
        json={"source_url": "https://github.com/demo/repo", "confirm": False},
    )
    assert preview.status_code == 200, preview.text
    assert preview.json()["installed"] is False
    assert preview.json()["candidates"] == ["demo-repo-main/skills/demo"]
    assert {item["path"] for item in preview.json()["files"]} == {"SKILL.md", "examples/example.txt"}

    install = test_client.post(
        "/api/skills/market/install",
        json={
            "source_url": "https://github.com/demo/repo",
            "skill_path": "demo-repo-main/skills/demo",
            "confirm": True,
        },
    )
    assert install.status_code == 200, install.text
    assert install.json()["installed"] is True
    assert install.json()["skill"]["source"] == "github"


# 测试场景：验证该正常业务场景从输入准备到结果断言的完整链路；函数名 test_skill_relations_are_real_database_rows 精确标识本用例的具体条件。
def test_skill_relations_are_real_database_rows(
    client: tuple[TestClient, dict[str, list]], tmp_path: Path
) -> None:
    test_client, _launched = client
    skill = _import_skill(test_client, tmp_path)
    session = test_client.post(
        "/api/sessions", json={"title": "Rows", "skill_ids": [skill["id"]]}
    ).json()
    with database.SessionLocal() as db:
        persisted_skill = db.get(Skill, skill["id"])
        persisted_session = db.get(Session, session["id"])
        assert persisted_skill is not None
        assert persisted_session is not None
        assert persisted_session.skill_ids == [skill["id"]]
        assert db.scalar(select(Agent).where(Agent.id == DEFAULT_AGENT_ID)) is not None
