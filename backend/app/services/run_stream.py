"""Transient, in-memory event delivery for local run streams.

The durable runtime audit trail remains in ``RunEvent``.  This broker is only
for low-latency UI delivery, including token deltas that must not be written to
SQLite one row at a time.
"""

from __future__ import annotations

import asyncio
import threading
from collections import OrderedDict, deque
from dataclasses import dataclass
from datetime import datetime, timezone
from itertools import count
from typing import Any
from uuid import uuid4


TERMINAL_EVENT_TYPES = frozenset({"run_completed", "run_interrupted", "run_stopped", "model_failed", "integration_failed"})


@dataclass(frozen=True, slots=True)
class RunStreamSubscription:
    run_id: str
    queue: asyncio.Queue[dict[str, Any]]
    loop: asyncio.AbstractEventLoop


class RunStreamBroker:
    """Thread-safe replay buffer and fan-out for asyncio SSE consumers."""

    def __init__(self, *, replay_size: int = 256, queue_size: int = 512, max_runs: int = 128) -> None:
        if replay_size < 1 or queue_size < 1 or max_runs < 1:
            raise ValueError("broker limits must be positive")
        self._replay_size = replay_size
        self._queue_size = queue_size
        self._max_runs = max_runs
        self._lock = threading.RLock()
        self._buffers: OrderedDict[str, deque[dict[str, Any]]] = OrderedDict()
        self._subscribers: dict[str, set[RunStreamSubscription]] = {}
        self._event_ids = count(1)
        self._instance_id = uuid4().hex

    def publish(self, run_id: str, event: dict[str, Any]) -> dict[str, Any]:
        """Append and fan out an event; safe from worker and event-loop threads."""

        with self._lock:
            envelope = {
                **event,
                "run_id": run_id,
                "event_id": f"{self._instance_id}-{next(self._event_ids)}",
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
            buffer = self._buffers.get(run_id)
            if buffer is None:
                buffer = deque(maxlen=self._replay_size)
                self._buffers[run_id] = buffer
            buffer.append(envelope)
            self._buffers.move_to_end(run_id)
            self._evict_inactive_runs_locked(protected_run_id=run_id)
            subscribers = tuple(self._subscribers.get(run_id, ()))
            for subscription in subscribers:
                try:
                    subscription.loop.call_soon_threadsafe(self._offer, subscription.queue, envelope)
                except RuntimeError:
                    self._subscribers.get(run_id, set()).discard(subscription)
        return dict(envelope)

    def subscribe(self, run_id: str) -> tuple[list[dict[str, Any]], RunStreamSubscription]:
        """Atomically capture replay and register the live subscriber."""

        subscription = RunStreamSubscription(
            run_id=run_id,
            queue=asyncio.Queue(maxsize=self._queue_size),
            loop=asyncio.get_running_loop(),
        )
        with self._lock:
            replay = [dict(item) for item in self._buffers.get(run_id, ())]
            self._subscribers.setdefault(run_id, set()).add(subscription)
            if run_id in self._buffers:
                self._buffers.move_to_end(run_id)
        return replay, subscription

    def unsubscribe(self, subscription: RunStreamSubscription) -> None:
        with self._lock:
            subscribers = self._subscribers.get(subscription.run_id)
            if subscribers is None:
                return
            subscribers.discard(subscription)
            if not subscribers:
                self._subscribers.pop(subscription.run_id, None)
                self._evict_inactive_runs_locked()

    def subscriber_count(self, run_id: str) -> int:
        """Expose cleanup state for diagnostics and tests."""

        with self._lock:
            return len(self._subscribers.get(run_id, ()))

    @staticmethod
    def _offer(queue: asyncio.Queue[dict[str, Any]], event: dict[str, Any]) -> None:
        if queue.full():
            try:
                queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
        try:
            queue.put_nowait(dict(event))
        except asyncio.QueueFull:
            # Another callback may have filled the bounded queue. The replay
            # buffer still protects reconnects, and the runtime must never block.
            pass

    def _evict_inactive_runs_locked(self, *, protected_run_id: str | None = None) -> None:
        if len(self._buffers) <= self._max_runs:
            return
        for run_id in tuple(self._buffers):
            if len(self._buffers) <= self._max_runs:
                break
            if run_id != protected_run_id and not self._subscribers.get(run_id):
                self._buffers.pop(run_id, None)


run_stream_broker = RunStreamBroker()
