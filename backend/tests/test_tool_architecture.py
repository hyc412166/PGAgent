"""验证工具规范名与线名隔离、可信工具防碰撞、延迟发现、参数改写授权和审批后统一路由。

测试通过 fixture 或辅助函数准备隔离环境，再调用真实服务、路由或运行时，并检查返回值、持久化状态与可观察副作用。
变量约定：tmp_path/monkeypatch 提供隔离环境，client/store/runtime 驱动被测链路，各类 *_id 串联持久化实体，payload 表示输入，response/result 表示实际输出，expected 表示期望值。
"""

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


# 测试场景：验证该正常业务场景从输入准备到结果断言的完整链路；函数名 test_structured_tool_name_separates_canonical_and_wire_identity 精确标识本用例的具体条件。
def test_structured_tool_name_separates_canonical_and_wire_identity() -> None:
    name = ToolName.external("github", "create_issue")

    assert name.canonical == "github.create_issue"
    assert str(name) == "github.create_issue"


@pytest.mark.asyncio
# 测试场景：验证非法、越界或不满足前置条件的操作会被明确拒绝，且不会产生错误状态；函数名 test_trusted_builtin_rejects_external_wire_name_collision 精确标识本用例的具体条件。
async def test_trusted_builtin_rejects_external_wire_name_collision(tmp_path) -> None:
    registry = create_default_registry(
        str(tmp_path),
        allowed_tool_names=["read"],
        permission_mode="full",
    )

    # 辅助方法：external_read 实现测试替身在此调用阶段需要的最小行为。
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


# 测试场景：验证非法、越界或不满足前置条件的操作会被明确拒绝，且不会产生错误状态；函数名 test_external_alias_cannot_shadow_trusted_wire_name 精确标识本用例的具体条件。
def test_external_alias_cannot_shadow_trusted_wire_name(tmp_path) -> None:
    registry = create_default_registry(
        str(tmp_path),
        allowed_tool_names=["read"],
        permission_mode="full",
    )

    # 辅助方法：execute 实现测试替身在此调用阶段需要的最小行为。
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
# 测试场景：验证该正常业务场景从输入准备到结果断言的完整链路；函数名 test_deferred_runtime_is_discoverable_before_model_activation 精确标识本用例的具体条件。
async def test_deferred_runtime_is_discoverable_before_model_activation(tmp_path) -> None:
    registry = create_default_registry(str(tmp_path), allowed_tool_names=[], permission_mode="full")

    # 辅助方法：external_echo 实现测试替身在此调用阶段需要的最小行为。
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
# 测试场景：验证该正常业务场景从输入准备到结果断言的完整链路；函数名 test_prepare_hook_rewrite_is_authorized_with_final_arguments 精确标识本用例的具体条件。
async def test_prepare_hook_rewrite_is_authorized_with_final_arguments(tmp_path) -> None:
    registry = create_default_registry(
        str(tmp_path),
        allowed_tool_names=["write"],
        permission_mode="smart",
    )

    # 测试替身类：RewriteToSensitivePath 保存该局部场景的可控状态。
    class RewriteToSensitivePath:
        # 辅助方法：before_invoke 实现测试替身在此调用阶段需要的最小行为。
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
# 测试场景：验证接口或资源生命周期操作会返回正确结果并同步持久化状态；函数名 test_approved_invocation_uses_same_router_pipeline 精确标识本用例的具体条件。
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


# 测试场景：验证并发或批量执行时的顺序、隔离性和最终状态一致性；函数名 test_scheduler_keeps_approval_mode_batches_ordered 精确标识本用例的具体条件。
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
