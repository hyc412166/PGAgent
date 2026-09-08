"""RunEvent 的有序写入与运行追踪关联。"""

from __future__ import annotations

from threading import Lock
from typing import Any, Mapping
from weakref import WeakValueDictionary

from sqlalchemy import event, func, select
from sqlalchemy.orm import Session as OrmSession
from sqlalchemy.orm.session import SessionTransaction

from src.observability import current_observability_context
from src.persistence.models import ConversationTurn, DelegatedTask, Run, RunEvent


class _RunLock:
    """包装可弱引用的线程锁，使结束的 Run 不会永久留在注册表。"""

    __slots__ = ("lock", "__weakref__")

    def __init__(self) -> None:
        self.lock = Lock()


_LOCK_REGISTRY_GUARD = Lock()
_RUN_LOCKS: WeakValueDictionary[str, _RunLock] = WeakValueDictionary()
_SESSION_RUN_LOCKS_KEY = "pgagent_run_event_locks"


def _lock_for_run(run_id: str) -> _RunLock:
    with _LOCK_REGISTRY_GUARD:
        run_lock = _RUN_LOCKS.get(run_id)
        if run_lock is None:
            run_lock = _RunLock()
            _RUN_LOCKS[run_id] = run_lock
        return run_lock


def _hold_run_lock_until_transaction_end(db: OrmSession, run_id: str) -> None:
    held_locks: dict[str, _RunLock] = db.info.setdefault(_SESSION_RUN_LOCKS_KEY, {})
    if run_id in held_locks:
        return

    run_lock = _lock_for_run(run_id)
    run_lock.lock.acquire()
    try:
        # 项目当前是单进程 SQLite 应用：同 Run 的锁必须一直持有到调用方
        # commit/rollback，否则下一个会话会在前一个序号未提交时再次读到旧的 max。
        if not db.in_transaction():
            db.begin()
        held_locks[run_id] = run_lock
    except BaseException:
        run_lock.lock.release()
        if not held_locks:
            db.info.pop(_SESSION_RUN_LOCKS_KEY, None)
        raise


@event.listens_for(OrmSession, "after_transaction_end")
def _release_run_locks(
    db: OrmSession,
    transaction: SessionTransaction,
) -> None:
    """在最外层事务结束时释放该 Session 持有的所有 Run 锁。"""

    if transaction.parent is not None:
        return
    held_locks: dict[str, _RunLock] = db.info.pop(_SESSION_RUN_LOCKS_KEY, {})
    for run_lock in reversed(tuple(held_locks.values())):
        run_lock.lock.release()


def append_run_event(
    db: OrmSession,
    *,
    run_id: str,
    event_type: str,
    payload: Mapping[str, Any] | None = None,
    step: int | None = None,
    trace_id: str | None = None,
) -> RunEvent:
    """在调用方事务内追加一条有序事件，但不提交该事务。"""

    _hold_run_lock_until_transaction_end(db, run_id)
    persisted_max = db.scalar(
        select(func.max(RunEvent.sequence)).where(RunEvent.run_id == run_id)
    )
    pending_max = max(
        (
            int(item.sequence)
            for item in db.new
            if isinstance(item, RunEvent) and item.run_id == run_id and item.sequence is not None
        ),
        default=0,
    )
    sequence = max(int(persisted_max or 0), pending_max) + 1

    resolved_trace_id = trace_id
    if resolved_trace_id is None:
        context_trace_id = current_observability_context().get("trace_id")
        resolved_trace_id = context_trace_id if isinstance(context_trace_id, str) else None
    if resolved_trace_id is None:
        run = db.get(Run, run_id)
        turn_id = run.turn_id if run is not None else None
        if turn_id is None:
            delegated = db.scalar(select(DelegatedTask).where(DelegatedTask.child_run_id == run_id))
            parent = db.get(Run, delegated.parent_run_id) if delegated is not None else None
            turn_id = parent.turn_id if parent is not None else None
        turn = db.get(ConversationTurn, turn_id) if turn_id else None
        resolved_trace_id = turn.trace_id if turn is not None else run_id

    item = RunEvent(
        run_id=run_id,
        event_type=event_type,
        payload=dict(payload or {}),
        step=step,
        trace_id=resolved_trace_id,
        sequence=sequence,
    )
    db.add(item)
    return item


__all__ = ["append_run_event"]
