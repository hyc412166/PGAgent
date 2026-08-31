from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from src.config import settings
from src.agent.engine import AgentRuntime, ModelToolCall, ModelTurn
from src.api.mcp import mcp_status
from src.mcp import attach_mcp_tools, mcp_runtime_pool
from src.mcp.config import McpConfig, load_mcp_config
from src.mcp.config import McpServerConfig
from src.mcp.runtime import ConnectedMcpServer, McpRuntimePool, McpSessionRuntime, McpToolBinding, _public_server_info
from src.mcp.tool_catalog_cache import CachedMcpTool, McpToolCatalogCache, ToolCatalogIdentity
from src.mcp import runtime as mcp_runtime_module
from src.tools import create_default_registry


FIXTURE_SERVER = Path(__file__).parent / "fixtures" / "mcp_echo_server.py"


def test_mcp_tool_timeout_defaults_to_five_minutes() -> None:
    config = McpServerConfig(command="fixture")
    assert config.tool_timeout_sec == 300


@pytest.mark.asyncio
async def test_mcp_runtime_owns_one_tool_timeout_and_cancels_the_call(tmp_path: Path) -> None:
    config = McpConfig(mcpServers={
        "slow": McpServerConfig(command="fixture", tool_timeout_sec=0.02),
    })
    runtime = McpSessionRuntime("timeout", tmp_path, config, McpToolCatalogCache())
    cancelled = asyncio.Event()
    observed_sdk_timeout: list[float | None] = []

    class FakeClient:
        async def call_tool(
            self,
            _name: str,
            _arguments: dict,
            *,
            read_timeout_seconds: float | None,
        ) -> object:
            observed_sdk_timeout.append(read_timeout_seconds)
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
                raise

    server = ConnectedMcpServer("slow", config.servers["slow"], tmp_path)
    runtime.servers["slow"] = server
    server.client = FakeClient()  # type: ignore[assignment]
    server.ready = True

    result = await runtime.call_tool("slow", "echo", {})

    assert result.ok is False
    assert result.error_code == "mcp_timeout"
    assert observed_sdk_timeout == [None]
    assert cancelled.is_set()


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
async def test_runtime_pool_identity_includes_startup_policy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_file = tmp_path / "mcp-policy.json"
    config_file.write_text(json.dumps({
        "mcpServers": {"echo": {"command": "fixture"}}
    }), encoding="utf-8")
    monkeypatch.setattr(settings, "mcp_config_path", str(config_file))

    async def controlled_start(_runtime: McpSessionRuntime) -> None:
        return None

    monkeypatch.setattr(McpSessionRuntime, "start", controlled_start)
    pool = McpRuntimePool()
    eager, eager_created = await pool.acquire(
        "owner",
        str(tmp_path),
        runtime_scope="same-scope",
        startup_policy="eager",
    )
    lazy, lazy_created = await pool.acquire(
        "owner",
        str(tmp_path),
        runtime_scope="same-scope",
        startup_policy="lazy_when_cached",
    )

    assert eager_created is True
    assert lazy_created is True
    assert eager is not lazy
    assert {item["startup_policy"] for item in pool.statuses()} == {"eager", "lazy_when_cached"}
    await pool.close_session("owner")
    assert pool.statuses() == []


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
    assert {item["name"]: item["catalog_source"] for item in statuses} == {
        "broken": "unavailable",
        "echo": "live",
    }
    await mcp_runtime_pool.close_session("mcp-optional-session")


@pytest.mark.asyncio
async def test_session_selection_only_starts_selected_mcp_servers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_file = tmp_path / "mcp.json"
    config_file.write_text(json.dumps({
        "mcpServers": {
            "unused": {"command": str(tmp_path / "must-not-start.exe"), "required": True},
            "echo server": {
                "command": sys.executable,
                "args": [str(FIXTURE_SERVER)],
                "required": True,
                "startup_timeout_sec": 15,
            },
        }
    }), encoding="utf-8")
    monkeypatch.setattr(settings, "mcp_config_path", str(config_file))
    registry = create_default_registry(str(tmp_path), allowed_tool_names=["MCP"], permission_mode="full")

    await attach_mcp_tools(
        registry,
        session_key="selected-mcp-session",
        workspace_root=str(tmp_path),
        selected_server_names=["echo server"],
    )

    assert "mcp__echo_server__echo" in registry.enabled_tool_names
    assert [item["name"] for item in mcp_runtime_pool.statuses()[0]["servers"]] == ["echo server"]
    assert registry.runtime_state()["mcp_server_names"] == ["echo server"]
    await mcp_runtime_pool.close_session("selected-mcp-session")


@pytest.mark.asyncio
async def test_empty_session_selection_does_not_create_an_mcp_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_file = tmp_path / "mcp.json"
    _write_config(config_file)
    monkeypatch.setattr(settings, "mcp_config_path", str(config_file))
    registry = create_default_registry(str(tmp_path), allowed_tool_names=["MCP"], permission_mode="full")

    await attach_mcp_tools(
        registry,
        session_key="no-mcp-session",
        workspace_root=str(tmp_path),
        selected_server_names=[],
    )

    assert "McpToolSearch" not in registry.enabled_tool_names
    assert not any(status["session_key"] == "no-mcp-session" for status in mcp_runtime_pool.statuses())


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

    assert events[0] == {"type": "mcp_catalog_loading", "servers": ["echo server"]}
    assert events[-1]["type"] == "mcp_ready"
    assert events[-1]["tool_count"] == 3
    assert events[-1]["connected_servers"] == ["echo server"]
    assert events[-1]["dormant_servers"] == []
    await mcp_runtime_pool.close_session("mcp-progress-session")


@pytest.mark.asyncio
async def test_warm_catalog_keeps_optional_stdio_server_dormant_until_tool_call(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_file = tmp_path / "mcp.json"
    _write_config(config_file, required=False)
    monkeypatch.setattr(settings, "mcp_config_path", str(config_file))

    cold = create_default_registry(str(tmp_path), allowed_tool_names=["MCP"], permission_mode="full")
    await attach_mcp_tools(cold, session_key="cold-session", workspace_root=str(tmp_path))
    assert mcp_runtime_pool.statuses()[0]["servers"][0]["status"] == "ready"
    await mcp_runtime_pool.close_session("cold-session")

    events: list[dict] = []
    warm = create_default_registry(str(tmp_path), allowed_tool_names=["MCP"], permission_mode="full")
    await attach_mcp_tools(
        warm,
        session_key="warm-session",
        workspace_root=str(tmp_path),
        agent_kind="subagent",
        runtime_scope="warm-child",
        progress_sink=events.append,
    )

    warm_status = mcp_runtime_pool.statuses()[0]["servers"][0]
    assert warm_status["status"] == "dormant"
    assert warm_status["catalog_source"] == "cache"
    assert warm_status["tool_count"] == 3
    assert events[-1]["dormant_servers"] == ["echo server"]

    search = await warm.execute_async("McpToolSearch", {"query": "echo text"})
    assert search.ok is True
    assert mcp_runtime_pool.statuses()[0]["servers"][0]["status"] == "dormant"

    result = await warm.execute_async("mcp__echo_server__echo", {"text": "lazy"})
    assert result.ok is True
    assert result.content == "echo:lazy"
    assert mcp_runtime_pool.statuses()[0]["servers"][0]["status"] == "ready"
    assert [event["type"] for event in events[-2:]] == ["mcp_connecting", "mcp_server_ready"]
    assert events[-2]["trigger"] == "tool_call"
    await mcp_runtime_pool.close_session("warm-session")


@pytest.mark.asyncio
async def test_main_and_subagent_own_distinct_runtimes_under_one_session(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_file = tmp_path / "mcp.json"
    _write_config(config_file, required=False)
    monkeypatch.setattr(settings, "mcp_config_path", str(config_file))

    main_registry = create_default_registry(str(tmp_path), allowed_tool_names=["MCP"], permission_mode="full")
    await attach_mcp_tools(
        main_registry,
        session_key="shared-owner",
        workspace_root=str(tmp_path),
        agent_kind="main",
    )

    child_registry = create_default_registry(str(tmp_path), allowed_tool_names=["MCP"], permission_mode="full")
    await attach_mcp_tools(
        child_registry,
        session_key="shared-owner",
        workspace_root=str(tmp_path),
        agent_kind="subagent",
        runtime_scope="child-run-1",
    )

    statuses = {item["runtime_scope"]: item for item in mcp_runtime_pool.statuses()}
    assert statuses["shared-owner"]["startup_policy"] == "eager"
    assert statuses["shared-owner"]["servers"][0]["status"] == "ready"
    assert statuses["child-run-1"]["startup_policy"] == "lazy_when_cached"
    assert statuses["child-run-1"]["servers"][0]["status"] == "dormant"

    await mcp_runtime_pool.close_session("shared-owner")
    assert mcp_runtime_pool.statuses() == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("startup_policy", "required", "expected_starts", "expected_status"),
    [("eager", False, 1, "ready"), ("lazy_when_cached", True, 0, "dormant")],
)
async def test_startup_policy_controls_warm_catalog_even_for_required_servers(
    tmp_path: Path,
    startup_policy: str,
    required: bool,
    expected_starts: int,
    expected_status: str,
) -> None:
    config = load_mcp_config(tmp_path / "missing.json").model_copy(update={
        "servers": {"cached": McpServerConfig(command="fixture", required=required)},
    })
    cache = McpToolCatalogCache()
    identity = ToolCatalogIdentity.create("cached", config.servers["cached"], tmp_path)
    cache.put(identity, (CachedMcpTool("echo", "Echo", {"type": "object"}, True),))
    runtime = McpSessionRuntime(
        "policy-session",
        tmp_path,
        config,
        cache,
        startup_policy=startup_policy,
    )
    starts = 0

    async def controlled_start(server: ConnectedMcpServer) -> None:
        nonlocal starts
        starts += 1
        server.client = object()  # type: ignore[assignment]
        server.tools = [SimpleNamespace(
            name="echo",
            description="Echo",
            input_schema={"type": "object"},
            annotations=SimpleNamespace(read_only_hint=True),
        )]

    original_start = ConnectedMcpServer.start
    ConnectedMcpServer.start = controlled_start
    try:
        await runtime.start()
    finally:
        ConnectedMcpServer.start = original_start

    assert starts == expected_starts
    assert runtime.status()["servers"][0]["status"] == expected_status
    await runtime.close()


def test_tool_catalog_cache_expires_and_evicts_lru_entries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = 100.0
    monkeypatch.setattr("src.mcp.tool_catalog_cache.time.monotonic", lambda: clock)
    cache = McpToolCatalogCache(capacity=2, ttl_sec=30)
    config = McpServerConfig(command="fixture")
    identities = [ToolCatalogIdentity.create(f"server-{index}", config, tmp_path) for index in range(3)]
    tools = (CachedMcpTool("echo", "Echo", {"type": "object"}, True),)

    cache.put(identities[0], tools)
    cache.put(identities[1], tools)
    assert cache.get(identities[0]) == tools
    cache.put(identities[2], tools)
    assert cache.get(identities[1]) is None

    clock = 131.0
    assert cache.get(identities[0]) is None


@pytest.mark.asyncio
async def test_changed_catalog_is_cached_but_not_called_with_frozen_schema(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = McpConfig(mcpServers={
        "changing": McpServerConfig(command="fixture", required=False),
    })
    cache = McpToolCatalogCache()
    description = "version one"

    class FakeContent:
        def model_dump(self, **_kwargs: object) -> dict[str, str]:
            return {"type": "text", "text": "ok"}

    class FakeClient:
        async def call_tool(self, *_args: object, **_kwargs: object) -> object:
            return SimpleNamespace(
                content=[FakeContent()],
                structured_content=None,
                is_error=False,
            )

    async def controlled_start(server: ConnectedMcpServer) -> None:
        server.dormant = False
        server.client = FakeClient()  # type: ignore[assignment]
        server.tools = [SimpleNamespace(
            name="echo",
            description=description,
            input_schema={"type": "object", "properties": {"text": {"type": "string"}}},
            annotations=SimpleNamespace(read_only_hint=True),
        )]

    async def controlled_close(server: ConnectedMcpServer) -> None:
        server.client = None

    monkeypatch.setattr(ConnectedMcpServer, "start", controlled_start)
    monkeypatch.setattr(ConnectedMcpServer, "close", controlled_close)

    cold = McpSessionRuntime("cold", tmp_path, config, cache)
    await cold.start()
    await cold.close()

    warm = McpSessionRuntime("warm", tmp_path, config, cache, startup_policy="lazy_when_cached")
    await warm.start()
    assert warm.status()["servers"][0]["status"] == "dormant"

    description = "version two"
    result = await warm.call_tool("changing", "echo", {"text": "hello"})

    assert result.ok is False
    assert result.error_code == "mcp_catalog_changed"
    assert warm.bindings()[0].description == "version two"

    warm.accept_current_catalog()
    next_run_result = await warm.call_tool("changing", "echo", {"text": "hello"})

    assert next_run_result.ok is True
    assert next_run_result.content == "ok"
    await warm.close()


@pytest.mark.asyncio
async def test_concurrent_warm_calls_share_one_server_start(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = McpConfig(mcpServers={
        "shared": McpServerConfig(command="fixture", required=False),
    })
    cache = McpToolCatalogCache()
    identity = ToolCatalogIdentity.create("shared", config.servers["shared"], tmp_path)
    cache.put(identity, (CachedMcpTool("echo", "Version one", {"type": "object"}, True),))
    runtime = McpSessionRuntime("concurrent", tmp_path, config, cache, startup_policy="lazy_when_cached")
    starts = 0
    tool_calls = 0
    client_published = asyncio.Event()
    release_discovery = asyncio.Event()

    class FakeClient:
        async def call_tool(self, *_args: object, **_kwargs: object) -> object:
            nonlocal tool_calls
            tool_calls += 1
            return SimpleNamespace(content=[], structured_content=None, is_error=False)

    async def controlled_start(server: ConnectedMcpServer) -> None:
        nonlocal starts
        starts += 1
        server.dormant = False
        server.client = FakeClient()  # type: ignore[assignment]
        client_published.set()
        await release_discovery.wait()
        server.tools = [SimpleNamespace(
            name="echo",
            description="Version two",
            input_schema={"type": "object"},
            annotations=SimpleNamespace(read_only_hint=True),
        )]

    async def controlled_close(server: ConnectedMcpServer) -> None:
        server.client = None

    monkeypatch.setattr(ConnectedMcpServer, "start", controlled_start)
    monkeypatch.setattr(ConnectedMcpServer, "close", controlled_close)
    await runtime.start()

    first_task = asyncio.create_task(runtime.call_tool("shared", "echo", {}))
    await client_published.wait()
    server = runtime.servers["shared"]
    assert server.client is not None
    assert server.ready is False

    second_task = asyncio.create_task(runtime.call_tool("shared", "echo", {}))
    await asyncio.sleep(0)

    assert second_task.done() is False

    release_discovery.set()
    first, second = await asyncio.gather(first_task, second_task)

    assert first.error_code == "mcp_catalog_changed"
    assert second.error_code == "mcp_catalog_changed"
    assert starts == 1
    assert tool_calls == 0
    await runtime.close()


@pytest.mark.asyncio
async def test_close_during_lazy_start_prevents_connection_and_tool_call(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = McpConfig(mcpServers={
        "closing": McpServerConfig(command="fixture", required=False),
    })
    cache = McpToolCatalogCache()
    identity = ToolCatalogIdentity.create("closing", config.servers["closing"], tmp_path)
    cache.put(identity, (CachedMcpTool("echo", "Echo", {"type": "object"}, True),))
    runtime = McpSessionRuntime("closing", tmp_path, config, cache, startup_policy="lazy_when_cached")
    startup_entered = asyncio.Event()
    release_startup = asyncio.Event()
    tool_calls = 0

    class FakeClient:
        async def call_tool(self, *_args: object, **_kwargs: object) -> object:
            nonlocal tool_calls
            tool_calls += 1
            return SimpleNamespace(content=[], structured_content=None, is_error=False)

    async def controlled_start(server: ConnectedMcpServer) -> None:
        server.dormant = False
        startup_entered.set()
        await release_startup.wait()
        server.client = FakeClient()  # type: ignore[assignment]
        server.tools = [SimpleNamespace(
            name="echo",
            description="Echo",
            input_schema={"type": "object"},
            annotations=SimpleNamespace(read_only_hint=True),
        )]

    monkeypatch.setattr(ConnectedMcpServer, "start", controlled_start)
    await runtime.start()
    server = runtime.servers["closing"]

    call_task = asyncio.create_task(runtime.call_tool("closing", "echo", {}))
    await startup_entered.wait()
    close_task = asyncio.create_task(runtime.close())
    await asyncio.sleep(0)
    release_startup.set()
    result, _ = await asyncio.gather(call_task, close_task)

    assert result.ok is False
    assert result.error_code == "mcp_not_connected"
    assert tool_calls == 0
    assert server.closed is True
    assert server.client is None
    assert server.ready is False


@pytest.mark.asyncio
async def test_approval_does_not_wake_cached_server_before_grant(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_file = tmp_path / "mcp.json"
    _write_config(config_file, required=False)
    monkeypatch.setattr(settings, "mcp_config_path", str(config_file))
    cold = create_default_registry(str(tmp_path), allowed_tool_names=["MCP"], permission_mode="full")
    await attach_mcp_tools(cold, session_key="approval-cold", workspace_root=str(tmp_path))
    await mcp_runtime_pool.close_session("approval-cold")

    warm = create_default_registry(str(tmp_path), allowed_tool_names=["MCP"], permission_mode="ask")
    await attach_mcp_tools(
        warm,
        session_key="approval-warm",
        workspace_root=str(tmp_path),
        agent_kind="subagent",
        runtime_scope="approval-child",
    )
    await warm.execute_async("McpToolSearch", {"query": "echo text"})

    pending = await warm.execute_async("mcp__echo_server__echo", {"text": "wait"})
    assert pending.approval_required is True
    assert mcp_runtime_pool.statuses()[0]["servers"][0]["status"] == "dormant"

    approved = await warm.execute_async("mcp__echo_server__echo", {"text": "go"}, approved=True)
    assert approved.ok is True
    assert approved.content == "echo:go"
    assert mcp_runtime_pool.statuses()[0]["servers"][0]["status"] == "ready"
    await mcp_runtime_pool.close_session("approval-warm")


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

        def accept_current_catalog(self) -> None:
            return None

        def status(self) -> dict:
            return {
                "servers": [
                    {"name": f"server-{index}", "status": status, "error_code": "TimeoutError" if status != "ready" else None}
                    for index, status in enumerate(server_statuses)
                ]
            }

    class FakePool:
        async def acquire(self, _session_key: str, _workspace_root: str, **_kwargs: object):
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
    initial_visible = {item["function"]["name"] for item in registry.schemas}
    assert "McpToolSearch" in initial_visible
    assert "MCP" not in initial_visible
    assert initial_visible.isdisjoint(dynamic_names)

    search_result = await registry.execute_async("McpToolSearch", {"query": "echo text"})
    assert search_result.ok is True
    search_payload = json.loads(search_result.content)
    assert [item["name"] for item in search_payload["activated_tools"]] == ["mcp__echo_server__echo"]
    assert "mcp__echo_server__echo" in {
        item["function"]["name"] for item in registry.schemas
    }

    empty_search = await registry.execute_async("McpToolSearch", {"query": "   "})
    assert empty_search.ok is False
    assert empty_search.error_code == "invalid_arguments"
    punctuation_search = await registry.execute_async("McpToolSearch", {"query": "!!!"})
    assert punctuation_search.ok is False
    assert punctuation_search.error_code == "invalid_arguments"

    echo_result = await registry.execute_async("mcp__echo_server__echo", {"text": "hello"})
    assert echo_result.ok is True
    assert echo_result.content == "echo:hello"
    assert echo_result.metadata["mcp_server"] == "echo server"

    await registry.execute_async("McpToolSearch", {"query": "remember state"})
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


@pytest.mark.asyncio
async def test_deferred_mcp_tool_exposure_survives_run_resume(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_file = tmp_path / "mcp.json"
    _write_config(config_file)
    monkeypatch.setattr(settings, "mcp_config_path", str(config_file))
    registry = create_default_registry(str(tmp_path), allowed_tool_names=["MCP"], permission_mode="full")
    await attach_mcp_tools(registry, session_key="mcp-resume-session", workspace_root=str(tmp_path))

    await registry.execute_async("McpToolSearch", {"query": "current working directory"})
    frozen = registry.runtime_state()
    assert frozen["mcp_active_tools"] == ["mcp__echo_server__cwd"]

    resumed = create_default_registry(str(tmp_path), allowed_tool_names=["MCP"], permission_mode="full")
    await attach_mcp_tools(
        resumed,
        session_key="mcp-resume-session",
        workspace_root=str(tmp_path),
        frozen_tools=frozen["mcp_tools"],
        frozen_active_tools=frozen["mcp_active_tools"],
    )

    visible = {item["function"]["name"] for item in resumed.schemas}
    assert "mcp__echo_server__cwd" in visible
    assert "mcp__echo_server__echo" not in visible
    await mcp_runtime_pool.close_session("mcp-resume-session")


@pytest.mark.asyncio
async def test_legacy_eager_snapshot_keeps_all_mcp_tools_visible_on_resume(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_file = tmp_path / "mcp.json"
    _write_config(config_file)
    monkeypatch.setattr(settings, "mcp_config_path", str(config_file))
    first = create_default_registry(str(tmp_path), allowed_tool_names=["MCP"], permission_mode="full")
    await attach_mcp_tools(first, session_key="mcp-legacy-session", workspace_root=str(tmp_path))
    legacy_catalog = first.runtime_state()["mcp_tools"]

    resumed = create_default_registry(str(tmp_path), allowed_tool_names=["MCP"], permission_mode="full")
    await attach_mcp_tools(
        resumed,
        session_key="mcp-legacy-session",
        workspace_root=str(tmp_path),
        frozen_tools=legacy_catalog,
    )

    visible = {item["function"]["name"] for item in resumed.schemas}
    assert {item["model_name"] for item in legacy_catalog}.issubset(visible)
    await mcp_runtime_pool.close_session("mcp-legacy-session")


@pytest.mark.asyncio
async def test_agent_loads_deferred_mcp_tool_on_the_turn_after_search(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_file = tmp_path / "mcp.json"
    _write_config(config_file)
    monkeypatch.setattr(settings, "mcp_config_path", str(config_file))
    registry = create_default_registry(str(tmp_path), allowed_tool_names=["MCP"], permission_mode="full")
    await attach_mcp_tools(registry, session_key="mcp-agent-session", workspace_root=str(tmp_path))
    visible_by_turn: list[set[str]] = []

    async def model_call(**kwargs) -> ModelTurn:
        visible = {item["function"]["name"] for item in kwargs["tools"]}
        visible_by_turn.append(visible)
        if len(visible_by_turn) == 1:
            assert "McpToolSearch" in visible
            assert "mcp__echo_server__echo" not in visible
            rendered_context = "\n".join(str(item.get("content") or "") for item in kwargs["messages"])
            assert "mcp__echo_server: 3 tools" in rendered_context
            assert "Use McpToolSearch" in rendered_context
            return ModelTurn(tool_calls=[
                ModelToolCall("search-1", "McpToolSearch", {"query": "echo text"}),
            ])
        if len(visible_by_turn) == 2:
            assert "mcp__echo_server__echo" in visible
            return ModelTurn(tool_calls=[
                ModelToolCall("echo-1", "mcp__echo_server__echo", {"text": "hello"}),
            ])
        assert any("echo:hello" in str(item.get("content") or "") for item in kwargs["messages"])
        return ModelTurn(content="done")

    outcome = await AgentRuntime(model_call=model_call, tool_registry=registry).run(
        system_prompt="safe",
        recent_messages=[{"role": "user", "content": "echo hello with the MCP server"}],
    )

    assert outcome.status == "completed"
    assert outcome.tool_calls == 2
    assert len(visible_by_turn) == 3
    await mcp_runtime_pool.close_session("mcp-agent-session")


@pytest.mark.asyncio
async def test_same_turn_search_cannot_execute_a_newly_activated_tool(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_file = tmp_path / "mcp.json"
    _write_config(config_file)
    monkeypatch.setattr(settings, "mcp_config_path", str(config_file))
    registry = create_default_registry(str(tmp_path), allowed_tool_names=["MCP"], permission_mode="full")
    await attach_mcp_tools(registry, session_key="mcp-same-turn-session", workspace_root=str(tmp_path))
    calls = 0

    async def model_call(**kwargs) -> ModelTurn:
        nonlocal calls
        calls += 1
        if calls == 1:
            return ModelTurn(tool_calls=[
                ModelToolCall("search-1", "McpToolSearch", {"query": "echo text"}),
                ModelToolCall("echo-1", "mcp__echo_server__echo", {"text": "too early"}),
            ])
        tool_messages = [item for item in kwargs["messages"] if item.get("role") == "tool"]
        assert any("tool_not_offered" in str(item.get("content") or "") for item in tool_messages)
        assert "mcp__echo_server__echo" in {
            item["function"]["name"] for item in kwargs["tools"]
        }
        return ModelTurn(content="done")

    outcome = await AgentRuntime(model_call=model_call, tool_registry=registry).run(
        system_prompt="safe",
        recent_messages=[{"role": "user", "content": "echo with MCP"}],
    )

    assert outcome.status == "completed"
    assert calls == 2
    await mcp_runtime_pool.close_session("mcp-same-turn-session")


@pytest.mark.asyncio
async def test_hidden_generic_mcp_call_is_rejected_when_not_offered(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_file = tmp_path / "mcp.json"
    _write_config(config_file)
    monkeypatch.setattr(settings, "mcp_config_path", str(config_file))
    registry = create_default_registry(str(tmp_path), allowed_tool_names=["MCP"], permission_mode="full")
    await attach_mcp_tools(registry, session_key="mcp-hidden-session", workspace_root=str(tmp_path))
    calls = 0

    async def model_call(**kwargs) -> ModelTurn:
        nonlocal calls
        calls += 1
        if calls == 1:
            return ModelTurn(tool_calls=[
                ModelToolCall("generic-1", "MCP", {"server": "echo server", "tool": "echo", "arguments": {}}),
            ])
        assert any("tool_not_offered" in str(item.get("content") or "") for item in kwargs["messages"])
        return ModelTurn(content="done")

    outcome = await AgentRuntime(model_call=model_call, tool_registry=registry).run(
        system_prompt="safe",
        recent_messages=[{"role": "user", "content": "guess a hidden tool"}],
    )

    assert outcome.status == "completed"
    await mcp_runtime_pool.close_session("mcp-hidden-session")


@pytest.mark.asyncio
async def test_mcp_search_is_local_and_approval_exempt_in_ask_mode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_file = tmp_path / "mcp.json"
    _write_config(config_file)
    monkeypatch.setattr(settings, "mcp_config_path", str(config_file))
    registry = create_default_registry(str(tmp_path), allowed_tool_names=["MCP"], permission_mode="ask")
    await attach_mcp_tools(registry, session_key="mcp-ask-session", workspace_root=str(tmp_path))

    search = await registry.execute_async("McpToolSearch", {"query": "echo text"})
    assert search.ok is True
    assert search.approval_required is False
    concrete = await registry.execute_async("mcp__echo_server__echo", {"text": "hello"})
    assert concrete.approval_required is True
    await mcp_runtime_pool.close_session("mcp-ask-session")


@pytest.mark.asyncio
async def test_mcp_capability_is_hidden_when_no_server_is_enabled(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_file = tmp_path / "mcp.json"
    config_file.write_text(json.dumps({"mcpServers": {}}), encoding="utf-8")
    monkeypatch.setattr(settings, "mcp_config_path", str(config_file))
    registry = create_default_registry(str(tmp_path), allowed_tool_names=["MCP"], permission_mode="full")

    await attach_mcp_tools(registry, session_key="mcp-empty-session", workspace_root=str(tmp_path))

    assert "MCP" not in {item["function"]["name"] for item in registry.schemas}
    assert "McpToolSearch" not in registry.enabled_tool_names
