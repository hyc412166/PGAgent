"""验证代理循环守卫对重复工具调用、停滞、预算、完成条件和恢复策略的判定。

测试通过 fixture 或辅助函数准备隔离环境，再调用真实服务、路由或运行时，并检查返回值、持久化状态与可观察副作用。
变量约定：tmp_path/monkeypatch 提供隔离环境，client/store/runtime 驱动被测链路，各类 *_id 串联持久化实体，payload 表示输入，response/result 表示实际输出，expected 表示期望值。
"""

from __future__ import annotations

import asyncio
import json
import time

import pytest

from src.context.window import ContextManager
from src.context.assembly import COMPACTION_SECTION_TITLES, ConversationCompactor
from src.agent.engine import AgentRuntime, ModelToolCall, ModelTurn, RuntimeConfig, merge_usage, normalize_usage, provider_web_search_calls
from src.agent.errors import APIErrorKind, call_with_retry, classify_api_error
from src.agent.guards import LoopGuard
from src.agent.turn import LocalToolStatus, TurnLedger
from src.model.output import AssistantMessageItem, LocalToolCallItem, NormalizedModelResponse
from src.tools import create_default_registry
from src.tools.types import ToolResult


# 测试替身类：StatusError 模拟外部依赖的响应与调用记录，使连接或协议测试无需访问真实服务。
class StatusError(RuntimeError):
    # 辅助方法：__init__ 实现测试替身在此调用阶段需要的最小行为。
    def __init__(self, status_code: int) -> None:
        super().__init__(f"HTTP {status_code}")
        self.status_code = status_code


# 测试替身类：ManualClock 模拟外部依赖的响应与调用记录，使连接或协议测试无需访问真实服务。
class ManualClock:
    # 辅助方法：__init__ 实现测试替身在此调用阶段需要的最小行为。
    def __init__(self) -> None:
        self.value = 0.0

    # 辅助方法：__call__ 实现测试替身在此调用阶段需要的最小行为。
    def __call__(self) -> float:
        return self.value

    # 辅助方法：advance 实现测试替身在此调用阶段需要的最小行为。
    def advance(self, seconds: float) -> None:
        self.value += seconds


# 辅助函数：write_call 封装本组测试重复使用的输入准备、状态查询或测试替身行为。
def write_call(call_id: str, path: str, content: str) -> ModelToolCall:
    return ModelToolCall(call_id, "apply_patch", {
        "patch": f"*** Begin Patch\n*** Add File: {path}\n+{content}\n*** End Patch",
    })


# 测试场景：验证非法、越界或不满足前置条件的操作会被明确拒绝，且不会产生错误状态；函数名 test_usage_merge_rejects_model_identity_drift 精确标识本用例的具体条件。
def test_usage_merge_rejects_model_identity_drift() -> None:
    with pytest.raises(ValueError, match="模型身份发生变化"):
        merge_usage(
            {"request_count": 1, "model_connection_id": "connection-1", "model_id": "model-1"},
            {"request_count": 1, "model_connection_id": "connection-2", "model_id": "model-2"},
        )


# 测试场景：验证该正常业务场景从输入准备到结果断言的完整链路；函数名 test_normalize_responses_usage_splits_cached_input_without_double_counting 精确标识本用例的具体条件。
def test_normalize_responses_usage_splits_cached_input_without_double_counting() -> None:
    raw = {
        "input_tokens": 100,
        "input_tokens_details": {"cached_tokens": 30, "cache_write_tokens": 10},
        "output_tokens": 20,
        "output_tokens_details": {"reasoning_tokens": 5},
        "total_tokens": 120,
    }
    normalized = normalize_usage(raw)
    assert normalized == {
        "request_count": 1,
        "input_tokens": 60,
        "output_tokens": 20,
        "cache_creation_tokens": 10,
        "cache_read_tokens": 30,
        "total_tokens": 120,
        "cost_usd": 0.0,
        "model_connection_id": None,
        "model_id": "",
        "provider": "",
    }
    assert normalize_usage(normalized) == normalized


# 测试场景：验证该正常业务场景从输入准备到结果断言的完整链路；函数名 test_normalize_responses_usage_accepts_missing_cache_write_detail 精确标识本用例的具体条件。
def test_normalize_responses_usage_accepts_missing_cache_write_detail() -> None:
    normalized = normalize_usage({
        "input_tokens": 80,
        "input_tokens_details": {"cached_tokens": 48},
        "output_tokens": 5,
        "total_tokens": 85,
    })
    assert normalized["input_tokens"] == 32
    assert normalized["cache_creation_tokens"] == 0
    assert normalized["cache_read_tokens"] == 48
    assert normalized["total_tokens"] == 85


# 测试场景：验证取消或终止请求会收敛相关运行状态，并正确清理或保留应有资源；函数名 test_identical_call_is_stopped_on_third_occurrence 精确标识本用例的具体条件。
def test_identical_call_is_stopped_on_third_occurrence() -> None:
    guard = LoopGuard(identical_limit=3)
    assert not guard.record_tool_call("read_file", {"path": "a", "line": 1}).stop
    # Canonical JSON makes argument order irrelevant.
    assert not guard.record_tool_call("read_file", {"line": 1, "path": "a"}).stop
    decision = guard.record_tool_call("read_file", {"path": "a", "line": 1})
    assert decision.stop
    assert decision.code == "repeated_tool_call"


# 测试场景：验证取消或终止请求会收敛相关运行状态，并正确清理或保留应有资源；函数名 test_no_progress_is_stopped_after_four_steps 精确标识本用例的具体条件。
def test_no_progress_is_stopped_after_four_steps() -> None:
    guard = LoopGuard(no_progress_limit=4)
    for _ in range(3):
        assert not guard.record_progress(False).stop
    decision = guard.record_progress(False)
    assert decision.stop
    assert decision.code == "no_progress"
    assert guard.no_progress_count == 4


# 测试场景：验证该正常业务场景从输入准备到结果断言的完整链路；函数名 test_real_progress_resets_no_progress_counter 精确标识本用例的具体条件。
def test_real_progress_resets_no_progress_counter() -> None:
    guard = LoopGuard(no_progress_limit=2)
    guard.record_progress(False)
    guard.record_progress(True)
    assert not guard.record_progress(False).stop


# 测试场景：验证时间、容量或上下文预算边界以及达到边界后的可观察处理结果；函数名 test_zero_hard_limits_allow_large_runs_but_keep_anti_loop_guards 精确标识本用例的具体条件。
def test_zero_hard_limits_allow_large_runs_but_keep_anti_loop_guards() -> None:
    guard = LoopGuard(max_steps=0, max_calls=0, identical_limit=3, no_progress_limit=4)
    for index in range(250):
        assert not guard.before_step().stop
        assert not guard.before_tool_call("read_file", {"path": f"file-{index}"}).stop
        assert not guard.record_progress(True).stop
    assert guard.steps == 250
    assert guard.calls == 250


@pytest.mark.asyncio
# 测试场景：验证该正常业务场景从输入准备到结果断言的完整链路；函数名 test_production_context_is_stable_prefix_plus_transcript 精确标识本用例的具体条件。
async def test_production_context_is_stable_prefix_plus_transcript(tmp_path) -> None:
    observed: list[dict] = []

    # 辅助方法：model_call 实现测试替身在此调用阶段需要的最小行为。
    async def model_call(**kwargs) -> ModelTurn:
        observed.extend(kwargs["messages"])
        return ModelTurn(content="done")

    runtime = AgentRuntime(
        model_call=model_call,
        tool_registry=create_default_registry(str(tmp_path)),
    )
    outcome = await runtime.run(
        system_prompt="safe",
        recent_messages=[{"role": "user", "content": "finish the refactor"}],
    )

    assert outcome.status == "completed"
    rendered = "\n".join(str(item.get("content") or "") for item in observed)
    assert "## System rules\nsafe" in rendered
    assert "<user_task>" not in rendered
    assert "finish the refactor" in rendered


@pytest.mark.asyncio
# 测试场景：验证该正常业务场景从输入准备到结果断言的完整链路；函数名 test_production_context_is_an_append_only_provider_prefix_across_user_turns 精确标识本用例的具体条件。
async def test_production_context_is_an_append_only_provider_prefix_across_user_turns(tmp_path) -> None:
    observed: list[list[dict]] = []

    # 辅助方法：model_call 实现测试替身在此调用阶段需要的最小行为。
    async def model_call(**kwargs) -> ModelTurn:
        observed.append([dict(item) for item in kwargs["messages"]])
        return ModelTurn(content="done")

    runtime = AgentRuntime(
        model_call=model_call,
        tool_registry=create_default_registry(str(tmp_path)),
    )
    first_transcript = [{"role": "user", "content": "first task"}]
    await runtime.run(system_prompt="safe", recent_messages=first_transcript)
    await runtime.run(
        system_prompt="safe",
        recent_messages=[
            *first_transcript,
            {"role": "assistant", "content": "done"},
            {"role": "user", "content": "second task"},
        ],
    )

    assert len(observed) == 2
    assert observed[1][:len(observed[0])] == observed[0]
    assert not any("<user_task>" in str(item.get("content") or "") for messages in observed for item in messages)


@pytest.mark.asyncio
# 测试场景：验证该正常业务场景从输入准备到结果断言的完整链路；函数名 test_provider_boundary_repairs_corrupt_historical_tool_groups 精确标识本用例的具体条件。
async def test_provider_boundary_repairs_corrupt_historical_tool_groups(tmp_path) -> None:
    observed: list[dict] = []

    # 辅助方法：model_call 实现测试替身在此调用阶段需要的最小行为。
    async def model_call(**kwargs) -> ModelTurn:
        observed.extend(dict(item) for item in kwargs["messages"])
        return ModelTurn(content="recovered")

    runtime = AgentRuntime(
        model_call=model_call,
        tool_registry=create_default_registry(str(tmp_path)),
        context_manager=ContextManager(max_tokens=512),
    )
    outcome = await runtime.run(
        system_prompt="safe",
        recent_messages=[],
        prepared_messages=[
            {"role": "system", "content": "safe"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"id": "call-a", "function": {"name": "read_file", "arguments": "{}"}}],
            },
            {"role": "tool", "tool_call_id": "call-a", "content": "valid"},
            {"role": "tool", "tool_call_id": "orphan", "content": "invalid"},
            {"role": "user", "content": "continue"},
        ],
    )

    assert outcome.status == "completed"
    assert [item["role"] for item in observed] == ["system", "user"]
    repair = next(item for item in outcome.events if item["type"] == "context_protocol_repaired")
    assert repair["removed_messages"] == 3
    assert repair["affected_call_ids"] == ["call-a", "orphan"]


@pytest.mark.asyncio
# 测试场景：验证该正常业务场景从输入准备到结果断言的完整链路；函数名 test_production_prepare_audits_corrupt_historical_tool_groups 精确标识本用例的具体条件。
async def test_production_prepare_audits_corrupt_historical_tool_groups(tmp_path) -> None:
    observed: list[dict] = []

    # 辅助方法：model_call 实现测试替身在此调用阶段需要的最小行为。
    async def model_call(**kwargs) -> ModelTurn:
        observed.extend(dict(item) for item in kwargs["messages"])
        return ModelTurn(content="recovered")

    runtime = AgentRuntime(
        model_call=model_call,
        tool_registry=create_default_registry(str(tmp_path)),
    )
    outcome = await runtime.run(
        system_prompt="safe",
        recent_messages=[
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"id": "call-a", "function": {"name": "read_file", "arguments": "{}"}}],
            },
            {"role": "tool", "tool_call_id": "call-a", "content": "valid"},
            {"role": "tool", "tool_call_id": "orphan", "content": "invalid"},
            {"role": "user", "content": "continue"},
        ],
    )

    assert outcome.status == "completed"
    assert all(item.get("tool_call_id") not in {"call-a", "orphan"} for item in observed)
    repair = next(item for item in outcome.events if item["type"] == "context_protocol_repaired")
    assert repair["phase"] == "prepare"
    assert repair["removed_messages"] == 3


@pytest.mark.asyncio
# 测试场景：验证并发或批量执行时的顺序、隔离性和最终状态一致性；函数名 test_multiple_task_calls_are_dispatched_concurrently 精确标识本用例的具体条件。
async def test_multiple_task_calls_are_dispatched_concurrently(tmp_path) -> None:
    started = asyncio.Event()
    active = 0
    max_active = 0
    start_count = 0
    model_turns = 0

    # 辅助方法：delegate 实现测试替身在此调用阶段需要的最小行为。
    async def delegate(task: str, *, agent_id: str, call_id: str | None = None) -> ToolResult:
        nonlocal active, max_active, start_count
        active += 1
        max_active = max(max_active, active)
        start_count += 1
        if start_count == 2:
            started.set()
        await asyncio.wait_for(started.wait(), timeout=1)
        active -= 1
        return ToolResult("task", True, f"{agent_id}: {task}")

    # 辅助方法：model_call 实现测试替身在此调用阶段需要的最小行为。
    async def model_call(**kwargs):  # type: ignore[no-untyped-def]
        nonlocal model_turns
        model_turns += 1
        if model_turns == 1:
            return ModelTurn(tool_calls=[
                ModelToolCall("task-a", "task", {"task": "research", "agent_id": "agent-a"}),
                ModelToolCall("task-b", "task", {"task": "review", "agent_id": "agent-b"}),
            ])
        return ModelTurn(content="parallel results received")

    runtime = AgentRuntime(
        model_call=model_call,
        tool_registry=create_default_registry(
            str(tmp_path),
            allowed_tool_names=["task"],
            permission_mode="full",
            task_delegate=delegate,
        ),
    )
    outcome = await runtime.run(system_prompt="", recent_messages=[])

    assert outcome.status == "completed"
    assert outcome.output == "parallel results received"
    assert max_active == 2


@pytest.mark.asyncio
# 测试场景：验证非法、越界或不满足前置条件的操作会被明确拒绝，且不会产生错误状态；函数名 test_parallel_task_turn_rejects_total_fanout_above_limit 精确标识本用例的具体条件。
async def test_parallel_task_turn_rejects_total_fanout_above_limit(tmp_path) -> None:
    invoked: list[str] = []
    model_turns = 0

    # 辅助方法：delegate 实现测试替身在此调用阶段需要的最小行为。
    async def delegate(task: str, *, agent_id: str, call_id: str | None = None) -> ToolResult:
        invoked.append(agent_id)
        return ToolResult("task", True, task)

    # 辅助方法：model_call 实现测试替身在此调用阶段需要的最小行为。
    async def model_call(**kwargs):  # type: ignore[no-untyped-def]
        nonlocal model_turns
        model_turns += 1
        if model_turns == 1:
            return ModelTurn(tool_calls=[
                ModelToolCall(f"task-{index}", "task", {
                    "task": f"work-{index}",
                    "agent_id": f"agent-{index}",
                })
                for index in range(9)
            ])
        tool_results = [
            json.loads(message["content"])
            for message in kwargs["messages"]
            if message.get("role") == "tool"
        ]
        assert len(tool_results) == 9
        assert {result["error_code"] for result in tool_results} == {"delegate_parallel_limit"}
        return ModelTurn(content="fanout rejected")

    runtime = AgentRuntime(
        model_call=model_call,
        tool_registry=create_default_registry(
            str(tmp_path),
            allowed_tool_names=["task"],
            permission_mode="full",
            task_delegate=delegate,
        ),
    )
    outcome = await runtime.run(system_prompt="", recent_messages=[])

    assert outcome.status == "completed"
    assert outcome.output == "fanout rejected"
    assert invoked == []


@pytest.mark.asyncio
# 测试场景：验证非法、越界或不满足前置条件的操作会被明确拒绝，且不会产生错误状态；函数名 test_mixed_tool_turn_cannot_bypass_total_delegate_limit 精确标识本用例的具体条件。
async def test_mixed_tool_turn_cannot_bypass_total_delegate_limit(tmp_path) -> None:
    invoked: list[str] = []
    model_turns = 0

    # 辅助方法：delegate 实现测试替身在此调用阶段需要的最小行为。
    async def delegate(task: str, *, agent_id: str, call_id: str | None = None) -> ToolResult:
        invoked.append(agent_id)
        return ToolResult("task", True, task)

    # 辅助方法：model_call 实现测试替身在此调用阶段需要的最小行为。
    async def model_call(**kwargs):  # type: ignore[no-untyped-def]
        nonlocal model_turns
        model_turns += 1
        if model_turns == 1:
            return ModelTurn(tool_calls=[
                ModelToolCall("batch-a", "task", {"tasks": [
                    {"task": f"research-{index}", "agent_id": f"agent-a-{index}"}
                    for index in range(5)
                ]}),
                ModelToolCall("read-between", "read", {"path": "missing.txt"}),
                ModelToolCall("batch-b", "task", {"tasks": [
                    {"task": f"review-{index}", "agent_id": f"agent-b-{index}"}
                    for index in range(4)
                ]}),
            ])
        task_results = [
            json.loads(message["content"])
            for message in kwargs["messages"]
            if message.get("role") == "tool" and message.get("name") == "task"
        ]
        assert len(task_results) == 2
        assert {result["error_code"] for result in task_results} == {"delegate_parallel_limit"}
        return ModelTurn(content="mixed fanout rejected")

    runtime = AgentRuntime(
        model_call=model_call,
        tool_registry=create_default_registry(
            str(tmp_path),
            allowed_tool_names=["task", "read"],
            permission_mode="full",
            task_delegate=delegate,
        ),
    )
    outcome = await runtime.run(system_prompt="", recent_messages=[])

    assert outcome.status == "completed"
    assert outcome.output == "mixed fanout rejected"
    assert invoked == []


@pytest.mark.asyncio
# 测试场景：验证并发或批量执行时的顺序、隔离性和最终状态一致性；函数名 test_parallel_child_approval_keeps_sibling_results_and_resumes_all 精确标识本用例的具体条件。
async def test_parallel_child_approval_keeps_sibling_results_and_resumes_all(tmp_path) -> None:
    child_a_completed = False
    calls: list[str] = []
    model_turns = 0

    # 辅助方法：delegate 实现测试替身在此调用阶段需要的最小行为。
    async def delegate(task: str, *, agent_id: str, call_id: str | None = None) -> ToolResult:
        calls.append(str(call_id))
        if call_id == "task-a" and not child_a_completed:
            return ToolResult(
                "task",
                False,
                json.dumps({"status": "awaiting_approval", "child_run_id": "child-a"}),
                error_code="delegate_child_awaiting_approval",
                metadata={
                    "task_id": "delegation-a",
                    "child_run_id": "child-a",
                    "delegated_child_awaiting_approval": True,
                },
            )
        return ToolResult("task", True, f"{agent_id}: {task} completed")

    # 辅助方法：model_call 实现测试替身在此调用阶段需要的最小行为。
    async def model_call(**kwargs):  # type: ignore[no-untyped-def]
        nonlocal model_turns
        model_turns += 1
        if model_turns == 1:
            return ModelTurn(tool_calls=[
                ModelToolCall("task-a", "task", {"task": "research", "agent_id": "agent-a"}),
                ModelToolCall("task-b", "task", {"task": "review", "agent_id": "agent-b"}),
            ])
        tool_ids = [
            message.get("tool_call_id")
            for message in kwargs["messages"]
            if message.get("role") == "tool"
        ]
        assert tool_ids[-2:] == ["task-a", "task-b"]
        return ModelTurn(content="all child results received")

    runtime = AgentRuntime(
        model_call=model_call,
        tool_registry=create_default_registry(
            str(tmp_path),
            allowed_tool_names=["task"],
            permission_mode="full",
            task_delegate=delegate,
        ),
    )
    waiting = await runtime.run(system_prompt="", recent_messages=[])
    assert waiting.status == "stopped"
    assert waiting.stop_reason == "delegated_child_awaiting_approval"
    assert [
        message.get("tool_call_id")
        for message in waiting.messages
        if message.get("role") == "tool"
    ] == ["task-a", "task-b"]

    still_waiting = await runtime.resume_after_delegated_child(waiting)
    assert still_waiting.status == "stopped"
    assert still_waiting.stop_reason == "delegated_child_awaiting_approval"
    assert still_waiting.output_ledger is waiting.output_ledger

    child_a_completed = True
    resumed = await runtime.resume_after_delegated_child(still_waiting)
    assert resumed.status == "completed"
    assert resumed.output == "all child results received"
    assert calls == ["task-a", "task-b", "task-a", "task-a"]


@pytest.mark.asyncio
# 测试场景：验证取消或终止请求会收敛相关运行状态，并正确清理或保留应有资源；函数名 test_active_runtime_fuse_stops_without_restoring_step_or_tool_limits 精确标识本用例的具体条件。
async def test_active_runtime_fuse_stops_without_restoring_step_or_tool_limits(tmp_path) -> None:
    clock = ManualClock()

    # 辅助方法：model_call 实现测试替身在此调用阶段需要的最小行为。
    async def model_call(**_kwargs) -> ModelTurn:
        clock.advance(2.0)
        return ModelTurn(content="late answer")

    runtime = AgentRuntime(
        model_call=model_call,
        tool_registry=create_default_registry(str(tmp_path)),
        config=RuntimeConfig(max_steps=0, max_tool_calls=0, max_run_seconds=1.0),
        clock=clock,
    )
    outcome = await runtime.run(system_prompt="safe", recent_messages=[])
    assert outcome.status == "stopped"
    assert outcome.stop_reason == "max_run_time"
    assert outcome.active_elapsed_seconds == pytest.approx(2.0)


@pytest.mark.asyncio
# 测试场景：验证权限、审批或敏感数据边界在完整调用链路中保持有效；函数名 test_active_runtime_accumulates_across_approval_pause 精确标识本用例的具体条件。
async def test_active_runtime_accumulates_across_approval_pause(tmp_path) -> None:
    clock = ManualClock()
    turns = 0

    # 辅助方法：model_call 实现测试替身在此调用阶段需要的最小行为。
    async def model_call(**_kwargs) -> ModelTurn:
        nonlocal turns
        turns += 1
        if turns == 1:
            clock.advance(0.6)
            return ModelTurn(tool_calls=[
                ModelToolCall(
                    "write",
                    "apply_patch",
                    {"patch": "*** Begin Patch\n*** Add File: x.txt\n+x\n*** End Patch"},
                )
            ])
        clock.advance(0.5)
        return ModelTurn(content="too late")

    runtime = AgentRuntime(
        model_call=model_call,
        tool_registry=create_default_registry(str(tmp_path), permission_mode="ask"),
        config=RuntimeConfig(max_run_seconds=1.0),
        clock=clock,
    )
    waiting = await runtime.run(system_prompt="safe", recent_messages=[])
    assert waiting.status == "awaiting_approval"
    assert waiting.active_elapsed_seconds == pytest.approx(0.6)
    resumed = await runtime.resume_after_approval(waiting)
    assert resumed.status == "stopped"
    assert resumed.stop_reason == "max_run_time"
    assert resumed.active_elapsed_seconds == pytest.approx(1.1)
    assert (tmp_path / "x.txt").exists()


@pytest.mark.asyncio
# 测试场景：验证非法、越界或不满足前置条件的操作会被明确拒绝，且不会产生错误状态；函数名 test_background_wait_does_not_consume_active_runtime_budget 精确标识本用例的具体条件。
async def test_background_wait_does_not_consume_active_runtime_budget(tmp_path) -> None:
    clock = ManualClock()
    turns = 0

    # 测试替身类：BackgroundStore 保存该局部场景的可控状态。
    class BackgroundStore:
        # 辅助方法：check 实现测试替身在此调用阶段需要的最小行为。
        def check(self, **_kwargs) -> ToolResult:
            clock.advance(10.0)
            return ToolResult(
                "check_background",
                True,
                '{"status":"completed"}',
                metadata={"background_wait_seconds": 10.0},
            )

    # 辅助方法：model_call 实现测试替身在此调用阶段需要的最小行为。
    async def model_call(**_kwargs) -> ModelTurn:
        nonlocal turns
        turns += 1
        clock.advance(0.4)
        if turns == 1:
            return ModelTurn(tool_calls=[
                ModelToolCall("wait-bg", "check_background", {"task_id": "job-1", "wait": True})
            ])
        return ModelTurn(content="background result received")

    runtime = AgentRuntime(
        model_call=model_call,
        tool_registry=create_default_registry(
            str(tmp_path),
            allowed_tool_names=["check_background"],
            background_store=BackgroundStore(),
        ),
        config=RuntimeConfig(max_run_seconds=1.0),
        clock=clock,
    )

    outcome = await runtime.run(system_prompt="safe", recent_messages=[])

    assert outcome.status == "completed"
    assert outcome.active_elapsed_seconds == pytest.approx(0.8)


@pytest.mark.asyncio
# 测试场景：验证时间、容量或上下文预算边界以及达到边界后的可观察处理结果；函数名 test_provider_timeout_is_not_misclassified_as_run_fuse 精确标识本用例的具体条件。
async def test_provider_timeout_is_not_misclassified_as_run_fuse(tmp_path) -> None:
    clock = ManualClock()
    attempts = 0

    # 辅助方法：model_call 实现测试替身在此调用阶段需要的最小行为。
    async def model_call(**_kwargs) -> ModelTurn:
        nonlocal attempts
        attempts += 1
        raise asyncio.TimeoutError("provider timed out")

    runtime = AgentRuntime(
        model_call=model_call,
        tool_registry=create_default_registry(str(tmp_path)),
        config=RuntimeConfig(max_run_seconds=50.0, model_timeout_seconds=90.0, api_base_delay=0),
        clock=clock,
    )
    outcome = await runtime.run(system_prompt="safe", recent_messages=[])
    assert outcome.status == "failed"
    assert outcome.stop_reason is None
    assert attempts == 3


@pytest.mark.asyncio
# 测试场景：验证权限、审批或敏感数据边界在完整调用链路中保持有效；函数名 test_observation_history_is_not_truncated_to_200_before_approval 精确标识本用例的具体条件。
async def test_observation_history_is_not_truncated_to_200_before_approval(tmp_path) -> None:
    calls = [
        ModelToolCall(f"missing-{index}", f"missing-{index}", {})
        for index in range(250)
    ]
    calls.append(ModelToolCall("patch", "apply_patch", {
        "patch": "*** Begin Patch\n*** Add File: x.txt\n+x\n*** End Patch",
    }))

    # 辅助方法：model_call 实现测试替身在此调用阶段需要的最小行为。
    async def model_call(**_kwargs) -> ModelTurn:
        return ModelTurn(tool_calls=calls)

    runtime = AgentRuntime(
        model_call=model_call,
        tool_registry=create_default_registry(str(tmp_path), permission_mode="ask"),
        config=RuntimeConfig(observation_history_limit=1_000),
    )
    outcome = await runtime.run(system_prompt="safe", recent_messages=[])
    assert outcome.status == "awaiting_approval"
    assert len(outcome.pending_approval["seen_observations"]) == 250


@pytest.mark.asyncio
# 测试场景：验证失败会保留可诊断信息并收敛为一致、可恢复的状态；函数名 test_auth_failure_is_not_retried 精确标识本用例的具体条件。
async def test_auth_failure_is_not_retried() -> None:
    attempts = 0

    # 辅助方法：operation 实现测试替身在此调用阶段需要的最小行为。
    async def operation() -> None:
        nonlocal attempts
        attempts += 1
        raise StatusError(401)

    with pytest.raises(StatusError):
        await call_with_retry(operation, max_attempts=3, sleep=lambda _delay: _completed())
    assert attempts == 1
    assert classify_api_error(StatusError(403)) == APIErrorKind.AUTH


@pytest.mark.asyncio
# 测试场景：验证时间、容量或上下文预算边界以及达到边界后的可观察处理结果；函数名 test_rate_limit_retries_at_most_three_total_attempts 精确标识本用例的具体条件。
async def test_rate_limit_retries_at_most_three_total_attempts() -> None:
    attempts = 0
    delays: list[float] = []

    # 辅助方法：operation 实现测试替身在此调用阶段需要的最小行为。
    async def operation() -> str:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise StatusError(429)
        return "ok"

    # 局部测试函数：fake_sleep 模拟该步骤的返回结果或异常。
    async def fake_sleep(delay: float) -> None:
        delays.append(delay)

    result = await call_with_retry(
        operation,
        max_attempts=3,
        base_delay=1,
        sleep=fake_sleep,
        random_source=lambda: 0.5,
    )
    assert result == "ok"
    assert attempts == 3
    assert delays == [1.0, 2.0]


@pytest.mark.asyncio
# 测试场景：验证状态能够可靠持久化、重放或在重启后恢复，并保持记录之间的关联；函数名 test_provider_managed_retry_is_persisted_before_terminal_failure 精确标识本用例的具体条件。
async def test_provider_managed_retry_is_persisted_before_terminal_failure(tmp_path) -> None:
    published: list[dict] = []

    # 辅助方法：model_call 实现测试替身在此调用阶段需要的最小行为。
    async def model_call(*, on_retry=None, **_kwargs) -> ModelTurn:
        await on_retry("request", 1, 0)
        raise StatusError(502)

    model_call.manages_retries = True
    runtime = AgentRuntime(
        model_call=model_call,
        tool_registry=create_default_registry(str(tmp_path)),
        event_sink=published.append,
    )

    outcome = await runtime.run(system_prompt="safe", recent_messages=[])

    retry = next(event for event in outcome.events if event["type"] == "model_retry")
    failed = next(event for event in outcome.events if event["type"] == "model_failed")
    assert retry == {
        "type": "model_retry",
        "stage": "request",
        "attempt": 1,
        "delay_seconds": 0,
    }
    assert retry in published
    assert failed["status_code"] == 502
    assert failed["error_kind"] == "server"
    assert failed["retry_exhausted"] is True
    assert failed["retry_attempt_count"] == 1
    assert outcome.error == "StatusError"
    assert "HTTP 502" not in str(outcome.error)


@pytest.mark.asyncio
# 测试场景：模型协议异常携带的安全供应商错误码必须进入诊断事件，原始正文不得进入事件或结果。
async def test_model_failure_event_preserves_safe_provider_error_code(tmp_path) -> None:
    class ProviderFailure(RuntimeError):
        retryable = False
        provider_error_code = "invalid_prompt"
        provider_error_type = "invalid_request_error"

    async def model_call(**_kwargs) -> ModelTurn:
        raise ProviderFailure("private provider response body")

    runtime = AgentRuntime(
        model_call=model_call,
        tool_registry=create_default_registry(str(tmp_path)),
    )

    outcome = await runtime.run(system_prompt="safe", recent_messages=[])

    failed = next(event for event in outcome.events if event["type"] == "model_failed")
    assert failed["provider_error_code"] == "invalid_prompt"
    assert failed["provider_error_type"] == "invalid_request_error"
    assert "private provider response body" not in json.dumps(failed, ensure_ascii=False)
    assert outcome.error == "ProviderFailure"


@pytest.mark.asyncio
# 测试场景：供应商错误标识若包含正文式内容，不得进入持久事件或诊断日志字段。
async def test_model_failure_event_drops_untrusted_provider_error_identifiers(tmp_path) -> None:
    class ProviderFailure(RuntimeError):
        retryable = False
        provider_error_code = "invalid_prompt private response body"
        provider_error_type = "invalid_request_error\nsecret"

    async def model_call(**_kwargs) -> ModelTurn:
        raise ProviderFailure("private provider response body")

    runtime = AgentRuntime(
        model_call=model_call,
        tool_registry=create_default_registry(str(tmp_path)),
    )

    outcome = await runtime.run(system_prompt="safe", recent_messages=[])

    failed = next(event for event in outcome.events if event["type"] == "model_failed")
    assert "provider_error_code" not in failed
    assert "provider_error_type" not in failed
    assert "private provider response body" not in json.dumps(failed, ensure_ascii=False)


@pytest.mark.asyncio
# 测试场景：验证取消或终止请求会收敛相关运行状态，并正确清理或保留应有资源；函数名 test_runtime_stops_repeated_model_tool_call 精确标识本用例的具体条件。
async def test_runtime_stops_repeated_model_tool_call(tmp_path) -> None:
    # 辅助方法：model_call 实现测试替身在此调用阶段需要的最小行为。
    async def model_call(**_kwargs) -> ModelTurn:
        return ModelTurn(tool_calls=[ModelToolCall("same", "glob", {"path": "."})])

    runtime = AgentRuntime(
        model_call=model_call,
        tool_registry=create_default_registry(str(tmp_path)),
        config=RuntimeConfig(api_base_delay=0, identical_call_limit=3),
    )
    outcome = await runtime.run(system_prompt="safe", recent_messages=[])
    assert outcome.status == "stopped"
    assert outcome.stop_reason == "repeated_tool_call"
    assert outcome.tool_calls == 3


@pytest.mark.asyncio
async def test_legacy_duplicate_call_with_changed_identity_fails_before_dispatch(tmp_path) -> None:
    turns = 0

    async def model_call(**_kwargs) -> ModelTurn:
        nonlocal turns
        turns += 1
        if turns == 1:
            return ModelTurn(tool_calls=[write_call("same", "first.txt", "safe")])
        return ModelTurn(tool_calls=[write_call("same", "second.txt", "must-not-run")])

    runtime = AgentRuntime(
        model_call=model_call,
        tool_registry=create_default_registry(str(tmp_path), permission_mode="full"),
        config=RuntimeConfig(api_base_delay=0, identical_call_limit=3),
    )
    outcome = await runtime.run(system_prompt="safe", recent_messages=[])

    assert outcome.status in {"failed", "stopped"}
    assert (tmp_path / "first.txt").read_text(encoding="utf-8") == "safe\n"
    assert not (tmp_path / "second.txt").exists()


@pytest.mark.asyncio
# 测试场景：验证该正常业务场景从输入准备到结果断言的完整链路；函数名 test_runtime_pauses_before_side_effect 精确标识本用例的具体条件。
async def test_runtime_pauses_before_side_effect(tmp_path) -> None:
    # 辅助方法：model_call 实现测试替身在此调用阶段需要的最小行为。
    async def model_call(**_kwargs) -> ModelTurn:
        return ModelTurn(
            tool_calls=[write_call("write-1", "x.txt", "hello")]
        )

    runtime = AgentRuntime(
        model_call=model_call,
        tool_registry=create_default_registry(str(tmp_path), permission_mode="ask"),
    )
    outcome = await runtime.run(system_prompt="safe", recent_messages=[])
    assert outcome.status == "awaiting_approval"
    assert outcome.pending_approval is not None
    assert outcome.pending_approval["id"] == "write-1"
    assert not (tmp_path / "x.txt").exists()


@pytest.mark.asyncio
# 测试场景：验证取消或终止请求会收敛相关运行状态，并正确清理或保留应有资源；函数名 test_runtime_stops_after_four_failed_no_progress_steps 精确标识本用例的具体条件。
async def test_runtime_stops_after_four_failed_no_progress_steps(tmp_path) -> None:
    turn = 0

    # 辅助方法：model_call 实现测试替身在此调用阶段需要的最小行为。
    async def model_call(**_kwargs) -> ModelTurn:
        nonlocal turn
        turn += 1
        return ModelTurn(tool_calls=[ModelToolCall(f"call-{turn}", f"missing-{turn}", {})])

    runtime = AgentRuntime(
        model_call=model_call,
        tool_registry=create_default_registry(str(tmp_path), workflow_profile_id="general"),
        config=RuntimeConfig(no_progress_limit=4),
    )
    outcome = await runtime.run(system_prompt="safe", recent_messages=[])
    assert outcome.status == "stopped"
    assert outcome.stop_reason == "no_progress"
    assert outcome.tool_calls == 4


@pytest.mark.asyncio
# 测试场景：验证状态能够可靠持久化、重放或在重启后恢复，并保持记录之间的关联；函数名 test_coding_runtime_gets_one_recovery_turn_before_no_progress_stop 精确标识本用例的具体条件。
async def test_coding_runtime_gets_one_recovery_turn_before_no_progress_stop(tmp_path) -> None:
    model_messages: list[list[dict]] = []
    turn = 0

    # 辅助方法：model_call 实现测试替身在此调用阶段需要的最小行为。
    async def model_call(**kwargs) -> ModelTurn:
        nonlocal turn
        turn += 1
        model_messages.append([dict(item) for item in kwargs["messages"]])
        if turn == 1:
            return ModelTurn(
                tool_calls=[
                    write_call("edit-1", "candidate.txt", "working candidate")
                ]
            )
        if turn <= 5:
            return ModelTurn(tool_calls=[ModelToolCall(f"missing-{turn}", f"missing-{turn}", {})])
        return ModelTurn(content="Recovered the intended candidate and stopped experimenting.")

    runtime = AgentRuntime(
        model_call=model_call,
        tool_registry=create_default_registry(
            str(tmp_path),
            allowed_tool_names=["apply_patch", "validate"],
            workflow_profile_id="coding",
            permission_mode="full",
        ),
        config=RuntimeConfig(no_progress_limit=4),
    )

    outcome = await runtime.run(system_prompt="safe", recent_messages=[])

    assert outcome.status == "completed"
    assert outcome.tool_calls == 5
    recovery_context = "\n".join(
        str(item.get("content") or "") for item in model_messages[-1]
    )
    assert "恢复轮" in recovery_context
    assert "git status" in recovery_context
    recovery_events = [
        item for item in outcome.events if item.get("type") == "stagnation_recovery_started"
    ]
    assert len(recovery_events) == 1
    assert outcome.guard_snapshot["stagnation_recovery_count"] == 1


@pytest.mark.asyncio
# 测试场景：验证状态能够可靠持久化、重放或在重启后恢复，并保持记录之间的关联；函数名 test_coding_runtime_recovers_once_from_tool_errors_before_any_edit 精确标识本用例的具体条件。
async def test_coding_runtime_recovers_once_from_tool_errors_before_any_edit(tmp_path) -> None:
    turns = 0
    recovery_messages: list[dict] = []

    # 辅助方法：model_call 实现测试替身在此调用阶段需要的最小行为。
    async def model_call(**kwargs) -> ModelTurn:
        nonlocal turns, recovery_messages
        turns += 1
        if turns <= 4:
            return ModelTurn(
                tool_calls=[ModelToolCall(f"bad-{turns}", f"missing-{turns}", {})]
            )
        recovery_messages = [dict(item) for item in kwargs["messages"]]
        return ModelTurn(content="Stopped repeating invalid tool calls.")

    runtime = AgentRuntime(
        model_call=model_call,
        tool_registry=create_default_registry(
            str(tmp_path),
            allowed_tool_names=["read", "rg"],
            workflow_profile_id="debug",
        ),
        config=RuntimeConfig(no_progress_limit=4),
    )

    outcome = await runtime.run(system_prompt="safe", recent_messages=[])

    assert outcome.status == "completed"
    assert outcome.tool_calls == 4
    recovery_context = "\n".join(
        str(item.get("content") or "") for item in recovery_messages
    )
    assert "error_code" in recovery_context
    assert "不要重复相同调用" in recovery_context


@pytest.mark.asyncio
# 测试场景：验证时间、容量或上下文预算边界以及达到边界后的可观察处理结果；函数名 test_stream_activity_resets_model_idle_timeout 精确标识本用例的具体条件。
async def test_stream_activity_resets_model_idle_timeout(tmp_path) -> None:
    # 辅助方法：model_call 实现测试替身在此调用阶段需要的最小行为。
    async def model_call(*, on_delta, **_kwargs) -> ModelTurn:
        await asyncio.sleep(0.3)
        await on_delta("still working")
        await asyncio.sleep(0.3)
        return ModelTurn(content="done")

    runtime = AgentRuntime(
        model_call=model_call,
        tool_registry=create_default_registry(str(tmp_path)),
        config=RuntimeConfig(model_timeout_seconds=0.5, api_max_attempts=1),
    )

    outcome = await runtime.run(system_prompt="safe", recent_messages=[])

    assert outcome.status == "completed"
    assert outcome.output == "done"


@pytest.mark.asyncio
# 测试场景：验证该正常业务场景从输入准备到结果断言的完整链路；函数名 test_stream_idle_after_activity_is_not_retried 精确标识本用例的具体条件。
async def test_stream_idle_after_activity_is_not_retried(tmp_path) -> None:
    calls = 0
    cancelled = asyncio.Event()

    # 辅助方法：model_call 实现测试替身在此调用阶段需要的最小行为。
    async def model_call(*, on_delta, **_kwargs) -> ModelTurn:
        nonlocal calls
        calls += 1
        await on_delta("partial")
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    runtime = AgentRuntime(
        model_call=model_call,
        tool_registry=create_default_registry(str(tmp_path)),
        config=RuntimeConfig(model_timeout_seconds=0.01, api_max_attempts=3, api_base_delay=0),
    )

    outcome = await runtime.run(system_prompt="safe", recent_messages=[])

    assert outcome.status == "failed"
    assert calls == 1
    assert cancelled.is_set()
    assert any(
        event.get("type") == "model_failed"
        and event.get("error_type") == "PartialModelIdleTimeout"
        for event in outcome.events
    )


@pytest.mark.asyncio
# 测试场景：验证该正常业务场景从输入准备到结果断言的完整链路；函数名 test_runtime_resumes_by_executing_exact_approved_call 精确标识本用例的具体条件。
async def test_runtime_resumes_by_executing_exact_approved_call(tmp_path) -> None:
    turns = 0

    # 辅助方法：model_call 实现测试替身在此调用阶段需要的最小行为。
    async def model_call(**kwargs) -> ModelTurn:
        nonlocal turns
        turns += 1
        if turns == 1:
            return ModelTurn(
                tool_calls=[write_call("approved-write", "done.txt", "yes")]
            )
        assert kwargs["messages"][-1]["role"] == "tool"
        assert kwargs["messages"][-1]["tool_call_id"] == "approved-write"
        return ModelTurn(content="完成")

    runtime = AgentRuntime(
        model_call=model_call,
        tool_registry=create_default_registry(str(tmp_path), permission_mode="ask"),
        config=RuntimeConfig(no_progress_limit=4),
    )
    waiting = await runtime.run(system_prompt="safe", recent_messages=[], mode="auto")
    resumed = await runtime.resume_after_approval(waiting)
    assert resumed.status == "completed"
    assert resumed.mode == "auto"
    assert resumed.output == "完成"
    assert turns == 2
    assert (tmp_path / "done.txt").read_text(encoding="utf-8") == "yes\n"


@pytest.mark.asyncio
# 测试场景：验证权限、审批或敏感数据边界在完整调用链路中保持有效；函数名 test_production_context_path_keeps_original_task_after_approval_resume 精确标识本用例的具体条件。
async def test_production_context_path_keeps_original_task_after_approval_resume(tmp_path) -> None:
    task = "Write the approved file and keep this goal after resume."
    turns = 0

    # 辅助方法：model_call 实现测试替身在此调用阶段需要的最小行为。
    async def model_call(**kwargs) -> ModelTurn:
        nonlocal turns
        turns += 1
        assert any(item.get("role") == "user" and item.get("content") == task for item in kwargs["messages"])
        assert not any("<user_task>" in str(item.get("content") or "") for item in kwargs["messages"])
        if turns == 1:
            return ModelTurn(tool_calls=[
                write_call("approved-write", "goal.txt", "kept")
            ])
        return ModelTurn(content="done")

    runtime = AgentRuntime(
        model_call=model_call,
        tool_registry=create_default_registry(str(tmp_path), permission_mode="ask"),
    )
    waiting = await runtime.run(
        system_prompt="safe",
        recent_messages=[{"role": "user", "content": task}],
    )
    resumed = await runtime.resume_after_approval(waiting)

    assert resumed.status == "completed"
    assert turns == 2


@pytest.mark.asyncio
# 测试场景：验证权限、审批或敏感数据边界在完整调用链路中保持有效；函数名 test_full_compaction_keeps_current_request_through_approval_resume 精确标识本用例的具体条件。
async def test_full_compaction_keeps_current_request_through_approval_resume(tmp_path) -> None:
    task = "Create the approved report and do not lose this task after context compaction."
    calls = 0

    # 辅助方法：model_call 实现测试替身在此调用阶段需要的最小行为。
    async def model_call(**kwargs) -> ModelTurn:
        nonlocal calls
        calls += 1
        messages = kwargs["messages"]
        continuations = [
            item for item in messages
            if str(item.get("content") or "").startswith("<continuation-summary")
        ]
        assert len(continuations) == 1
        assert task in continuations[0]["content"]
        if calls == 1:
            # The retained prior observation remains a complete provider-valid
            # assistant/tool pair even as the old user turn is discarded.
            old_assistant = next(index for index, item in enumerate(messages) if item.get("role") == "assistant")
            assert messages[old_assistant + 1].get("tool_call_id") == "old-read"
            return ModelTurn(tool_calls=[
                write_call("approved-write", "report.txt", "approved"),
                ModelToolCall("second-read", "read", {"path": "report.txt"}),
            ])
        assert any(item.get("tool_call_id") == "approved-write" for item in messages)
        return ModelTurn(content="report complete")

    durable_events: list[dict] = []
    recent_messages = [
        {"role": "user", "content": "old background " + ("x" * 8_000)},
        {"role": "user", "content": task},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"id": "old-read", "function": {"name": "read_file", "arguments": "{}"}}],
        },
        {
            "role": "tool",
            "tool_call_id": "old-read",
            "name": "read_file",
            "content": "old observation " + ("z" * 2_500),
        },
    ]
    runtime = AgentRuntime(
        model_call=model_call,
        tool_registry=create_default_registry(str(tmp_path), permission_mode="ask"),
        context_manager=ContextManager(max_tokens=560),
        conversation_compactor=ConversationCompactor(
            model_call=lambda **_kwargs: {
                "choices": [{"message": {"content": "\n".join(
                    f"## {index}. {title}\nOld read completed; approved report remains."
                    for index, title in enumerate(COMPACTION_SECTION_TITLES, start=1)
                )}}]
            },
            preserve_recent_messages=2,
        ),
        event_sink=durable_events.append,
    )

    waiting = await runtime.run(
        system_prompt="Follow safety rules.",
        recent_messages=recent_messages,
    )
    assert waiting.error is None, waiting.error
    assert waiting.status == "awaiting_approval"
    resumed = await runtime.resume_after_approval(waiting)
    assert resumed.status == "completed"
    assert resumed.output == "report complete"
    assert (tmp_path / "report.txt").read_text(encoding="utf-8") == "approved\n"
    assert calls == 2
    compacted = [event for event in durable_events if event["type"] == "context_compaction_finished"]
    assert compacted and compacted[-1]["effective"] is True


@pytest.mark.asyncio
# 测试场景：验证并发或批量执行时的顺序、隔离性和最终状态一致性；函数名 test_approval_resume_completes_entire_multi_tool_batch 精确标识本用例的具体条件。
async def test_approval_resume_completes_entire_multi_tool_batch(tmp_path) -> None:
    (tmp_path / "before.txt").write_text("before", encoding="utf-8")
    turns = 0

    # 辅助方法：model_call 实现测试替身在此调用阶段需要的最小行为。
    async def model_call(**kwargs) -> ModelTurn:
        nonlocal turns
        turns += 1
        if turns == 1:
            return ModelTurn(tool_calls=[
                ModelToolCall("read-before", "read", {"path": "before.txt"}),
                write_call("write-middle", "created.txt", "created"),
                ModelToolCall("list-after", "glob", {"path": "."}),
            ])
        tool_ids = [message.get("tool_call_id") for message in kwargs["messages"] if message.get("role") == "tool"]
        assert tool_ids[-3:] == ["read-before", "write-middle", "list-after"]
        return ModelTurn(content="batch complete")

    runtime = AgentRuntime(model_call=model_call, tool_registry=create_default_registry(str(tmp_path), permission_mode="ask"))
    waiting = await runtime.run(system_prompt="safe", recent_messages=[])
    assert waiting.status == "awaiting_approval"
    resumed = await runtime.resume_after_approval(waiting)
    assert resumed.status == "completed"
    assert resumed.output == "batch complete"
    assert resumed.tool_calls == 3
    assert (tmp_path / "created.txt").read_text(encoding="utf-8") == "created\n"


@pytest.mark.asyncio
# 测试场景：验证并发或批量执行时的顺序、隔离性和最终状态一致性；函数名 test_multi_side_effect_batch_pauses_for_each_approval 精确标识本用例的具体条件。
async def test_multi_side_effect_batch_pauses_for_each_approval(tmp_path) -> None:
    turns = 0

    # 辅助方法：model_call 实现测试替身在此调用阶段需要的最小行为。
    async def model_call(**_kwargs) -> ModelTurn:
        nonlocal turns
        turns += 1
        if turns == 1:
            return ModelTurn(tool_calls=[
                write_call("write-1", "one.txt", "1"),
                write_call("write-2", "two.txt", "2"),
            ])
        return ModelTurn(content="done")

    runtime = AgentRuntime(model_call=model_call, tool_registry=create_default_registry(str(tmp_path), permission_mode="ask"))
    first = await runtime.run(system_prompt="safe", recent_messages=[])
    second = await runtime.resume_after_approval(first)
    assert second.status == "awaiting_approval"
    assert second.output_ledger is first.output_ledger
    assert second.pending_approval["id"] == "write-2"
    assert (tmp_path / "one.txt").exists()
    assert not (tmp_path / "two.txt").exists()
    final = await runtime.resume_after_approval(second)
    assert final.status == "completed"
    assert (tmp_path / "two.txt").exists()
    assert turns == 2


@pytest.mark.asyncio
async def test_approval_resume_validates_all_remaining_calls_before_any_dispatch(tmp_path) -> None:
    async def model_call(**_kwargs) -> ModelTurn:
        return ModelTurn(tool_calls=[
            write_call("write-1", "one.txt", "1"),
            write_call("write-2", "two.txt", "2"),
        ])

    runtime = AgentRuntime(
        model_call=model_call,
        tool_registry=create_default_registry(str(tmp_path), permission_mode="ask"),
    )
    waiting = await runtime.run(system_prompt="safe", recent_messages=[])
    waiting.pending_approval["remaining_calls"][0]["arguments"] = {
        "path": "tampered.txt",
        "content": "unsafe",
    }

    with pytest.raises(ValueError, match="approval.*ledger"):
        await runtime.resume_after_approval(waiting)

    assert not (tmp_path / "one.txt").exists()
    assert not (tmp_path / "two.txt").exists()
    assert not (tmp_path / "tampered.txt").exists()


@pytest.mark.asyncio
# 测试场景：验证时间、容量或上下文预算边界以及达到边界后的可观察处理结果；函数名 test_model_attempt_timeout_is_retried_and_bounded 精确标识本用例的具体条件。
async def test_model_attempt_timeout_is_retried_and_bounded(tmp_path) -> None:
    attempts = 0

    # 辅助方法：model_call 实现测试替身在此调用阶段需要的最小行为。
    async def model_call(**_kwargs) -> ModelTurn:
        nonlocal attempts
        attempts += 1
        await asyncio.sleep(0.03)
        return ModelTurn(content="too late")

    runtime = AgentRuntime(
        model_call=model_call,
        tool_registry=create_default_registry(str(tmp_path)),
        config=RuntimeConfig(model_timeout_seconds=0.001, api_max_attempts=3, api_base_delay=0),
    )
    outcome = await runtime.run(system_prompt="safe", recent_messages=[])
    assert outcome.status == "failed"
    assert attempts == 3


@pytest.mark.asyncio
# 测试场景：验证时间、容量或上下文预算边界以及达到边界后的可观察处理结果；函数名 test_synchronous_model_timeout_is_not_retried 精确标识本用例的具体条件。
async def test_synchronous_model_timeout_is_not_retried(tmp_path) -> None:
    attempts = 0

    # 辅助方法：model_call 实现测试替身在此调用阶段需要的最小行为。
    def model_call(**_kwargs) -> ModelTurn:
        nonlocal attempts
        attempts += 1
        time.sleep(0.05)
        return ModelTurn(content="late")

    runtime = AgentRuntime(
        model_call=model_call,
        tool_registry=create_default_registry(str(tmp_path)),
        config=RuntimeConfig(model_timeout_seconds=0.001, api_max_attempts=3, api_base_delay=0),
    )
    outcome = await runtime.run(system_prompt="safe", recent_messages=[])
    assert outcome.status == "failed"
    # Under load, wait_for may expire before the executor starts the first job;
    # either zero or one actual call is safe, but never a retrying second call.
    await asyncio.sleep(0.06)
    assert attempts <= 1


@pytest.mark.asyncio
# 测试场景：验证取消或终止请求会收敛相关运行状态，并正确清理或保留应有资源；函数名 test_resume_guard_stop_is_published_to_event_sink 精确标识本用例的具体条件。
async def test_resume_guard_stop_is_published_to_event_sink(tmp_path) -> None:
    published: list[dict] = []

    # 辅助方法：model_call 实现测试替身在此调用阶段需要的最小行为。
    async def model_call(**_kwargs) -> ModelTurn:
        return ModelTurn(tool_calls=[
            write_call("write", "x.txt", "x"),
            ModelToolCall("list", "glob", {"path": "."}),
        ])

    runtime = AgentRuntime(
        model_call=model_call,
        tool_registry=create_default_registry(str(tmp_path), permission_mode="ask"),
        event_sink=published.append,
        config=RuntimeConfig(max_tool_calls=1),
    )
    waiting = await runtime.run(system_prompt="safe", recent_messages=[])
    stopped = await runtime.resume_after_approval(waiting)
    assert stopped.status == "stopped"
    assert stopped.stop_reason == "max_tool_calls"
    assert any(event["type"] == "run_stopped" for event in published)


@pytest.mark.asyncio
async def test_approval_resume_rejects_ledger_mismatch_before_dispatch(tmp_path) -> None:
    async def model_call(**_kwargs) -> ModelTurn:
        return ModelTurn(tool_calls=[write_call("write-call", "allowed.txt", "safe")])

    runtime = AgentRuntime(
        model_call=model_call,
        tool_registry=create_default_registry(str(tmp_path), permission_mode="ask"),
    )
    waiting = await runtime.run(system_prompt="safe", recent_messages=[])
    waiting.pending_approval["arguments"] = {"path": "tampered.txt", "content": "unsafe"}

    with pytest.raises(ValueError, match="approval.*ledger"):
        await runtime.resume_after_approval(waiting)

    assert not (tmp_path / "allowed.txt").exists()
    assert not (tmp_path / "tampered.txt").exists()


@pytest.mark.asyncio
# 测试场景：验证该正常业务场景从输入准备到结果断言的完整链路；函数名 test_runtime_aggregates_usage_across_all_model_turns 精确标识本用例的具体条件。
async def test_runtime_aggregates_usage_across_all_model_turns(tmp_path) -> None:
    turn = 0

    # 辅助方法：model_call 实现测试替身在此调用阶段需要的最小行为。
    async def model_call(**_kwargs) -> ModelTurn:
        nonlocal turn
        turn += 1
        usage = {
            "request_count": 1,
            "input_tokens": turn * 10,
            "output_tokens": turn,
            "total_tokens": turn * 11,
            "cost_usd": turn / 100,
            "model_id": "demo-model",
            "provider": "demo",
        }
        if turn < 3:
            return ModelTurn(
                tool_calls=[ModelToolCall(f"read-{turn}", "glob", {"path": "."})],
                usage=usage,
            )
        return ModelTurn(content="done", usage=usage)

    runtime = AgentRuntime(model_call=model_call, tool_registry=create_default_registry(str(tmp_path)))
    outcome = await runtime.run(system_prompt="safe", recent_messages=[])
    assert outcome.status == "completed"
    assert outcome.usage == {
        "request_count": 3,
        "input_tokens": 60,
        "output_tokens": 6,
        "cache_creation_tokens": 0,
        "cache_read_tokens": 0,
        "total_tokens": 66,
        "cost_usd": 0.06,
        "model_connection_id": None,
        "model_id": "demo-model",
        "provider": "demo",
    }
    completed_event = next(event for event in outcome.events if event["type"] == "run_completed")
    assert completed_event["usage"] == outcome.usage


@pytest.mark.asyncio
# 测试场景：验证取消或终止请求会收敛相关运行状态，并正确清理或保留应有资源；函数名 test_explicit_token_budget_stops_before_executing_another_tool 精确标识本用例的具体条件。
async def test_explicit_token_budget_stops_before_executing_another_tool(tmp_path) -> None:
    calls = 0

    # 辅助方法：model_call 实现测试替身在此调用阶段需要的最小行为。
    async def model_call(**_kwargs) -> ModelTurn:
        nonlocal calls
        calls += 1
        return ModelTurn(
            tool_calls=[ModelToolCall("read", "glob", {"path": "."})],
            usage={
                "request_count": 1,
                "input_tokens": 18,
                "output_tokens": 2,
                "total_tokens": 20,
            },
        )

    runtime = AgentRuntime(
        model_call=model_call,
        tool_registry=create_default_registry(str(tmp_path)),
        config=RuntimeConfig(max_task_tokens=20),
    )

    outcome = await runtime.run(system_prompt="safe", recent_messages=[])

    assert calls == 1
    assert outcome.status == "stopped"
    assert outcome.stop_reason == "max_task_tokens"
    assert outcome.tool_calls == 0
    assert outcome.usage["total_tokens"] == 20


@pytest.mark.asyncio
# 测试场景：验证权限、审批或敏感数据边界在完整调用链路中保持有效；函数名 test_approval_resume_carries_usage_forward_without_double_counting 精确标识本用例的具体条件。
async def test_approval_resume_carries_usage_forward_without_double_counting(tmp_path) -> None:
    turns = 0

    # 辅助方法：model_call 实现测试替身在此调用阶段需要的最小行为。
    async def model_call(**_kwargs) -> ModelTurn:
        nonlocal turns
        turns += 1
        usage = {
            "request_count": 1,
            "input_tokens": 10,
            "output_tokens": 2,
            "total_tokens": 12,
            "model_id": "demo",
            "provider": "demo",
        }
        if turns == 1:
            return ModelTurn(
                tool_calls=[write_call("write", "x.txt", "x")],
                usage=usage,
            )
        return ModelTurn(content="done", usage=usage)

    runtime = AgentRuntime(model_call=model_call, tool_registry=create_default_registry(str(tmp_path), permission_mode="ask"))
    waiting = await runtime.run(system_prompt="safe", recent_messages=[])
    assert waiting.usage["request_count"] == 1
    resumed = await runtime.resume_after_approval(waiting)
    assert resumed.usage["request_count"] == 2
    assert resumed.usage["total_tokens"] == 24


@pytest.mark.asyncio
# 测试场景：验证权限、审批或敏感数据边界在完整调用链路中保持有效；函数名 test_approval_resume_preserves_seen_observations_for_no_progress_guard 精确标识本用例的具体条件。
async def test_approval_resume_preserves_seen_observations_for_no_progress_guard(tmp_path) -> None:
    (tmp_path / "stable.txt").write_text("unchanged", encoding="utf-8")
    turns = 0

    # 辅助方法：model_call 实现测试替身在此调用阶段需要的最小行为。
    async def model_call(**_kwargs) -> ModelTurn:
        nonlocal turns
        turns += 1
        if turns == 1:
            return ModelTurn(tool_calls=[
                ModelToolCall("read-first", "read", {"path": "stable.txt"}),
                write_call("approve-write", "new.txt", "new"),
            ])
        if turns == 2:
            return ModelTurn(tool_calls=[ModelToolCall("read-again", "read", {"path": "stable.txt"})])
        return ModelTurn(tool_calls=[ModelToolCall(f"missing-{turns}", f"missing-{turns}", {})])

    runtime = AgentRuntime(
        model_call=model_call,
        tool_registry=create_default_registry(str(tmp_path), permission_mode="ask"),
        config=RuntimeConfig(no_progress_limit=4),
    )
    waiting = await runtime.run(system_prompt="safe", recent_messages=[])
    resumed = await runtime.resume_after_approval(waiting)
    assert resumed.status == "stopped"
    assert resumed.stop_reason == "no_progress"
    assert turns == 5
    assert resumed.guard_snapshot["stagnation_recovery_count"] == 0


@pytest.mark.asyncio
# 测试场景：验证失败会保留可诊断信息并收敛为一致、可恢复的状态；函数名 test_event_sink_failure_is_reported_without_stranding_run 精确标识本用例的具体条件。
async def test_event_sink_failure_is_reported_without_stranding_run(tmp_path) -> None:
    # 辅助方法：model_call 实现测试替身在此调用阶段需要的最小行为。
    async def model_call(**_kwargs) -> ModelTurn:
        return ModelTurn(content="ok")

    # 辅助方法：broken_sink 实现测试替身在此调用阶段需要的最小行为。
    def broken_sink(_event) -> None:
        raise OSError("database unavailable")

    runtime = AgentRuntime(
        model_call=model_call,
        tool_registry=create_default_registry(str(tmp_path)),
        event_sink=broken_sink,
    )
    outcome = await runtime.run(system_prompt="safe", recent_messages=[])
    assert outcome.status == "completed"
    assert any(event["type"] == "event_sink_failed" for event in outcome.events)


@pytest.mark.asyncio
# 测试场景：验证时间、容量或上下文预算边界以及达到边界后的可观察处理结果；函数名 test_event_sink_timeout_is_reported_without_stranding_run 精确标识本用例的具体条件。
async def test_event_sink_timeout_is_reported_without_stranding_run(tmp_path) -> None:
    # 辅助方法：model_call 实现测试替身在此调用阶段需要的最小行为。
    async def model_call(**_kwargs) -> ModelTurn:
        return ModelTurn(content="ok")

    # 辅助方法：stuck_sink 实现测试替身在此调用阶段需要的最小行为。
    async def stuck_sink(_event) -> None:
        await asyncio.Event().wait()

    runtime = AgentRuntime(
        model_call=model_call,
        tool_registry=create_default_registry(str(tmp_path)),
        event_sink=stuck_sink,
        config=RuntimeConfig(event_sink_timeout_seconds=0.001),
    )
    outcome = await runtime.run(system_prompt="safe", recent_messages=[])
    assert outcome.status == "completed"
    assert any(
        event["type"] == "event_sink_failed" and event["error_kind"] == "timeout"
        for event in outcome.events
    )


@pytest.mark.asyncio
# 测试场景：验证该正常业务场景从输入准备到结果断言的完整链路；函数名 test_assistant_deltas_use_only_transient_stream_sink 精确标识本用例的具体条件。
async def test_assistant_deltas_use_only_transient_stream_sink(tmp_path) -> None:
    durable_events: list[dict] = []
    transient_events: list[dict] = []

    # 辅助方法：model_call 实现测试替身在此调用阶段需要的最小行为。
    async def model_call(**kwargs) -> ModelTurn:  # type: ignore[no-untyped-def]
        await kwargs["on_delta"]("one ")
        await kwargs["on_delta"]("two")
        return ModelTurn(content="one two")

    runtime = AgentRuntime(
        model_call=model_call,
        tool_registry=create_default_registry(str(tmp_path)),
        event_sink=durable_events.append,
        stream_sink=transient_events.append,
    )

    outcome = await runtime.run(system_prompt="safe", recent_messages=[])

    assert outcome.status == "completed"
    assert [event["delta"] for event in transient_events] == ["one ", "two"]
    assert all(event["type"] == "assistant_delta" for event in transient_events)
    assert not any(event["type"] == "assistant_delta" for event in durable_events)
    assert not any(event["type"] == "assistant_delta" for event in outcome.events)


@pytest.mark.asyncio
# 测试场景：验证 provider reasoning 只用于协议续接，不进入用户时间线。
async def test_provider_reasoning_is_not_published_to_user_timeline(tmp_path) -> None:
    durable_events: list[dict] = []
    transient_events: list[dict] = []

    # 辅助方法：model_call 实现测试替身在此调用阶段需要的最小行为。
    async def model_call(**kwargs) -> ModelTurn:  # type: ignore[no-untyped-def]
        assert "on_thought_delta" in kwargs
        await kwargs["on_thought_delta"]("先看天气")
        await kwargs["on_thought_delta"]("，再给结论")
        await kwargs["on_delta"]("今天晴。")
        return ModelTurn(content="今天晴。", reasoning_content="内部摘要")

    runtime = AgentRuntime(
        model_call=model_call,
        tool_registry=create_default_registry(str(tmp_path)),
        event_sink=durable_events.append,
        stream_sink=transient_events.append,
    )

    outcome = await runtime.run(system_prompt="safe", recent_messages=[])

    assert outcome.status == "completed"
    assert not any(event["type"] == "thought_delta" for event in transient_events)
    assert not any(event["type"] == "thought_summary" for event in durable_events)


@pytest.mark.asyncio
# 测试场景：验证该正常业务场景从输入准备到结果断言的完整链路；函数名 test_provider_web_search_is_visible_without_local_dispatch 精确标识本用例的具体条件。
async def test_provider_web_search_is_visible_without_local_dispatch(tmp_path) -> None:
    durable_events: list[dict] = []

    # 辅助方法：model_call 实现测试替身在此调用阶段需要的最小行为。
    async def model_call(**_kwargs) -> ModelTurn:
        return ModelTurn(
            content="检索完成。",
            provider_payload={
                "protocol": "responses",
                "items": [{
                    "type": "web_search_call",
                    "id": "ws_1",
                    "status": "completed",
                    "action": {
                        "type": "search",
                        "query": "latest release",
                        "sources": [{"url": "https://example.com/release"}],
                    },
                }],
            },
        )

    runtime = AgentRuntime(
        model_call=model_call,
        tool_registry=create_default_registry(str(tmp_path)),
        event_sink=durable_events.append,
    )
    outcome = await runtime.run(system_prompt="safe", recent_messages=[])

    assert outcome.status == "completed"
    assert outcome.tool_calls == 1
    assert provider_web_search_calls(outcome.messages[-1]["_pgagent_provider"]) == [{
        "id": "ws_1", "query": "latest release", "source_count": 1, "ok": True,
    }]
    hosted_events = [event for event in durable_events if event.get("tool_call_id") == "ws_1"]
    assert [event["type"] for event in hosted_events] == ["tool_started", "tool_finished"]
    assert hosted_events[0]["arguments"] == {"query": {"text": "latest release", "chars": 14}}
    assert hosted_events[1]["source_count"] == 1


@pytest.mark.asyncio
# 测试场景：验证该正常业务场景从输入准备到结果断言的完整链路；函数名 test_tool_step_thought_summary_hides_memory_citation 精确标识本用例的具体条件。
async def test_tool_step_thought_summary_hides_memory_citation(tmp_path) -> None:
    durable_events: list[dict] = []
    calls = 0

    # 辅助方法：model_call 实现测试替身在此调用阶段需要的最小行为。
    async def model_call(**_kwargs) -> ModelTurn:
        nonlocal calls
        calls += 1
        if calls == 1:
            return ModelTurn(
                content=(
                    "I will inspect the workspace.\n"
                    '<pgagent-memory-citation>{"memory_ids":["m1"],"note":"used"}'
                    "</pgagent-memory-citation>"
                ),
                tool_calls=[ModelToolCall("list", "glob", {"path": "."})],
            )
        return ModelTurn(content="done")

    runtime = AgentRuntime(
        model_call=model_call,
        tool_registry=create_default_registry(str(tmp_path)),
        event_sink=durable_events.append,
    )
    outcome = await runtime.run(system_prompt="safe", recent_messages=[])

    assert outcome.status == "completed"
    summaries = [event["summary"] for event in durable_events if event["type"] == "thought_summary"]
    assert "I will inspect the workspace." in summaries
    assert all("pgagent-memory-citation" not in summary for summary in summaries)
    assert not any(event["type"] == "progress" for event in durable_events)


@pytest.mark.asyncio
# 测试场景：验证该正常业务场景从输入准备到结果断言的完整链路；函数名 test_tool_step_has_no_fixed_progress_fallback_without_model_status 精确标识本用例的具体条件。
async def test_tool_step_has_no_fixed_progress_fallback_without_model_status(tmp_path) -> None:
    durable_events: list[dict] = []
    calls = 0

    # 辅助方法：model_call 实现测试替身在此调用阶段需要的最小行为。
    async def model_call(**_kwargs) -> ModelTurn:
        nonlocal calls
        calls += 1
        if calls == 1:
            return ModelTurn(
                tool_calls=[
                    ModelToolCall("list", "glob", {"path": "."}),
                    ModelToolCall("read", "read", {"path": "missing.txt"}),
                ],
            )
        return ModelTurn(content="done")

    runtime = AgentRuntime(
        model_call=model_call,
        tool_registry=create_default_registry(str(tmp_path)),
        event_sink=durable_events.append,
    )
    outcome = await runtime.run(system_prompt="safe", recent_messages=[])

    assert outcome.status == "completed"
    assert not any(event["type"] in {"progress", "thought_summary"} for event in durable_events)


@pytest.mark.asyncio
# 测试场景：验证该正常业务场景从输入准备到结果断言的完整链路；函数名 test_runtime_keeps_legacy_model_callable_without_delta_keyword_compatible 精确标识本用例的具体条件。
async def test_runtime_keeps_legacy_model_callable_without_delta_keyword_compatible(tmp_path) -> None:
    # 辅助方法：model_call 实现测试替身在此调用阶段需要的最小行为。
    async def model_call(messages, tools, mode) -> ModelTurn:  # type: ignore[no-untyped-def]
        assert isinstance(messages, list)
        assert isinstance(tools, list)
        assert mode == "auto"
        return ModelTurn(content="compatible")

    runtime = AgentRuntime(
        model_call=model_call,
        tool_registry=create_default_registry(str(tmp_path)),
    )
    outcome = await runtime.run(system_prompt="safe", recent_messages=[])
    assert outcome.status == "completed"
    assert outcome.output == "compatible"


@pytest.mark.asyncio
async def test_legacy_modelturn_response_reuses_prior_output_ledger(tmp_path) -> None:
    ledger = TurnLedger()
    ledger.accept_response(NormalizedModelResponse(
        response_id="prior-tool-response",
        status="completed",
        items=[LocalToolCallItem(
            response_id="prior-tool-response",
            item_id="prior-tool",
            call_id="prior-tool-call",
            tool_name="glob",
            arguments={"path": "."},
        )],
    ))
    ledger.take_local_calls()
    ledger.mark_local_running("prior-tool-call")
    ledger.commit_local_result("prior-tool-call", tool_name="glob")

    async def model_call(**_kwargs) -> ModelTurn:
        return ModelTurn(content="legacy final")

    runtime = AgentRuntime(
        model_call=model_call,
        tool_registry=create_default_registry(str(tmp_path)),
    )
    outcome = await runtime.run(
        system_prompt="safe",
        recent_messages=[],
        prior_output_ledger=ledger,
    )

    assert outcome.status == "completed"
    assert outcome.output == "legacy final"
    assert outcome.output_ledger is ledger
    assert outcome.output_ledger.local_call_count == 1
    assert outcome.output_ledger.local_status("prior-tool-call") is LocalToolStatus.RESULT_COMMITTED


@pytest.mark.asyncio
async def test_runtime_consumes_normalized_sidecar_for_follow_up_and_local_dispatch(tmp_path) -> None:
    calls = 0

    async def model_call(**_kwargs):  # type: ignore[no-untyped-def]
        nonlocal calls
        calls += 1
        if calls == 1:
            normalized = NormalizedModelResponse(
                response_id="resp-tools",
                status="completed",
                items=[
                    AssistantMessageItem(
                        response_id="resp-tools",
                        item_id="msg-progress",
                        output_index=0,
                        content="正在检查。",
                        phase="commentary",
                    ),
                    LocalToolCallItem(
                        response_id="resp-tools",
                        item_id="tool-1",
                        output_index=1,
                        call_id="read-1",
                        tool_name="glob",
                        arguments={"path": "."},
                    ),
                ],
            )
            return {
                "content": "不应从旧扁平字段决定正文",
                "tool_calls": [{"id": "wrong", "function": {"name": "missing", "arguments": "{}"}}],
                "_pgagent_normalized_response": normalized,
            }
        return {
            "content": "旧字段也不应覆盖 sidecar",
            "_pgagent_normalized_response": NormalizedModelResponse(
                response_id="resp-final",
                status="completed",
                items=[AssistantMessageItem(
                    response_id="resp-final",
                    item_id="msg-final",
                    content="检查完成。",
                    phase="final_answer",
                    end_turn=True,
                )],
            ),
        }

    runtime = AgentRuntime(
        model_call=model_call,
        tool_registry=create_default_registry(str(tmp_path)),
    )
    outcome = await runtime.run(system_prompt="safe", recent_messages=[])

    assert calls == 2
    assert outcome.status == "completed"
    assert outcome.output == "检查完成。"
    assert outcome.tool_calls == 1
    assert any(message.get("tool_call_id") == "read-1" for message in outcome.messages)
    assert not any(message.get("tool_call_id") == "wrong" for message in outcome.messages)


@pytest.mark.asyncio
async def test_runtime_follows_up_on_explicit_end_turn_false_without_tool(tmp_path) -> None:
    calls = 0

    async def model_call(**_kwargs):  # type: ignore[no-untyped-def]
        nonlocal calls
        calls += 1
        end_turn = False if calls == 1 else True
        return {
            "content": "legacy",
            "_pgagent_normalized_response": NormalizedModelResponse(
                response_id=f"resp-{calls}",
                status="completed",
                items=[AssistantMessageItem(
                    response_id=f"resp-{calls}",
                    item_id=f"msg-{calls}",
                    content="继续处理" if calls == 1 else "最终结果",
                    end_turn=end_turn,
                )],
            ),
        }

    runtime = AgentRuntime(
        model_call=model_call,
        tool_registry=create_default_registry(str(tmp_path)),
    )
    outcome = await runtime.run(system_prompt="safe", recent_messages=[])

    assert calls == 2
    assert outcome.status == "completed"
    assert outcome.output == "最终结果"


@pytest.mark.asyncio
async def test_runtime_does_not_dispatch_local_item_from_incomplete_response(tmp_path) -> None:
    dispatched = 0

    async def model_call(**_kwargs):  # type: ignore[no-untyped-def]
        return {
            "_pgagent_normalized_response": NormalizedModelResponse(
                response_id="resp-incomplete",
                status="in_progress",
                items=[LocalToolCallItem(
                    response_id="resp-incomplete",
                    item_id="tool-incomplete",
                    call_id="write-incomplete",
                    tool_name="apply_patch",
                    arguments={"patch": "must not run"},
                )],
            ),
        }

    runtime = AgentRuntime(
        model_call=model_call,
        tool_registry=create_default_registry(str(tmp_path)),
    )

    async def dispatch(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        nonlocal dispatched
        dispatched += 1
        return ToolResult("apply_patch", True, "unexpected")

    runtime._dispatch_tool = dispatch  # type: ignore[method-assign]
    outcome = await runtime.run(system_prompt="safe", recent_messages=[])

    assert outcome.status == "failed"
    assert outcome.error == "model_response_incomplete"
    assert outcome.tool_calls == 0
    assert dispatched == 0


@pytest.mark.asyncio
async def test_runtime_rejects_empty_hosted_only_response_without_run_completed(tmp_path) -> None:
    from src.model.output import HostedToolItem

    async def model_call(**_kwargs):  # type: ignore[no-untyped-def]
        return {
            "_pgagent_normalized_response": NormalizedModelResponse(
                response_id="resp-hosted-only",
                status="completed",
                items=[HostedToolItem(
                    response_id="resp-hosted-only",
                    item_id="hosted-only",
                    call_id="hosted-call",
                    tool_name="web_search",
                )],
            ),
        }

    outcome = await AgentRuntime(
        model_call=model_call,
        tool_registry=create_default_registry(str(tmp_path)),
    ).run(system_prompt="safe", recent_messages=[])

    assert outcome.status == "failed"
    assert outcome.error == "empty_model_output"
    assert not any(event["type"] == "run_completed" for event in outcome.events)


@pytest.mark.asyncio
async def test_runtime_rejects_tool_result_name_mismatch_before_transcript_commit(tmp_path) -> None:
    turns = 0

    async def model_call(**_kwargs):  # type: ignore[no-untyped-def]
        nonlocal turns
        turns += 1
        if turns > 1:
            return ModelTurn(content="must not reach follow-up")
        return {
            "_pgagent_normalized_response": NormalizedModelResponse(
                response_id="resp-tool",
                status="completed",
                items=[LocalToolCallItem(
                    response_id="resp-tool",
                    item_id="tool-read",
                    call_id="read-1",
                    tool_name="glob",
                    arguments={"path": "."},
                )],
            ),
        }

    runtime = AgentRuntime(
        model_call=model_call,
        tool_registry=create_default_registry(str(tmp_path)),
    )

    async def wrong_result(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        return ToolResult("apply_patch", True, "wrong tool result")

    runtime._dispatch_tool = wrong_result  # type: ignore[method-assign]
    outcome = await runtime.run(system_prompt="safe", recent_messages=[])

    assert outcome.status == "failed"
    assert outcome.error == "ToolResultMismatch"
    assert turns == 1
    assert not any(message.get("role") == "tool" for message in outcome.messages)


@pytest.mark.asyncio
async def test_tool_result_is_not_committed_when_transcript_construction_fails(
    tmp_path,
    monkeypatch,
) -> None:
    import src.agent.engine as agent_engine

    ledger = TurnLedger()
    monkeypatch.setattr(agent_engine, "TurnLedger", lambda: ledger)

    async def model_call(**_kwargs):  # type: ignore[no-untyped-def]
        return {
            "_pgagent_normalized_response": NormalizedModelResponse(
                response_id="resp-tool-transcript",
                status="completed",
                items=[LocalToolCallItem(
                    response_id="resp-tool-transcript",
                    item_id="tool-glob",
                    call_id="glob-1",
                    tool_name="glob",
                    arguments={"path": "."},
                )],
            ),
        }

    runtime = AgentRuntime(
        model_call=model_call,
        tool_registry=create_default_registry(str(tmp_path)),
    )

    def fail_transcript(**_kwargs):  # type: ignore[no-untyped-def]
        raise TypeError("transcript construction failed")

    runtime._prepare_tool_result_message = fail_transcript  # type: ignore[method-assign]

    with pytest.raises(TypeError, match="transcript construction failed"):
        await runtime.run(system_prompt="safe", recent_messages=[])

    assert ledger.local_status("glob-1") is LocalToolStatus.RUNNING


# 辅助函数：_completed 封装本组测试重复使用的输入准备、状态查询或测试替身行为。
async def _completed() -> None:
    return None
