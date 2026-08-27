from __future__ import annotations

import asyncio

import pytest

from src.agent.loop import run_agent_loop


@pytest.mark.asyncio
async def test_agent_loop_runs_prepare_act_observe_until_terminal() -> None:
    calls: list[str] = []

    def prepare(state):  # type: ignore[no-untyped-def]
        calls.append("prepare")
        return {**state, "status": "acting"}

    async def act(state):  # type: ignore[no-untyped-def]
        calls.append("act")
        if calls.count("act") == 1:
            return {**state, "status": "observing"}
        return {**state, "status": "completed", "output": "done"}

    def observe(state):  # type: ignore[no-untyped-def]
        calls.append("observe")
        return {**state, "status": "acting"}

    result = await run_agent_loop(
        {"status": "received"},
        prepare_context=prepare,
        act=act,
        observe=observe,
    )

    assert calls == ["prepare", "act", "observe", "act"]
    assert result["status"] == "completed"
    assert result["output"] == "done"


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal_status", ["awaiting_approval", "completed", "failed", "stopped"])
async def test_agent_loop_does_not_observe_terminal_act_state(terminal_status: str) -> None:
    observed = False

    async def prepare(state):  # type: ignore[no-untyped-def]
        return {**state, "status": "acting"}

    async def act(state):  # type: ignore[no-untyped-def]
        return {**state, "status": terminal_status}

    async def observe(state):  # type: ignore[no-untyped-def]
        nonlocal observed
        observed = True
        return state

    result = await run_agent_loop(
        {"status": "received"},
        prepare_context=prepare,
        act=act,
        observe=observe,
    )

    assert result["status"] == terminal_status
    assert not observed


@pytest.mark.asyncio
async def test_agent_loop_propagates_cancellation() -> None:
    async def prepare(state):  # type: ignore[no-untyped-def]
        return {**state, "status": "acting"}

    async def act(_state):  # type: ignore[no-untyped-def]
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await run_agent_loop(
            {"status": "received"},
            prepare_context=prepare,
            act=act,
            observe=lambda state: state,
        )
