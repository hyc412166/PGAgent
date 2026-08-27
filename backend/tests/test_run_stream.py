from __future__ import annotations

import asyncio

import pytest

from src.runs.stream import RunStreamBroker, TERMINAL_EVENT_TYPES


@pytest.mark.asyncio
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
