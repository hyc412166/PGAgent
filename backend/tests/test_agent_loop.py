"""验证通用代理循环的 prepare、act、observe 状态推进、终止条件与取消传播。

测试通过 fixture 或辅助函数准备隔离环境，再调用真实服务、路由或运行时，并检查返回值、持久化状态与可观察副作用。
变量约定：tmp_path/monkeypatch 提供隔离环境，client/store/runtime 驱动被测链路，各类 *_id 串联持久化实体，payload 表示输入，response/result 表示实际输出，expected 表示期望值。
"""

from __future__ import annotations

import asyncio

import pytest

from src.agent.loop import run_agent_loop


@pytest.mark.asyncio
# 测试场景：验证该正常业务场景从输入准备到结果断言的完整链路；函数名 test_agent_loop_runs_prepare_act_observe_until_terminal 精确标识本用例的具体条件。
async def test_agent_loop_runs_prepare_act_observe_until_terminal() -> None:
    calls: list[str] = []

    # 辅助方法：prepare 实现测试替身在此调用阶段需要的最小行为。
    def prepare(state):  # type: ignore[no-untyped-def]
        calls.append("prepare")
        return {**state, "status": "acting"}

    # 辅助方法：act 实现测试替身在此调用阶段需要的最小行为。
    async def act(state):  # type: ignore[no-untyped-def]
        calls.append("act")
        if calls.count("act") == 1:
            return {**state, "status": "observing"}
        return {**state, "status": "completed", "output": "done"}

    # 辅助方法：observe 实现测试替身在此调用阶段需要的最小行为。
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
# 测试场景：验证非法、越界或不满足前置条件的操作会被明确拒绝，且不会产生错误状态；函数名 test_agent_loop_does_not_observe_terminal_act_state 精确标识本用例的具体条件。
async def test_agent_loop_does_not_observe_terminal_act_state(terminal_status: str) -> None:
    observed = False

    # 辅助方法：prepare 实现测试替身在此调用阶段需要的最小行为。
    async def prepare(state):  # type: ignore[no-untyped-def]
        return {**state, "status": "acting"}

    # 辅助方法：act 实现测试替身在此调用阶段需要的最小行为。
    async def act(state):  # type: ignore[no-untyped-def]
        return {**state, "status": terminal_status}

    # 辅助方法：observe 实现测试替身在此调用阶段需要的最小行为。
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
# 测试场景：验证取消或终止请求会收敛相关运行状态，并正确清理或保留应有资源；函数名 test_agent_loop_propagates_cancellation 精确标识本用例的具体条件。
async def test_agent_loop_propagates_cancellation() -> None:
    # 辅助方法：prepare 实现测试替身在此调用阶段需要的最小行为。
    async def prepare(state):  # type: ignore[no-untyped-def]
        return {**state, "status": "acting"}

    # 辅助方法：act 实现测试替身在此调用阶段需要的最小行为。
    async def act(_state):  # type: ignore[no-untyped-def]
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await run_agent_loop(
            {"status": "received"},
            prepare_context=prepare,
            act=act,
            observe=lambda state: state,
        )
