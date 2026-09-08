"""Transient, in-memory event delivery for local run streams.

The durable runtime audit trail remains in ``RunEvent``.  This broker is only
for low-latency UI delivery, including token deltas that must not be written to
SQLite one row at a time.
"""
# 文件职责：负责运行创建、恢复、流式传输和生命周期中的 stream 子模块。
# 逻辑关系：上层通过 runs/stream.py 使用本模块；本模块把处理结果交给同领域服务、持久化层或 API 响应层。

from __future__ import annotations

import asyncio
import threading
from collections import OrderedDict, deque
from dataclasses import dataclass
from datetime import datetime, timezone
from itertools import count
from typing import Any
from uuid import uuid4


# 变量说明：TERMINAL_EVENT_TYPES 表示当前流程使用的 TERMINAL_EVENT_TYPES 集合。
TERMINAL_EVENT_TYPES = frozenset({"run_completed", "run_interrupted", "run_stopped", "model_failed", "integration_failed"})


# 类职责：定义 RunStreamSubscription 在本领域中的数据与行为。
@dataclass(frozen=True, slots=True)
class RunStreamSubscription:
    # 变量说明：run_id 表示当前运行标识。
    run_id: str
    # 变量说明：queue 表示当前步骤使用的 queue 值。
    queue: asyncio.Queue[dict[str, Any]]
    # 变量说明：loop 表示当前步骤使用的 loop 值。
    loop: asyncio.AbstractEventLoop


# 类职责：定义 RunStreamBroker 在本领域中的数据与行为。
class RunStreamBroker:
    """Thread-safe replay buffer and fan-out for asyncio SSE consumers."""

    # 函数职责：初始化实例依赖与初始状态。
    # 参数关系：replay_size 表示当前步骤使用的 replay_size 值；queue_size 表示当前步骤使用的 queue_size 值；max_runs 表示当前流程使用的 max_runs 集合。
    # 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
    def __init__(self, *, replay_size: int = 256, queue_size: int = 512, max_runs: int = 128) -> None:
        if replay_size < 1 or queue_size < 1 or max_runs < 1:
            raise ValueError("broker limits must be positive")
        # 变量说明：_replay_size 表示当前步骤使用的 _replay_size 值。
        self._replay_size = replay_size
        # 变量说明：_queue_size 表示当前步骤使用的 _queue_size 值。
        self._queue_size = queue_size
        # 变量说明：_max_runs 表示当前流程使用的 _max_runs 集合。
        self._max_runs = max_runs
        # 变量说明：_lock 表示当前步骤使用的 _lock 值。
        self._lock = threading.RLock()
        # 变量说明：_buffers 表示当前流程使用的 _buffers 集合。
        self._buffers: OrderedDict[str, deque[dict[str, Any]]] = OrderedDict()
        # 变量说明：_subscribers 表示当前流程使用的 _subscribers 集合。
        self._subscribers: dict[str, set[RunStreamSubscription]] = {}
        # 变量说明：_event_ids 表示_event 对象标识集合。
        self._event_ids = count(1)
        # 变量说明：_instance_id 表示_instance 对象的唯一标识。
        self._instance_id = uuid4().hex

    # 函数职责：完成 publish 对应的业务处理。
    # 参数关系：run_id 表示当前运行标识；event 表示当前运行事件。
    # 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
    def publish(self, run_id: str, event: dict[str, Any]) -> dict[str, Any]:
        """Append and fan out an event; safe from worker and event-loop threads."""

        with self._lock:
            # 变量说明：envelope 表示当前步骤使用的 envelope 值。
            envelope = {
                **event,
                "run_id": run_id,
                "event_id": f"{self._instance_id}-{next(self._event_ids)}",
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
            # 变量说明：buffer 表示当前步骤使用的 buffer 值。
            buffer = self._buffers.get(run_id)
            if buffer is None:
                # 变量说明：buffer 表示当前步骤使用的 buffer 值。
                buffer = deque(maxlen=self._replay_size)
                self._buffers[run_id] = buffer
            buffer.append(envelope)
            self._buffers.move_to_end(run_id)
            self._evict_inactive_runs_locked(protected_run_id=run_id)
            # 变量说明：subscribers 表示当前流程使用的 subscribers 集合。
            subscribers = tuple(self._subscribers.get(run_id, ()))
            for subscription in subscribers:
                try:
                    subscription.loop.call_soon_threadsafe(self._offer, subscription.queue, envelope)
                except RuntimeError:
                    self._subscribers.get(run_id, set()).discard(subscription)
        return dict(envelope)

    # 函数职责：完成 subscribe 对应的业务处理。
    # 参数关系：run_id 表示当前运行标识。
    # 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
    def subscribe(self, run_id: str) -> tuple[list[dict[str, Any]], RunStreamSubscription]:
        """Atomically capture replay and register the live subscriber."""

        # 变量说明：subscription 表示当前步骤使用的 subscription 值。
        subscription = RunStreamSubscription(
            run_id=run_id,
            queue=asyncio.Queue(maxsize=self._queue_size),
            loop=asyncio.get_running_loop(),
        )
        with self._lock:
            # 变量说明：replay 表示当前步骤使用的 replay 值。
            replay = [dict(item) for item in self._buffers.get(run_id, ())]
            self._subscribers.setdefault(run_id, set()).add(subscription)
            if run_id in self._buffers:
                self._buffers.move_to_end(run_id)
        return replay, subscription

    # 函数职责：完成 unsubscribe 对应的业务处理。
    # 参数关系：subscription 表示当前步骤使用的 subscription 值。
    # 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
    def unsubscribe(self, subscription: RunStreamSubscription) -> None:
        with self._lock:
            # 变量说明：subscribers 表示当前流程使用的 subscribers 集合。
            subscribers = self._subscribers.get(subscription.run_id)
            if subscribers is None:
                return
            subscribers.discard(subscription)
            if not subscribers:
                self._subscribers.pop(subscription.run_id, None)
                self._evict_inactive_runs_locked()

    # 函数职责：完成 subscriber_count 对应的业务处理。
    # 参数关系：run_id 表示当前运行标识。
    # 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
    def subscriber_count(self, run_id: str) -> int:
        """Expose cleanup state for diagnostics and tests."""

        with self._lock:
            return len(self._subscribers.get(run_id, ()))

    # 函数职责：完成 offer 对应的业务处理。
    # 参数关系：queue 表示当前步骤使用的 queue 值；event 表示当前运行事件。
    # 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
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

    # 函数职责：完成 evict_inactive_runs_locked 对应的业务处理。
    # 参数关系：protected_run_id 表示protected_run 对象的唯一标识。
    # 返回关系：结果返回给调用层，并由调用层继续持久化、发送事件或推进运行状态。
    def _evict_inactive_runs_locked(self, *, protected_run_id: str | None = None) -> None:
        if len(self._buffers) <= self._max_runs:
            return
        for run_id in tuple(self._buffers):
            if len(self._buffers) <= self._max_runs:
                break
            if run_id != protected_run_id and not self._subscribers.get(run_id):
                self._buffers.pop(run_id, None)


# 变量说明：run_stream_broker 表示当前步骤使用的 run_stream_broker 值。
run_stream_broker = RunStreamBroker()
