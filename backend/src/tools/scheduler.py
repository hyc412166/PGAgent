"""Batch scheduling policy derived from ToolRuntime execution metadata."""
# 文件职责：负责工具定义、授权、注册、调度与执行中的 scheduler 子模块。
# 逻辑关系：上层通过 tools/scheduler.py 使用本模块；本模块把处理结果交给同领域服务、持久化层或 API 响应层。

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .registry import ToolRegistry


# 类职责：定义 InvocationBatchPlan 在本领域中的数据与行为。
@dataclass(frozen=True, slots=True)
class InvocationBatchPlan:
    # 变量说明：parallel 表示当前步骤使用的 parallel 值。
    parallel: bool
    # 变量说明：reason 表示当前步骤使用的 reason 值。
    reason: str


# 类职责：定义 InvocationScheduler 在本领域中的数据与行为。
class InvocationScheduler:
    # 函数职责：完成 plan 对应的业务处理。
    # 参数关系：names 表示当前流程使用的 names 集合；registry 表示当前步骤使用的 registry 值；permission_mode 表示当前步骤使用的 permission_mode 值。
    # 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
    def plan(
        self,
        names: Iterable[str],
        *,
        registry: "ToolRegistry",
        permission_mode: str,
    ) -> InvocationBatchPlan:
        # 变量说明：wire_names 表示当前流程使用的 wire_names 集合。
        wire_names = tuple(str(name) for name in names)
        if len(wire_names) < 2:
            return InvocationBatchPlan(False, "single_invocation")
        if permission_mode == "ask":
            return InvocationBatchPlan(False, "approval_must_be_ordered")

        # 变量说明：runtimes 表示当前流程使用的 runtimes 集合。
        runtimes = tuple(registry.resolve_wire_name(name) for name in wire_names)
        if any(runtime is None for runtime in runtimes):
            return InvocationBatchPlan(False, "unresolved_tool")
        if all(runtime.execution.supports_parallel for runtime in runtimes if runtime is not None):
            return InvocationBatchPlan(True, "runtime_declared_parallel")

        # 变量说明：delegated 表示当前步骤使用的 delegated 值。
        delegated = all(name in {"task", "Agent"} for name in wire_names)
        if delegated and permission_mode == "full":
            return InvocationBatchPlan(True, "approved_delegation_batch")
        return InvocationBatchPlan(False, "ordered_side_effects")
