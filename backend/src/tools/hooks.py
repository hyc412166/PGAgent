"""Extensible invocation hooks with an explicit prepare/authorize/post order."""
# 文件职责：负责工具定义、授权、注册、调度与执行中的 hooks 子模块。
# 逻辑关系：上层通过 tools/hooks.py 使用本模块；本模块把处理结果交给同领域服务、持久化层或 API 响应层。

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from .invocation import ToolInvocation
from .runtime import ToolRuntime
from .types import ToolResult


# 类职责：定义 HookContext 在本领域中的数据与行为。
@dataclass(slots=True)
class HookContext:
    # 变量说明：runtime 表示当前步骤使用的 runtime 值。
    runtime: ToolRuntime
    # 变量说明：workspace_root 表示当前步骤使用的 workspace_root 值。
    workspace_root: str
    # 变量说明：permission_mode 表示当前步骤使用的 permission_mode 值。
    permission_mode: str
    # 变量说明：values 表示当前流程使用的 values 集合。
    values: dict[str, Any] = field(default_factory=dict)


# 类职责：定义 HookDecision 在本领域中的数据与行为。
class HookDecision:
    pass


# 类职责：定义 Continue 在本领域中的数据与行为。
# 继承关系：复用基类提供的契约，并向调用方暴露本类声明的字段和方法。
@dataclass(frozen=True, slots=True)
class Continue(HookDecision):
    pass


# 类职责：定义 Rewrite 在本领域中的数据与行为。
# 继承关系：复用基类提供的契约，并向调用方暴露本类声明的字段和方法。
@dataclass(frozen=True, slots=True)
class Rewrite(HookDecision):
    # 变量说明：arguments 表示当前流程使用的 arguments 集合。
    arguments: dict[str, Any]


# 类职责：定义 RequireApproval 在本领域中的数据与行为。
# 继承关系：复用基类提供的契约，并向调用方暴露本类声明的字段和方法。
@dataclass(frozen=True, slots=True)
class RequireApproval(HookDecision):
    # 变量说明：reason 表示当前步骤使用的 reason 值。
    reason: str


# 类职责：定义 Reject 在本领域中的数据与行为。
# 继承关系：复用基类提供的契约，并向调用方暴露本类声明的字段和方法。
@dataclass(frozen=True, slots=True)
class Reject(HookDecision):
    # 变量说明：reason 表示当前步骤使用的 reason 值。
    reason: str
    # 变量说明：error_code 表示当前步骤使用的 error_code 值。
    error_code: str = "permission_denied"


# 类职责：定义 PrepareHook 在本领域中的数据与行为。
# 继承关系：复用基类提供的契约，并向调用方暴露本类声明的字段和方法。
class PrepareHook(Protocol):
    # 函数职责：异步完成 before_invoke 对应的业务处理。
    # 参数关系：invocation 表示当前步骤使用的 invocation 值；context 表示当前步骤使用的 context 值。
    # 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
    async def before_invoke(
        self,
        invocation: ToolInvocation,
        context: HookContext,
    ) -> HookDecision: ...


# 类职责：定义 PostHook 在本领域中的数据与行为。
# 继承关系：复用基类提供的契约，并向调用方暴露本类声明的字段和方法。
class PostHook(Protocol):
    # 函数职责：异步完成 after_invoke 对应的业务处理。
    # 参数关系：invocation 表示当前步骤使用的 invocation 值；result 表示本步骤产生的结果；context 表示当前步骤使用的 context 值。
    # 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
    async def after_invoke(
        self,
        invocation: ToolInvocation,
        result: ToolResult,
        context: HookContext,
    ) -> ToolResult: ...


# 类职责：定义 LifecycleHook 在本领域中的数据与行为。
# 继承关系：复用基类提供的契约，并向调用方暴露本类声明的字段和方法。
class LifecycleHook(Protocol):
    # 函数职责：异步完成 tool_started 对应的业务处理。
    # 参数关系：invocation 表示当前步骤使用的 invocation 值；context 表示当前步骤使用的 context 值。
    # 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
    async def tool_started(self, invocation: ToolInvocation, context: HookContext) -> None: ...

    # 函数职责：异步完成 tool_finished 对应的业务处理。
    # 参数关系：invocation 表示当前步骤使用的 invocation 值；result 表示本步骤产生的结果；context 表示当前步骤使用的 context 值。
    # 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
    async def tool_finished(
        self,
        invocation: ToolInvocation,
        result: ToolResult,
        context: HookContext,
    ) -> None: ...
