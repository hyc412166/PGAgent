# 文件职责：组织 engine 的上下文准备、模型行动和工具观察节点；状态由 RunState 在节点之间传递。
"""Deterministic lifecycle loop for one AgentRuntime execution."""

from __future__ import annotations

import inspect
from typing import Awaitable, Callable

from .state import RunState


# 节点接受当前运行状态，允许同步返回或异步更新；调度器统一处理两种实现。
# 变量说明：Node 表示当前步骤使用的 Node 值。
Node = Callable[[RunState], RunState | Awaitable[RunState]]


# 调用 node 并取得实际状态；result 可能是状态本身，也可能是待等待的协程。
# 函数职责：异步完成 invoke 对应的智能体处理。
# 参数关系：node 表示当前步骤使用的 node 值；state 表示当前运行状态。
# 返回关系：结果用于更新运行状态、形成模型输入或发送给上层调用方。
async def _invoke(node: Node, state: RunState) -> RunState:
    # 变量说明：result 表示本步骤处理结果。
    result = node(state)
    if inspect.isawaitable(result):
        return await result
    return result


# initial 是本次运行的起点；prepare_context 只执行一次，act/observe 交替执行直到终态。
# 函数职责：异步执行 agent_loop 对应流程。
# 参数关系：initial 表示当前步骤使用的 initial 值；prepare_context 表示当前步骤使用的 prepare_context 值；act 表示当前步骤使用的 act 值；observe 表示当前步骤使用的 observe 值。
# 返回关系：结果用于更新运行状态、形成模型输入或发送给上层调用方。
async def run_agent_loop(
    initial: RunState,
    *,
    prepare_context: Node,
    act: Node,
    observe: Node,
) -> RunState:
    """Run the fixed prepare/act/observe lifecycle until a terminal status."""

    # 变量说明：state 表示当前运行状态。
    state = await _invoke(prepare_context, initial)
    while state.get("status") == "acting":
        # 变量说明：state 表示当前运行状态。
        state = await _invoke(act, state)
        if state.get("status") != "observing":
            break
        # 变量说明：state 表示当前运行状态。
        state = await _invoke(observe, state)
    return state
