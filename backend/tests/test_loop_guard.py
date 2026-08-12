from __future__ import annotations

import asyncio
import time

import pytest

from app.runtime.context import ContextManager
from app.runtime.engine import AgentRuntime, ModelToolCall, ModelTurn, RuntimeConfig, merge_usage
from app.runtime.errors import APIErrorKind, call_with_retry, classify_api_error
from app.runtime.guards import LoopGuard
from app.tools import create_default_registry


class StatusError(RuntimeError):
    def __init__(self, status_code: int) -> None:
        super().__init__(f"HTTP {status_code}")
        self.status_code = status_code


class ManualClock:
    def __init__(self) -> None:
        self.value = 0.0

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


def test_usage_merge_rejects_model_identity_drift() -> None:
    with pytest.raises(ValueError, match="模型身份发生变化"):
        merge_usage(
            {"request_count": 1, "model_connection_id": "connection-1", "model_id": "model-1"},
            {"request_count": 1, "model_connection_id": "connection-2", "model_id": "model-2"},
        )


def test_identical_call_is_stopped_on_third_occurrence() -> None:
    guard = LoopGuard(identical_limit=3)
    assert not guard.record_tool_call("read_file", {"path": "a", "line": 1}).stop
    # Canonical JSON makes argument order irrelevant.
    assert not guard.record_tool_call("read_file", {"line": 1, "path": "a"}).stop
    decision = guard.record_tool_call("read_file", {"path": "a", "line": 1})
    assert decision.stop
    assert decision.code == "repeated_tool_call"


def test_no_progress_is_stopped_after_four_steps() -> None:
    guard = LoopGuard(no_progress_limit=4)
    for _ in range(3):
        assert not guard.record_progress(False).stop
    decision = guard.record_progress(False)
    assert decision.stop
    assert decision.code == "no_progress"
    assert guard.no_progress_count == 4


def test_real_progress_resets_no_progress_counter() -> None:
    guard = LoopGuard(no_progress_limit=2)
    guard.record_progress(False)
    guard.record_progress(True)
    assert not guard.record_progress(False).stop


def test_zero_hard_limits_allow_large_runs_but_keep_anti_loop_guards() -> None:
    guard = LoopGuard(max_steps=0, max_calls=0, identical_limit=3, no_progress_limit=4)
    for index in range(250):
        assert not guard.before_step().stop
        assert not guard.before_tool_call("read_file", {"path": f"file-{index}"}).stop
        assert not guard.record_progress(True).stop
    assert guard.steps == 250
    assert guard.calls == 250


@pytest.mark.asyncio
async def test_active_runtime_fuse_stops_without_restoring_step_or_tool_limits(tmp_path) -> None:
    clock = ManualClock()

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
async def test_active_runtime_accumulates_across_approval_pause(tmp_path) -> None:
    clock = ManualClock()
    turns = 0

    async def model_call(**_kwargs) -> ModelTurn:
        nonlocal turns
        turns += 1
        if turns == 1:
            clock.advance(0.6)
            return ModelTurn(tool_calls=[
                ModelToolCall("write", "write_file", {"path": "x.txt", "content": "x"})
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
async def test_provider_timeout_is_not_misclassified_as_run_fuse(tmp_path) -> None:
    clock = ManualClock()
    attempts = 0

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
async def test_observation_history_is_not_truncated_to_200_before_approval(tmp_path) -> None:
    calls = [
        ModelToolCall(f"missing-{index}", f"missing-{index}", {})
        for index in range(250)
    ]
    calls.append(ModelToolCall("write", "write_file", {"path": "x.txt", "content": "x"}))

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
async def test_auth_failure_is_not_retried() -> None:
    attempts = 0

    async def operation() -> None:
        nonlocal attempts
        attempts += 1
        raise StatusError(401)

    with pytest.raises(StatusError):
        await call_with_retry(operation, max_attempts=3, sleep=lambda _delay: _completed())
    assert attempts == 1
    assert classify_api_error(StatusError(403)) == APIErrorKind.AUTH


@pytest.mark.asyncio
async def test_rate_limit_retries_at_most_three_total_attempts() -> None:
    attempts = 0
    delays: list[float] = []

    async def operation() -> str:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise StatusError(429)
        return "ok"

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
async def test_runtime_stops_repeated_model_tool_call(tmp_path) -> None:
    async def model_call(**_kwargs) -> ModelTurn:
        return ModelTurn(tool_calls=[ModelToolCall("same", "list_files", {"path": "."})])

    runtime = AgentRuntime(
        model_call=model_call,
        tool_registry=create_default_registry(str(tmp_path)),
        config=RuntimeConfig(api_base_delay=0),
    )
    outcome = await runtime.run(system_prompt="safe", recent_messages=[])
    assert outcome.status == "stopped"
    assert outcome.stop_reason == "repeated_tool_call"
    assert outcome.tool_calls == 3


@pytest.mark.asyncio
async def test_runtime_pauses_before_side_effect(tmp_path) -> None:
    async def model_call(**_kwargs) -> ModelTurn:
        return ModelTurn(
            tool_calls=[ModelToolCall("write-1", "write_file", {"path": "x.txt", "content": "hello"})]
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
async def test_runtime_stops_after_four_failed_no_progress_steps(tmp_path) -> None:
    turn = 0

    async def model_call(**_kwargs) -> ModelTurn:
        nonlocal turn
        turn += 1
        return ModelTurn(tool_calls=[ModelToolCall(f"call-{turn}", f"missing-{turn}", {})])

    runtime = AgentRuntime(
        model_call=model_call,
        tool_registry=create_default_registry(str(tmp_path)),
    )
    outcome = await runtime.run(system_prompt="safe", recent_messages=[])
    assert outcome.status == "stopped"
    assert outcome.stop_reason == "no_progress"
    assert outcome.tool_calls == 4


@pytest.mark.asyncio
async def test_runtime_resumes_by_executing_exact_approved_call(tmp_path) -> None:
    turns = 0

    async def model_call(**kwargs) -> ModelTurn:
        nonlocal turns
        turns += 1
        if turns == 1:
            return ModelTurn(
                tool_calls=[ModelToolCall("approved-write", "write_file", {"path": "done.txt", "content": "yes"})]
            )
        assert kwargs["messages"][-1]["role"] == "tool"
        assert kwargs["messages"][-1]["tool_call_id"] == "approved-write"
        return ModelTurn(content="完成")

    runtime = AgentRuntime(model_call=model_call, tool_registry=create_default_registry(str(tmp_path), permission_mode="ask"))
    waiting = await runtime.run(system_prompt="safe", recent_messages=[], mode="auto")
    resumed = await runtime.resume_after_approval(waiting)
    assert resumed.status == "completed"
    assert resumed.mode == "auto"
    assert resumed.output == "完成"
    assert turns == 2
    assert (tmp_path / "done.txt").read_text(encoding="utf-8") == "yes"


@pytest.mark.asyncio
async def test_runtime_trim_keeps_current_task_anchor_through_approval_resume(tmp_path) -> None:
    task = "Create the approved report and do not lose this task after context compaction."
    calls = 0

    async def model_call(**kwargs) -> ModelTurn:
        nonlocal calls
        calls += 1
        messages = kwargs["messages"]
        anchors = [item for item in messages if ContextManager.is_task_anchor(item)]
        assert len(anchors) == 1
        assert task in anchors[0]["content"]
        if calls == 1:
            # The retained prior observation remains a complete provider-valid
            # assistant/tool pair even as the old user turn is discarded.
            old_assistant = next(index for index, item in enumerate(messages) if item.get("role") == "assistant")
            assert messages[old_assistant + 1].get("tool_call_id") == "old-read"
            return ModelTurn(tool_calls=[
                ModelToolCall("approved-write", "write_file", {"path": "report.txt", "content": "approved"}),
                ModelToolCall("second-read", "read_file", {"path": "report.txt"}),
            ])
        assert any(item.get("tool_call_id") == "approved-write" for item in messages)
        return ModelTurn(content="report complete")

    durable_events: list[dict] = []
    recent_messages = [
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
    assert (tmp_path / "report.txt").read_text(encoding="utf-8") == "approved"
    assert calls == 2
    compacted = [event for event in durable_events if event["type"] == "context_compacted"]
    assert compacted and compacted[-1]["task_anchor_preserved"] is True


@pytest.mark.asyncio
async def test_approval_resume_completes_entire_multi_tool_batch(tmp_path) -> None:
    (tmp_path / "before.txt").write_text("before", encoding="utf-8")
    turns = 0

    async def model_call(**kwargs) -> ModelTurn:
        nonlocal turns
        turns += 1
        if turns == 1:
            return ModelTurn(tool_calls=[
                ModelToolCall("read-before", "read_file", {"path": "before.txt"}),
                ModelToolCall("write-middle", "write_file", {"path": "created.txt", "content": "created"}),
                ModelToolCall("list-after", "list_files", {"path": "."}),
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
    assert (tmp_path / "created.txt").read_text(encoding="utf-8") == "created"


@pytest.mark.asyncio
async def test_multi_side_effect_batch_pauses_for_each_approval(tmp_path) -> None:
    turns = 0

    async def model_call(**_kwargs) -> ModelTurn:
        nonlocal turns
        turns += 1
        if turns == 1:
            return ModelTurn(tool_calls=[
                ModelToolCall("write-1", "write_file", {"path": "one.txt", "content": "1"}),
                ModelToolCall("write-2", "write_file", {"path": "two.txt", "content": "2"}),
            ])
        return ModelTurn(content="done")

    runtime = AgentRuntime(model_call=model_call, tool_registry=create_default_registry(str(tmp_path), permission_mode="ask"))
    first = await runtime.run(system_prompt="safe", recent_messages=[])
    second = await runtime.resume_after_approval(first)
    assert second.status == "awaiting_approval"
    assert second.pending_approval["id"] == "write-2"
    assert (tmp_path / "one.txt").exists()
    assert not (tmp_path / "two.txt").exists()
    final = await runtime.resume_after_approval(second)
    assert final.status == "completed"
    assert (tmp_path / "two.txt").exists()
    assert turns == 2


@pytest.mark.asyncio
async def test_model_attempt_timeout_is_retried_and_bounded(tmp_path) -> None:
    attempts = 0

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
async def test_synchronous_model_timeout_is_not_retried(tmp_path) -> None:
    attempts = 0

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
async def test_resume_guard_stop_is_published_to_event_sink(tmp_path) -> None:
    published: list[dict] = []

    async def model_call(**_kwargs) -> ModelTurn:
        return ModelTurn(tool_calls=[
            ModelToolCall("write", "write_file", {"path": "x.txt", "content": "x"}),
            ModelToolCall("list", "list_files", {"path": "."}),
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
async def test_runtime_aggregates_usage_across_all_model_turns(tmp_path) -> None:
    turn = 0

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
                tool_calls=[ModelToolCall(f"read-{turn}", "list_files", {"path": ".", "turn": turn})],
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
async def test_approval_resume_carries_usage_forward_without_double_counting(tmp_path) -> None:
    turns = 0

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
                tool_calls=[ModelToolCall("write", "write_file", {"path": "x.txt", "content": "x"})],
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
async def test_approval_resume_preserves_seen_observations_for_no_progress_guard(tmp_path) -> None:
    (tmp_path / "stable.txt").write_text("unchanged", encoding="utf-8")
    turns = 0

    async def model_call(**_kwargs) -> ModelTurn:
        nonlocal turns
        turns += 1
        if turns == 1:
            return ModelTurn(tool_calls=[
                ModelToolCall("read-first", "read_file", {"path": "stable.txt"}),
                ModelToolCall("approve-write", "write_file", {"path": "new.txt", "content": "new"}),
            ])
        if turns == 2:
            return ModelTurn(tool_calls=[ModelToolCall("read-again", "read_file", {"path": "stable.txt"})])
        return ModelTurn(tool_calls=[ModelToolCall(f"missing-{turns}", f"missing-{turns}", {})])

    runtime = AgentRuntime(model_call=model_call, tool_registry=create_default_registry(str(tmp_path), permission_mode="ask"))
    waiting = await runtime.run(system_prompt="safe", recent_messages=[])
    resumed = await runtime.resume_after_approval(waiting)
    assert resumed.status == "stopped"
    assert resumed.stop_reason == "no_progress"
    assert turns == 5


@pytest.mark.asyncio
async def test_event_sink_failure_is_reported_without_stranding_run(tmp_path) -> None:
    async def model_call(**_kwargs) -> ModelTurn:
        return ModelTurn(content="ok")

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
async def test_event_sink_timeout_is_reported_without_stranding_run(tmp_path) -> None:
    async def model_call(**_kwargs) -> ModelTurn:
        return ModelTurn(content="ok")

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
async def test_assistant_deltas_use_only_transient_stream_sink(tmp_path) -> None:
    durable_events: list[dict] = []
    transient_events: list[dict] = []

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
async def test_runtime_keeps_legacy_model_callable_without_delta_keyword_compatible(tmp_path) -> None:
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


async def _completed() -> None:
    return None
