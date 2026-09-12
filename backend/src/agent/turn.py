"""Response ledger and structured Turn transition decisions."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.model.output import LocalToolCallItem, NormalizedModelResponse


class TurnStatus(StrEnum):
    """Internal lifecycle states for one accepted user Turn."""

    ACTING = "acting"
    DRAINING_TOOLS = "draining_tools"
    AWAITING_APPROVAL = "awaiting_approval"
    OBSERVING = "observing"
    COMPLETED = "completed"
    STOPPED = "stopped"
    FAILED = "failed"


class LocalToolStatus(StrEnum):
    """Side-effect lifecycle for one completed local tool-call item."""

    SCHEDULED = "scheduled"
    RUNNING = "running"
    RESULT_COMMITTED = "result_committed"


@dataclass(frozen=True, slots=True)
class TurnDecision:
    """A transition derived only from response and tool lifecycle facts."""

    status: TurnStatus
    follow_up: bool
    reason: str | None
    assistant_text: str
    local_call_count: int
    local_result_count: int
    hosted_tool_count: int


@dataclass(slots=True)
class _LocalToolRecord:
    item: LocalToolCallItem
    status: LocalToolStatus = LocalToolStatus.SCHEDULED
    observed_by_model: bool = False


class TurnLedger:
    """Track provider responses and local side effects for a single Turn.

    Local calls enter this ledger only as completed ``LocalToolCallItem``
    instances.  Taking a call is an atomic scheduling boundary, and committing
    its result is single-use so a replay cannot repeat a side effect.
    """

    def __init__(self) -> None:
        self._responses: list[NormalizedModelResponse] = []
        self._response_ids: set[str] = set()
        self._item_ids: set[tuple[str, str]] = set()
        self._local_calls: dict[str, _LocalToolRecord] = {}
        self._taken_local_call_ids: set[str] = set()
        self._hosted_tool_count = 0
        self._awaiting_approval = False
        self._background_wait = False
        self._stopped = False
        self._failed = False

    @property
    def responses(self) -> tuple[NormalizedModelResponse, ...]:
        return tuple(self._responses)

    @property
    def local_call_count(self) -> int:
        return len(self._local_calls)

    @property
    def local_result_count(self) -> int:
        return sum(
            record.status is LocalToolStatus.RESULT_COMMITTED
            for record in self._local_calls.values()
        )

    @property
    def hosted_tool_count(self) -> int:
        return self._hosted_tool_count

    def accept_response(self, response: NormalizedModelResponse) -> None:
        """Atomically append one normalized response after duplicate checks."""

        # 延迟导入打破 src.agent 与 src.model 门面的初始化环；调用时 agent 已加载完成。
        from src.model.output import HostedToolItem, LocalToolCallItem, NormalizedModelResponse

        if not isinstance(response, NormalizedModelResponse):
            raise TypeError("TurnLedger accepts only NormalizedModelResponse")
        if response.response_id is not None and response.response_id in self._response_ids:
            raise ValueError(f"duplicate response id: {response.response_id}")

        response_scope = response.response_id or f"anonymous-response-{len(self._responses)}"
        pending_item_ids: set[tuple[str, str]] = set()
        pending_call_ids: set[str] = set()
        for item in response.items:
            if item.item_id is not None:
                item_key = (response_scope, item.item_id)
                if item_key in self._item_ids or item_key in pending_item_ids:
                    raise ValueError(f"duplicate item id: {item.item_id}")
                pending_item_ids.add(item_key)
            if isinstance(item, LocalToolCallItem):
                if item.call_id in self._local_calls or item.call_id in pending_call_ids:
                    raise ValueError(f"duplicate local tool call id: {item.call_id}")
                pending_call_ids.add(item.call_id)

        # A newly accepted response proves all earlier committed observations
        # were included in a subsequent provider request.
        for record in self._local_calls.values():
            if record.status is LocalToolStatus.RESULT_COMMITTED:
                record.observed_by_model = True

        self._responses.append(response)
        if response.response_id is not None:
            self._response_ids.add(response.response_id)
        self._item_ids.update(pending_item_ids)
        for item in response.items:
            if isinstance(item, LocalToolCallItem):
                self._local_calls[item.call_id] = _LocalToolRecord(item=item)
            elif isinstance(item, HostedToolItem):
                self._hosted_tool_count += 1

    def take_local_calls(self) -> list[LocalToolCallItem]:
        """Take each completed local call once for the existing safety router."""

        # 未关闭的 provider response 不能释放副作用调用。
        if self.decision().status is not TurnStatus.DRAINING_TOOLS:
            return []
        calls: list[LocalToolCallItem] = []
        for call_id, record in self._local_calls.items():
            if (
                record.status is not LocalToolStatus.SCHEDULED
                or call_id in self._taken_local_call_ids
            ):
                continue
            self._taken_local_call_ids.add(call_id)
            calls.append(record.item)
        return calls

    def local_status(self, call_id: str) -> LocalToolStatus:
        return self._local_record(call_id).status

    def mark_local_running(self, call_id: str) -> None:
        record = self._local_record(call_id)
        if record.status is LocalToolStatus.RESULT_COMMITTED:
            raise ValueError(f"local tool result already committed: {call_id}")
        # 并行调度可能在同一批次重复确认 running，保持该转换幂等。
        record.status = LocalToolStatus.RUNNING

    def commit_local_result(self, call_id: str, *, tool_name: str) -> None:
        record = self._local_record(call_id)
        if record.item.tool_name != tool_name:
            raise ValueError(
                f"tool name mismatch for {call_id}: expected {record.item.tool_name}, got {tool_name}"
            )
        if record.status is LocalToolStatus.RESULT_COMMITTED:
            raise ValueError(f"local tool result already committed: {call_id}")
        if record.status is not LocalToolStatus.RUNNING:
            raise ValueError(f"local tool call was not scheduled: {call_id}")
        record.status = LocalToolStatus.RESULT_COMMITTED

    def set_waiting(self, *, approval: bool = False, background: bool = False) -> None:
        self._awaiting_approval = approval
        self._background_wait = background

    def mark_stopped(self) -> None:
        self._stopped = True

    def mark_failed(self) -> None:
        self._failed = True

    def decision(self) -> TurnDecision:
        """Return the next transition without inspecting assistant wording."""

        assistant_text = self._latest_assistant_text()
        base = {
            "assistant_text": assistant_text,
            "local_call_count": self.local_call_count,
            "local_result_count": self.local_result_count,
            "hosted_tool_count": self.hosted_tool_count,
        }
        if self._failed:
            return TurnDecision(TurnStatus.FAILED, False, "failed", **base)
        if self._stopped:
            return TurnDecision(TurnStatus.STOPPED, False, "stopped", **base)
        if self._awaiting_approval:
            return TurnDecision(TurnStatus.AWAITING_APPROVAL, False, "awaiting_approval", **base)
        if self._background_wait:
            return TurnDecision(TurnStatus.OBSERVING, False, "background_wait", **base)
        if not self._responses:
            return TurnDecision(TurnStatus.ACTING, True, "model_response_required", **base)

        current = self._responses[-1]
        response_status = str(getattr(current.status, "value", current.status))
        if response_status in {"failed", "incomplete"}:
            return TurnDecision(TurnStatus.FAILED, False, "model_response_failed", **base)
        if response_status != "completed":
            return TurnDecision(TurnStatus.FAILED, False, "model_response_incomplete", **base)

        active_calls = [
            record
            for record in self._local_calls.values()
            if record.status is not LocalToolStatus.RESULT_COMMITTED
        ]
        if active_calls:
            return TurnDecision(TurnStatus.DRAINING_TOOLS, False, "local_tool_drain", **base)
        if any(
            record.status is LocalToolStatus.RESULT_COMMITTED and not record.observed_by_model
            for record in self._local_calls.values()
        ):
            return TurnDecision(TurnStatus.OBSERVING, True, "local_tool_results", **base)

        latest_assistant = next(
            (item for item in reversed(current.items) if item.item_type == "assistant_message"),
            None,
        )
        latest_end_turn = getattr(latest_assistant, "end_turn", None)
        if str(getattr(latest_end_turn, "value", latest_end_turn)) == "false":
            return TurnDecision(
                TurnStatus.OBSERVING,
                True,
                "provider_requested_follow_up",
                **base,
            )
        if not assistant_text.strip():
            return TurnDecision(TurnStatus.FAILED, False, "empty_model_output", **base)
        return TurnDecision(TurnStatus.COMPLETED, False, None, **base)

    def _local_record(self, call_id: str) -> _LocalToolRecord:
        try:
            return self._local_calls[call_id]
        except KeyError as exc:
            raise ValueError(f"unknown local tool call id: {call_id}") from exc

    def _latest_assistant_text(self) -> str:
        if not self._responses:
            return ""
        return "".join(
            item.content
            for item in self._responses[-1].items
            if item.item_type == "assistant_message"
        )


__all__ = [
    "LocalToolStatus",
    "TurnDecision",
    "TurnLedger",
    "TurnStatus",
]
