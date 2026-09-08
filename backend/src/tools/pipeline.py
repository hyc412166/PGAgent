"""Single invocation lifecycle for authorization, hooks and execution."""
# 文件职责：负责工具定义、授权、注册、调度与执行中的 pipeline 子模块。
# 逻辑关系：上层通过 tools/pipeline.py 使用本模块；本模块把处理结果交给同领域服务、持久化层或 API 响应层。

from __future__ import annotations

import asyncio
from collections.abc import Sequence

from .authorization import AuthorizationHook
from .hooks import (
    Continue,
    HookContext,
    LifecycleHook,
    PostHook,
    PrepareHook,
    Reject,
    RequireApproval,
    Rewrite,
)
from .invocation import (
    AwaitingApproval,
    Cancelled,
    DispatchFailed,
    Executed,
    Rejected,
    ToolDispatchOutcome,
    ToolInvocation,
)
from .runtime import ToolRuntime
from .types import ApprovalRequest, ToolResult


# 类职责：定义 InvocationPipeline 在本领域中的数据与行为。
class InvocationPipeline:
    # 函数职责：初始化实例依赖与初始状态。
    # 参数关系：workspace_root 表示当前步骤使用的 workspace_root 值；permission_mode 表示当前步骤使用的 permission_mode 值；prepare_hooks 表示当前流程使用的 prepare_hooks 集合；authorization_hook 表示当前步骤使用的 authorization_hook 值；post_hooks 表示当前流程使用的 post_hooks 集合；lifecycle_hooks 表示当前流程使用的 lifecycle_hooks 集合。
    # 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
    def __init__(
        self,
        *,
        workspace_root: str,
        permission_mode: str,
        prepare_hooks: Sequence[PrepareHook] = (),
        authorization_hook: AuthorizationHook | None = None,
        post_hooks: Sequence[PostHook] = (),
        lifecycle_hooks: Sequence[LifecycleHook] = (),
    ) -> None:
        # 变量说明：workspace_root 表示当前步骤使用的 workspace_root 值。
        self.workspace_root = workspace_root
        # 变量说明：permission_mode 表示当前步骤使用的 permission_mode 值。
        self.permission_mode = permission_mode
        # 变量说明：prepare_hooks 表示当前流程使用的 prepare_hooks 集合。
        self.prepare_hooks = tuple(prepare_hooks)
        # 变量说明：authorization_hook 表示当前步骤使用的 authorization_hook 值。
        self.authorization_hook = authorization_hook or AuthorizationHook()
        # 变量说明：post_hooks 表示当前流程使用的 post_hooks 集合。
        self.post_hooks = tuple(post_hooks)
        # 变量说明：lifecycle_hooks 表示当前流程使用的 lifecycle_hooks 集合。
        self.lifecycle_hooks = tuple(lifecycle_hooks)

    # 函数职责：异步完成 dispatch 对应的业务处理。
    # 参数关系：invocation 表示当前步骤使用的 invocation 值；runtime 表示当前步骤使用的 runtime 值。
    # 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
    async def dispatch(
        self,
        invocation: ToolInvocation,
        runtime: ToolRuntime,
    ) -> ToolDispatchOutcome:
        # 变量说明：context 表示当前步骤使用的 context 值。
        context = HookContext(
            runtime=runtime,
            workspace_root=self.workspace_root,
            permission_mode=self.permission_mode,
        )
        for hook in self.prepare_hooks:
            # 变量说明：decision 表示当前步骤使用的 decision 值。
            decision = await hook.before_invoke(invocation, context)
            if isinstance(decision, Rewrite):
                # 变量说明：arguments 表示当前流程使用的 arguments 集合。
                invocation.arguments = dict(decision.arguments)
            elif isinstance(decision, RequireApproval):
                return self._approval(invocation, decision.reason)
            elif isinstance(decision, Reject):
                return Rejected(ToolResult(
                    invocation.wire_name,
                    False,
                    decision.reason,
                    error_code=decision.error_code,
                ))
            elif not isinstance(decision, Continue):
                raise TypeError(f"unsupported hook decision: {type(decision).__name__}")

        # 变量说明：authorization 表示当前步骤使用的 authorization 值。
        authorization = await self.authorization_hook.before_invoke(invocation, context)
        if isinstance(authorization, RequireApproval):
            return self._approval(invocation, authorization.reason)
        if isinstance(authorization, Reject):
            return Rejected(ToolResult(
                invocation.wire_name,
                False,
                authorization.reason,
                error_code=authorization.error_code,
            ))

        for hook in self.lifecycle_hooks:
            await hook.tool_started(invocation, context)
        try:
            # 变量说明：result 表示本步骤产生的结果。
            result = await runtime.invoke(invocation)
            for hook in self.post_hooks:
                # 变量说明：result 表示本步骤产生的结果。
                result = await hook.after_invoke(invocation, result, context)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # 变量说明：result 表示本步骤产生的结果。
            result = ToolResult(
                invocation.wire_name,
                False,
                f"工具执行失败: {type(exc).__name__}",
                error_code="tool_error",
            )
            return DispatchFailed(result)
        finally:
            if "result" in locals():
                for hook in self.lifecycle_hooks:
                    await hook.tool_finished(invocation, result, context)
        return Executed(result)

    # 函数职责：完成 approval 对应的业务处理。
    # 参数关系：invocation 表示当前步骤使用的 invocation 值；reason 表示当前步骤使用的 reason 值。
    # 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
    @staticmethod
    def _approval(invocation: ToolInvocation, reason: str) -> AwaitingApproval:
        # 变量说明：request 表示调用方传入的请求数据。
        request = ApprovalRequest(
            id=invocation.call_id,
            tool_name=invocation.wire_name,
            arguments=dict(invocation.arguments),
            reason=reason,
        )
        return AwaitingApproval(request=request, invocation=invocation)
