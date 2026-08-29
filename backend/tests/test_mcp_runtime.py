from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from src.config import settings
from src.api.mcp import mcp_status
from src.mcp import attach_mcp_tools, mcp_runtime_pool
from src.mcp.config import load_mcp_config
from src.mcp.config import McpServerConfig
from src.mcp.runtime import ConnectedMcpServer, McpRuntimePool, McpSessionRuntime, McpToolBinding, _public_server_info
from src.mcp import runtime as mcp_runtime_module
from src.tools import create_default_registry


FIXTURE_SERVER = Path(__file__).parent / "fixtures" / "mcp_echo_server.py"


def _write_config(path: Path, *, required: bool = True) -> None:
    path.write_text(json.dumps({
        "mcpServers": {
            "echo server": {
                "command": sys.executable,
                "args": [str(FIXTURE_SERVER)],
                "required": required,
                "startup_timeout_sec": 15,
                "tool_timeout_sec": 15,
            }
        }
    }), encoding="utf-8")


@pytest.mark.asyncio
async def test_configuration_change_during_startup_retries_with_new_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_file = tmp_path / "mcp-race.json"
    config_file.write_text(json.dumps({
        "mcpServers": {"echo": {"command": "old-command"}}
    }), encoding="utf-8")
    monkeypatch.setattr(settings, "mcp_config_path", str(config_file))
    first_started = asyncio.Event()
    release_first = asyncio.Event()
    commands: list[str | None] = []

    async def controlled_start(runtime: McpSessionRuntime) -> None:
        commands.append(runtime.config.servers["echo"].command)
        if len(commands) == 1:
            first_started.set()
            await release_first.wait()

    monkeypatch.setattr(McpSessionRuntime, "start", controlled_start)
    pool = McpRuntimePool()
    get_task = asyncio.create_task(pool.get("race-session", str(tmp_path)))
    await first_started.wait()

    def write_new_config() -> None:
        config_file.write_text(json.dumps({
            "mcpServers": {"echo": {"command": "new-command"}}
        }), encoding="utf-8")

    await pool.apply_configuration(write_new_config)
    release_first.set()
    runtime = await get_task

    assert runtime is not None
    assert commands == ["old-command", "new-command"]
    assert runtime.config.servers["echo"].command == "new-command"
    await pool.shutdown()


@pytest.mark.asyncio
async def test_stale_startup_failure_retries_after_configuration_change(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_file = tmp_path / "mcp-stale-failure.json"
    config_file.write_text(json.dumps({
        "mcpServers": {"echo": {"command": "broken-command"}}
    }), encoding="utf-8")
    monkeypatch.setattr(settings, "mcp_config_path", str(config_file))
    first_started = asyncio.Event()
    release_first = asyncio.Event()

    async def controlled_start(runtime: McpSessionRuntime) -> None:
        if runtime.config.servers["echo"].command == "broken-command":
            first_started.set()
            await release_first.wait()
            raise RuntimeError("obsolete startup failure")

    monkeypatch.setattr(McpSessionRuntime, "start", controlled_start)
    pool = McpRuntimePool()
    get_task = asyncio.create_task(pool.get("stale-failure-session", str(tmp_path)))
    await first_started.wait()

    def write_fixed_config() -> None:
        config_file.write_text(json.dumps({
            "mcpServers": {"echo": {"command": "fixed-command"}}
        }), encoding="utf-8")

    await pool.apply_configuration(write_fixed_config)
    release_first.set()
    runtime = await get_task

    assert runtime is not None
    assert runtime.config.servers["echo"].command == "fixed-command"
    await pool.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_type", [RuntimeError, asyncio.CancelledError])
async def test_startup_discovery_failure_finishes_transport_exit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_type: type[BaseException],
) -> None:
    exit_state = {"started": False, "finished": False, "cancelled": False}

    class FailingClient:
        protocol_version = "test"
        server_info = None

        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args: object) -> None:
            exit_state["started"] = True
            try:
                await asyncio.sleep(0)
                exit_state["finished"] = True
            except asyncio.CancelledError:
                exit_state["cancelled"] = True
                raise

        async def list_tools(self, **_kwargs: object):
            raise failure_type("discovery failed")

    monkeypatch.setattr(mcp_runtime_module, "Client", FailingClient)
    server = ConnectedMcpServer(
        "failing",
        McpServerConfig(command=sys.executable),
        tmp_path,
    )

    with pytest.raises(failure_type, match="discovery failed"):
        await server.start()

    assert exit_state == {"started": True, "finished": True, "cancelled": False}
    assert server.client is None
    assert server._owner_task is None


@pytest.mark.asyncio
async def test_mcp_config_expands_environment_and_validates_transport(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PGAGENT_TEST_MCP_TOKEN", "secret-value")
    config_file = tmp_path / "mcp.json"
    config_file.write_text(json.dumps({
        "mcpServers": {
            "remote": {
                "url": "https://example.com/mcp",
                "headers": {"Authorization": "Bearer ${PGAGENT_TEST_MCP_TOKEN}"},
            }
        }
    }), encoding="utf-8")

    config = load_mcp_config(config_file)

    assert config.servers["remote"].headers == {"Authorization": "Bearer secret-value"}
    assert config.servers["remote"].transport == "streamable_http"
    monkeypatch.setattr(settings, "mcp_config_path", str(config_file))
    public_status = await mcp_status()
    assert "secret-value" not in json.dumps(public_status)
    assert public_status["configured_servers"] == [{
        "name": "remote",
        "enabled": True,
        "required": False,
        "transport": "streamable_http",
    }]


def test_disabled_server_does_not_expand_missing_environment(tmp_path: Path) -> None:
    config_file = tmp_path / "mcp.json"
    config_file.write_text(json.dumps({
        "mcpServers": {
            "disabled": {
                "url": "${MISSING_DISABLED_URL}",
                "headers": {"Authorization": "Bearer ${MISSING_DISABLED_TOKEN}"},
                "enabled": False,
            }
        }
    }), encoding="utf-8")

    config = load_mcp_config(config_file)

    assert config.servers["disabled"].enabled is False
    assert config.servers["disabled"].url == "${MISSING_DISABLED_URL}"
    assert config.servers["disabled"].headers["Authorization"] == "Bearer ${MISSING_DISABLED_TOKEN}"


@pytest.mark.asyncio
async def test_invalid_config_status_does_not_echo_expanded_secret(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PGAGENT_INVALID_MCP_SECRET", "TOP-SECRET-DO-NOT-LEAK")
    config_file = tmp_path / "mcp.json"
    config_file.write_text(json.dumps({
        "mcpServers": {
            "invalid": {
                "url": "https://example.com/mcp",
                "headers": "${PGAGENT_INVALID_MCP_SECRET}",
            }
        }
    }), encoding="utf-8")
    monkeypatch.setattr(settings, "mcp_config_path", str(config_file))

    with pytest.raises(HTTPException) as captured:
        await mcp_status()

    assert captured.value.status_code == 422
    assert "TOP-SECRET-DO-NOT-LEAK" not in json.dumps(captured.value.detail)


@pytest.mark.asyncio
async def test_optional_stdio_failure_does_not_hide_ready_server(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config_file = tmp_path / "mcp.json"
    config_file.write_text(json.dumps({
        "mcpServers": {
            "broken": {
                "command": str(tmp_path / "missing-command.exe"),
                "required": False,
                "startup_timeout_sec": 2,
            },
            "echo": {
                "command": sys.executable,
                "args": [str(FIXTURE_SERVER)],
                "required": True,
                "startup_timeout_sec": 15,
            },
        }
    }), encoding="utf-8")
    monkeypatch.setattr(settings, "mcp_config_path", str(config_file))
    registry = create_default_registry(str(tmp_path), allowed_tool_names=["MCP"], permission_mode="full")

    await attach_mcp_tools(registry, session_key="mcp-optional-session", workspace_root=str(tmp_path))

    assert "mcp__echo__echo" in registry.enabled_tool_names
    statuses = mcp_runtime_pool.statuses()[0]["servers"]
    assert {item["name"]: item["status"] for item in statuses} == {"broken": "failed", "echo": "ready"}
    await mcp_runtime_pool.close_session("mcp-optional-session")


@pytest.mark.asyncio
async def test_failed_optional_server_reconnects_on_next_attachment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_file = tmp_path / "mcp.json"
    config_file.write_text(json.dumps({
        "mcpServers": {
            "retry": {
                "command": "retry-command",
                "required": False,
                "startup_timeout_sec": 2,
            }
        }
    }), encoding="utf-8")
    monkeypatch.setattr(settings, "mcp_config_path", str(config_file))
    attempts = 0

    async def controlled_start(server: ConnectedMcpServer) -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise TimeoutError("first install exceeded startup timeout")
        server.client = object()  # type: ignore[assignment]
        server.tools = [SimpleNamespace(
            name="browser_navigate",
            description="Navigate the browser",
            input_schema={"type": "object", "properties": {"url": {"type": "string"}}},
            annotations=SimpleNamespace(read_only_hint=False),
        )]

    async def controlled_refresh(server: ConnectedMcpServer) -> None:
        return None

    async def controlled_close(server: ConnectedMcpServer) -> None:
        server.client = None

    monkeypatch.setattr(ConnectedMcpServer, "start", controlled_start)
    monkeypatch.setattr(ConnectedMcpServer, "refresh_tools", controlled_refresh)
    monkeypatch.setattr(ConnectedMcpServer, "close", controlled_close)
    pool = McpRuntimePool()
    monkeypatch.setattr("src.mcp.integration.mcp_runtime_pool", pool)

    first = create_default_registry(str(tmp_path), allowed_tool_names=["MCP"], permission_mode="full")
    await attach_mcp_tools(first, session_key="retry-session", workspace_root=str(tmp_path))
    assert "mcp__retry__browser_navigate" not in first.enabled_tool_names

    second = create_default_registry(str(tmp_path), allowed_tool_names=["MCP"], permission_mode="full")
    await attach_mcp_tools(second, session_key="retry-session", workspace_root=str(tmp_path))

    assert attempts == 2
    assert "mcp__retry__browser_navigate" in second.enabled_tool_names
    assert pool.statuses()[0]["servers"][0]["status"] == "ready"
    await pool.shutdown()


@pytest.mark.asyncio
async def test_mcp_attachment_reports_connection_progress(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config_file = tmp_path / "mcp.json"
    _write_config(config_file)
    monkeypatch.setattr(settings, "mcp_config_path", str(config_file))
    registry = create_default_registry(str(tmp_path), allowed_tool_names=["MCP"], permission_mode="full")
    events: list[dict] = []

    await attach_mcp_tools(
        registry,
        session_key="mcp-progress-session",
        workspace_root=str(tmp_path),
        progress_sink=events.append,
    )

    assert events[0] == {"type": "mcp_connecting", "servers": ["echo server"]}
    assert events[-1]["type"] == "mcp_ready"
    assert events[-1]["tool_count"] == 3
    await mcp_runtime_pool.close_session("mcp-progress-session")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("bindings", "server_statuses", "expected_type"),
    [
        ([], ["ready"], "mcp_ready"),
        ([McpToolBinding("ok", "echo", "mcp__ok__echo", "echo", {"type": "object"}, True, True)], ["ready", "failed"], "mcp_degraded"),
    ],
)
async def test_mcp_progress_uses_connection_health_not_tool_count(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    bindings: list[McpToolBinding],
    server_statuses: list[str],
    expected_type: str,
) -> None:
    config_file = tmp_path / "mcp.json"
    _write_config(config_file)
    monkeypatch.setattr(settings, "mcp_config_path", str(config_file))

    class FakeRuntime:
        async def refresh_tools(self, *, reconnect_failed: bool = False) -> list[McpToolBinding]:
            return bindings

        def status(self) -> dict:
            return {
                "servers": [
                    {"name": f"server-{index}", "status": status, "error_code": "TimeoutError" if status != "ready" else None}
                    for index, status in enumerate(server_statuses)
                ]
            }

    class FakePool:
        async def acquire(self, _session_key: str, _workspace_root: str):
            return FakeRuntime(), False

    monkeypatch.setattr("src.mcp.integration.mcp_runtime_pool", FakePool())
    registry = create_default_registry(str(tmp_path), allowed_tool_names=["MCP"], permission_mode="full")
    events: list[dict] = []

    await attach_mcp_tools(
        registry,
        session_key="health-session",
        workspace_root=str(tmp_path),
        progress_sink=events.append,
    )

    assert events[-1]["type"] == expected_type
    assert events[-1]["tool_count"] == len(bindings)


def test_public_server_info_only_exposes_name_and_version() -> None:
    assert _public_server_info({
        "name": "fixture",
        "version": "1.2.3",
        "instructions": "server controlled text",
    }) == {"name": "fixture", "version": "1.2.3"}


@pytest.mark.asyncio
async def test_stdio_mcp_discovery_approval_call_and_shutdown(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config_file = tmp_path / "mcp.json"
    _write_config(config_file)
    monkeypatch.setattr(settings, "mcp_config_path", str(config_file))
    registry = create_default_registry(
        str(tmp_path),
        allowed_tool_names=["MCP", "ListMcpResources", "ReadMcpResource"],
        permission_mode="smart",
    )

    await attach_mcp_tools(
        registry,
        session_key="mcp-test-session",
        workspace_root=str(tmp_path),
    )

    dynamic_names = [name for name in registry.enabled_tool_names if name.startswith("mcp__")]
    assert dynamic_names == [
        "mcp__echo_server__cwd",
        "mcp__echo_server__echo",
        "mcp__echo_server__remember",
    ]
    assert {item["function"]["name"] for item in registry.schemas} >= set(dynamic_names)

    echo_result = await registry.execute_async("mcp__echo_server__echo", {"text": "hello"})
    assert echo_result.ok is True
    assert echo_result.content == "echo:hello"
    assert echo_result.metadata["mcp_server"] == "echo server"

    pending = await registry.execute_async("mcp__echo_server__remember", {"value": "state"})
    assert pending.approval_required is True
    approved = await registry.execute_async("mcp__echo_server__remember", {"value": "state"}, approved=True)
    assert approved.ok is True
    assert approved.changed is True
    assert approved.content == "remembered:state"

    status = mcp_runtime_pool.statuses()[0]
    assert status["servers"][0]["status"] == "ready"
    assert status["servers"][0]["tool_count"] == 3

    await mcp_runtime_pool.close_session("mcp-test-session")
    assert mcp_runtime_pool.statuses() == []


@pytest.mark.asyncio
async def test_same_session_uses_distinct_stdio_runtime_per_workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config_file = tmp_path / "mcp.json"
    _write_config(config_file)
    monkeypatch.setattr(settings, "mcp_config_path", str(config_file))
    parent_root = tmp_path / "parent"
    child_root = tmp_path / "worktree"
    parent_root.mkdir()
    child_root.mkdir()

    parent = create_default_registry(str(parent_root), allowed_tool_names=["MCP"], permission_mode="full")
    child = create_default_registry(str(child_root), allowed_tool_names=["MCP"], permission_mode="full")
    await attach_mcp_tools(parent, session_key="shared-session", workspace_root=str(parent_root))
    await attach_mcp_tools(child, session_key="shared-session", workspace_root=str(child_root))

    parent_cwd = await parent.execute_async("mcp__echo_server__cwd", {})
    child_cwd = await child.execute_async("mcp__echo_server__cwd", {})
    assert Path(parent_cwd.content) == parent_root
    assert Path(child_cwd.content) == child_root
    assert len(mcp_runtime_pool.statuses()) == 2

    await mcp_runtime_pool.close_session("shared-session")
    assert mcp_runtime_pool.statuses() == []


@pytest.mark.asyncio
async def test_frozen_mcp_catalog_rejects_definition_drift(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config_file = tmp_path / "mcp.json"
    _write_config(config_file)
    monkeypatch.setattr(settings, "mcp_config_path", str(config_file))
    registry = create_default_registry(str(tmp_path), allowed_tool_names=["MCP"], permission_mode="full")
    await attach_mcp_tools(registry, session_key="mcp-frozen-session", workspace_root=str(tmp_path))
    frozen = registry.runtime_state()["mcp_tools"]
    frozen[0]["description"] = "different definition"

    resumed = create_default_registry(str(tmp_path), allowed_tool_names=["MCP"], permission_mode="full")
    with pytest.raises(RuntimeError, match="catalog changed"):
        await attach_mcp_tools(
            resumed,
            session_key="mcp-frozen-session",
            workspace_root=str(tmp_path),
            frozen_tools=frozen,
        )
    await mcp_runtime_pool.close_session("mcp-frozen-session")
