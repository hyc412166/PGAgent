"""Single invocation lifecycle for authorization, hooks and execution."""

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


class InvocationPipeline:
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
        self.workspace_root = workspace_root
        self.permission_mode = permission_mode
        self.prepare_hooks = tuple(prepare_hooks)
        self.authorization_hook = authorization_hook or AuthorizationHook()
        self.post_hooks = tuple(post_hooks)
        self.lifecycle_hooks = tuple(lifecycle_hooks)

    async def dispatch(
        self,
        invocation: ToolInvocation,
        runtime: ToolRuntime,
    ) -> ToolDispatchOutcome:
        context = HookContext(
            runtime=runtime,
            workspace_root=self.workspace_root,
            permission_mode=self.permission_mode,
        )
        for hook in self.prepare_hooks:
            decision = await hook.before_invoke(invocation, context)
            if isinstance(decision, Rewrite):
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
            result = await runtime.invoke(invocation)
            for hook in self.post_hooks:
                result = await hook.after_invoke(invocation, result, context)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
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

    @staticmethod
    def _approval(invocation: ToolInvocation, reason: str) -> AwaitingApproval:
        request = ApprovalRequest(
            id=invocation.call_id,
            tool_name=invocation.wire_name,
            arguments=dict(invocation.arguments),
            reason=reason,
        )
        return AwaitingApproval(request=request, invocation=invocation)
