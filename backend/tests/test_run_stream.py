"""验证运行事件代理的历史回放、生命周期事件发布和跨线程订阅投递。

测试通过 fixture 或辅助函数准备隔离环境，再调用真实服务、路由或运行时，并检查返回值、持久化状态与可观察副作用。
变量约定：tmp_path/monkeypatch 提供隔离环境，client/store/runtime 驱动被测链路，各类 *_id 串联持久化实体，payload 表示输入，response/result 表示实际输出，expected 表示期望值。
"""

from __future__ import annotations

import asyncio

import pytest

from src.runs.stream import RunStreamBroker, TERMINAL_EVENT_TYPES, run_stream_broker


@pytest.mark.asyncio
# 测试场景：验证状态能够可靠持久化、重放或在重启后恢复，并保持记录之间的关联；函数名 test_broker_replays_started_events_then_delivers_lifecycle_and_terminal 精确标识本用例的具体条件。
async def test_broker_replays_started_events_then_delivers_lifecycle_and_terminal() -> None:
    broker = RunStreamBroker(replay_size=8, queue_size=8)
    started = broker.publish("run-1", {"type": "context_prepared", "estimated_tokens": 12})

    replay, subscription = broker.subscribe("run-1")
    try:
        assert [event["type"] for event in replay] == ["context_prepared"]
        assert replay[0]["event_id"] == started["event_id"]

        broker.publish("run-1", {"type": "tool_finished", "tool_name": "read_file"})
        lifecycle = await asyncio.wait_for(subscription.queue.get(), timeout=1)
        assert lifecycle["type"] == "tool_finished"

        broker.publish("run-1", {"type": "run_completed", "output": "done"})
        terminal = await asyncio.wait_for(subscription.queue.get(), timeout=1)
        assert terminal["type"] in TERMINAL_EVENT_TYPES
        assert terminal["event_id"] != lifecycle["event_id"]
    finally:
        broker.unsubscribe(subscription)

    assert broker.subscriber_count("run-1") == 0


@pytest.mark.asyncio
# 测试场景：验证该正常业务场景从输入准备到结果断言的完整链路；函数名 test_broker_publish_from_worker_thread_reaches_async_subscriber 精确标识本用例的具体条件。
async def test_broker_publish_from_worker_thread_reaches_async_subscriber() -> None:
    broker = RunStreamBroker()
    _replay, subscription = broker.subscribe("run-thread")
    try:
        await asyncio.to_thread(
            broker.publish,
            "run-thread",
            {"type": "assistant_delta", "delta": "chunk"},
        )
        event = await asyncio.wait_for(subscription.queue.get(), timeout=1)
        assert event["delta"] == "chunk"
        assert event["event_id"]
    finally:
        broker.unsubscribe(subscription)


@pytest.mark.asyncio
async def test_run_stream_sink_emits_one_canonical_assistant_delta() -> None:
    from src.runs.lifecycle import RunCoordinator

    run_id = "canonical-assistant-delta-test"
    replay, subscription = run_stream_broker.subscribe(run_id)
    try:
        sink = RunCoordinator._stream_sink(run_id)
        sink({"type": "assistant_delta", "delta": "首段", "step": 1})
        sink({"type": "assistant_delta", "delta": "后段", "step": 1})
        events = replay + [
            await subscription.queue.get(),
            await subscription.queue.get(),
            await subscription.queue.get(),
        ]
        assert [event["type"] for event in events] == [
            "assistant_message_started",
            "assistant_message_delta",
            "assistant_message_delta",
        ]
        assert [event["delta"] for event in events[1:]] == ["首段", "后段"]
    finally:
        run_stream_broker.unsubscribe(subscription)


def test_turn_terminal_events_close_stream_but_model_response_does_not() -> None:
    from src.api.runtime import _stream_event_is_terminal

    assert _stream_event_is_terminal({"type": "turn_completed"})
    assert _stream_event_is_terminal({"type": "turn_failed"})
    assert _stream_event_is_terminal({"type": "turn_stopped"})
    assert not _stream_event_is_terminal({"type": "model_response_completed"})
