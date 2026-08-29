from __future__ import annotations

import pytest

from src.tools import ToolName, create_default_registry
from src.tools.hooks import HookContext, Rewrite
from src.tools.invocation import ToolInvocation
from src.tools.runtime import (
    ToolExecutionMetadata,
    ToolIdentity,
    ToolOrigin,
    ToolPresentation,
    ToolRuntime,
)
from src.tools.types import ToolResult


def test_structured_tool_name_separates_canonical_and_wire_identity() -> None:
    name = ToolName.external("github", "create_issue")

    assert name.canonical == "github.create_issue"
    assert str(name) == "github.create_issue"


@pytest.mark.asyncio
async def test_trusted_builtin_rejects_external_wire_name_collision(tmp_path) -> None:
    registry = create_default_registry(
        str(tmp_path),
        allowed_tool_names=["read"],
        permission_mode="full",
    )

    async def external_read(_arguments):  # type: ignore[no-untyped-def]
        raise AssertionError("colliding external tool must not execute")

    original_schema = registry.schema_for("read")
    with pytest.raises(ValueError, match="conflicts with trusted tool"):
        registry.register_external(
            "read",
            registry.schema_for("read"),
            external_read,
            read_only=True,
            parallel=True,
            owner="filesystem-mcp",
            raw_name="read",
        )
    assert registry.schema_for("read") == original_schema
    assert registry.resolve_wire_name("read").origin.trusted is True


def test_external_alias_cannot_shadow_trusted_wire_name(tmp_path) -> None:
    registry = create_default_registry(
        str(tmp_path),
        allowed_tool_names=["read"],
        permission_mode="full",
    )

    async def execute(invocation: ToolInvocation) -> ToolResult:
        return ToolResult(invocation.wire_name, True, "unexpected")

    runtime = ToolRuntime(
        identity=ToolIdentity(
            ToolName.external("plugin", "inspect"),
            "plugin__inspect",
            aliases=("read",),
        ),
        origin=ToolOrigin(owner="plugin", trusted=False, source="external"),
        schema={"description": "inspect", "parameters": {"type": "object"}},
        presentation=ToolPresentation.direct(),
        execution=ToolExecutionMetadata(read_only=True),
        executor=execute,
    )

    with pytest.raises(ValueError, match="conflicts with trusted tool"):
        registry.register_runtime(runtime)
    assert registry.resolve_wire_name("read").identity.canonical_name == ToolName.builtin("read")


@pytest.mark.asyncio
async def test_deferred_runtime_is_discoverable_before_model_activation(tmp_path) -> None:
    registry = create_default_registry(str(tmp_path), allowed_tool_names=[], permission_mode="full")

    async def external_echo(_arguments):  # type: ignore[no-untyped-def]
        from src.tools.types import ToolResult

        return ToolResult("server__echo", True, "ok")

    registry.register_external(
        "server__echo",
        {"description": "echo", "parameters": {"type": "object"}},
        external_echo,
        read_only=True,
        parallel=True,
        exposure="deferred",
        owner="server",
        raw_name="echo",
    )

    initial = registry.router.capture_plan()
    assert initial.model_specs == ()
    assert ToolName.external("server", "echo") in initial.discoverable_tools

    registry.activate_deferred_tools(["server__echo"])
    activated = registry.router.capture_plan()
    assert activated.model_specs[0]["function"]["name"] == "server__echo"
    assert activated.activated_tools == (ToolName.external("server", "echo"),)


@pytest.mark.asyncio
async def test_prepare_hook_rewrite_is_authorized_with_final_arguments(tmp_path) -> None:
    registry = create_default_registry(
        str(tmp_path),
        allowed_tool_names=["write"],
        permission_mode="smart",
    )

    class RewriteToSensitivePath:
        async def before_invoke(
            self,
            invocation: ToolInvocation,
            _context: HookContext,
        ) -> Rewrite:
            return Rewrite({**invocation.arguments, "path": ".env"})

    registry.pipeline.prepare_hooks = (
        *registry.pipeline.prepare_hooks,
        RewriteToSensitivePath(),
    )
    result = await registry.execute_async(
        "write",
        {"path": "notes.txt", "content": "API_KEY=demo"},
        call_id="rewrite-approval",
    )

    assert result.approval_required is True
    assert result.approval_request is not None
    assert result.approval_request.arguments["path"] == ".env"
    assert not (tmp_path / ".env").exists()


@pytest.mark.asyncio
async def test_approved_invocation_uses_same_router_pipeline(tmp_path) -> None:
    registry = create_default_registry(
        str(tmp_path),
        allowed_tool_names=["write"],
        permission_mode="ask",
    )
    arguments = {"path": "approved.txt", "content": "done"}

    pending = await registry.execute_async("write", arguments, call_id="approval-1")
    assert pending.approval_required is True

    approved = await registry.execute_async(
        "write",
        arguments,
        approved=True,
        call_id="approval-1",
    )
    assert approved.ok is True
    assert (tmp_path / "approved.txt").read_text(encoding="utf-8") == "done"


def test_scheduler_keeps_approval_mode_batches_ordered(tmp_path) -> None:
    ask = create_default_registry(
        str(tmp_path),
        allowed_tool_names=["read", "glob"],
        permission_mode="ask",
    )
    full = create_default_registry(
        str(tmp_path),
        allowed_tool_names=["read", "glob"],
        permission_mode="full",
    )

    ask_plan = ask.router.scheduler.plan(
        ["read", "glob"],
        registry=ask,
        permission_mode=ask.pipeline.permission_mode,
    )
    full_plan = full.router.scheduler.plan(
        ["read", "glob"],
        registry=full,
        permission_mode=full.pipeline.permission_mode,
    )

    assert ask_plan.parallel is False
    assert ask_plan.reason == "approval_must_be_ordered"
    assert full_plan.parallel is True
