"""Unified metadata and executor object for built-in and external tools."""
# 文件职责：负责工具定义、授权、注册、调度与执行中的 runtime 子模块。
# 逻辑关系：上层通过 tools/runtime.py 使用本模块；本模块把处理结果交给同领域服务、持久化层或 API 响应层。

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any

from .invocation import ToolInvocation
from .name import ToolName
from .types import ToolResult


# 变量说明：ToolExecutor 表示当前步骤使用的 ToolExecutor 值。
ToolExecutor = Callable[[ToolInvocation], Awaitable[ToolResult]]


# 类职责：定义 ToolIdentity 在本领域中的数据与行为。
@dataclass(frozen=True, slots=True)
class ToolIdentity:
    # 变量说明：canonical_name 表示当前步骤使用的 canonical_name 值。
    canonical_name: ToolName
    # 变量说明：wire_name 表示当前步骤使用的 wire_name 值。
    wire_name: str
    # 变量说明：aliases 表示当前流程使用的 aliases 集合。
    aliases: tuple[str, ...] = ()


# 类职责：定义 ToolOrigin 在本领域中的数据与行为。
@dataclass(frozen=True, slots=True)
class ToolOrigin:
    # 变量说明：owner 表示当前步骤使用的 owner 值。
    owner: str
    # 变量说明：trusted 表示当前步骤使用的 trusted 值。
    trusted: bool
    # 变量说明：source 表示当前步骤使用的 source 值。
    source: str


# 类职责：定义 ToolPresentation 在本领域中的数据与行为。
@dataclass(frozen=True, slots=True)
class ToolPresentation:
    # 变量说明：advertise_by_default 表示当前步骤使用的 advertise_by_default 值。
    advertise_by_default: bool = True
    # 变量说明：discoverable 表示当前步骤使用的 discoverable 值。
    discoverable: bool = True
    # 变量说明：model_callable 表示当前步骤使用的 model_callable 值。
    model_callable: bool = True

    # 函数职责：完成 direct 对应的业务处理。
    # 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
    @classmethod
    def direct(cls) -> "ToolPresentation":
        return cls()

    # 函数职责：完成 deferred 对应的业务处理。
    # 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
    @classmethod
    def deferred(cls) -> "ToolPresentation":
        return cls(advertise_by_default=False, discoverable=True, model_callable=True)

    # 函数职责：完成 hidden 对应的业务处理。
    # 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
    @classmethod
    def hidden(cls) -> "ToolPresentation":
        return cls(advertise_by_default=False, discoverable=False, model_callable=False)


# 类职责：定义 ToolExecutionMetadata 在本领域中的数据与行为。
@dataclass(frozen=True, slots=True)
class ToolExecutionMetadata:
    # 变量说明：read_only 表示当前步骤使用的 read_only 值。
    read_only: bool = False
    # 变量说明：supports_parallel 表示当前步骤使用的 supports_parallel 值。
    supports_parallel: bool = False
    # 变量说明：approval_exempt 表示当前步骤使用的 approval_exempt 值。
    approval_exempt: bool = False
    # 变量说明：side_effect_scope 表示当前步骤使用的 side_effect_scope 值。
    side_effect_scope: str | None = None


# 类职责：协调 ToolRuntime 负责的业务流程与依赖。
@dataclass(slots=True)
class ToolRuntime:
    # 变量说明：identity 表示当前步骤使用的 identity 值。
    identity: ToolIdentity
    # 变量说明：origin 表示当前步骤使用的 origin 值。
    origin: ToolOrigin
    # 变量说明：schema 表示当前步骤使用的 schema 值。
    schema: Mapping[str, Any]
    # 变量说明：presentation 表示当前步骤使用的 presentation 值。
    presentation: ToolPresentation
    # 变量说明：execution 表示当前步骤使用的 execution 值。
    execution: ToolExecutionMetadata
    # 变量说明：executor 表示当前步骤使用的 executor 值。
    executor: ToolExecutor

    # 函数职责：异步完成 invoke 对应的业务处理。
    # 参数关系：invocation 表示当前步骤使用的 invocation 值。
    # 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
    async def invoke(self, invocation: ToolInvocation) -> ToolResult:
        # 变量说明：result 表示本步骤产生的结果。
        result = await self.executor(invocation)
        # 变量说明：tool_name 表示当前步骤使用的 tool_name 值。
        result.tool_name = self.identity.wire_name
        if result.approval_request is not None:
            # 变量说明：tool_name 表示当前步骤使用的 tool_name 值。
            result.approval_request.tool_name = self.identity.wire_name
        return result
