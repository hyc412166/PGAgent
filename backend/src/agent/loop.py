"""Deterministic lifecycle loop for one AgentRuntime execution."""

from __future__ import annotations

import inspect
from typing import Awaitable, Callable

from .state import RunState


Node = Callable[[RunState], RunState | Awaitable[RunState]]


async def _invoke(node: Node, state: RunState) -> RunState:
    result = node(state)
    if inspect.isawaitable(result):
        return await result
    return result


async def run_agent_loop(
    initial: RunState,
    *,
    prepare_context: Node,
    act: Node,
    observe: Node,
) -> RunState:
    """Run the fixed prepare/act/observe lifecycle until a terminal status."""

    state = await _invoke(prepare_context, initial)
    while state.get("status") == "acting":
        state = await _invoke(act, state)
        if state.get("status") != "observing":
            break
        state = await _invoke(observe, state)
    return state
