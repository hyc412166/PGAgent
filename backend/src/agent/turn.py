"""Response ledger and structured Turn transition decisions."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import json
from typing import TYPE_CHECKING, Any, Mapping

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

    def mark_local_awaiting_approval(self, call_id: str) -> None:
        """Record that policy stopped the call before its side effect executed."""

        record = self._local_record(call_id)
        if record.status is LocalToolStatus.RESULT_COMMITTED:
            raise ValueError(f"local tool result already committed: {call_id}")
        record.status = LocalToolStatus.SCHEDULED
        self._awaiting_approval = True

    def validate_approval_resume(
        self,
        *,
        call_id: str,
        tool_name: str,
        arguments: Mapping[str, Any],
    ) -> None:
        """Reject a changed or ambiguous approval before dispatching its tool."""

        if not self._awaiting_approval:
            raise ValueError("approval ledger is not awaiting approval")
        self.validate_scheduled_call(
            call_id=call_id,
            tool_name=tool_name,
            arguments=arguments,
        )

    def validate_scheduled_call(
        self,
        *,
        call_id: str,
        tool_name: str,
        arguments: Mapping[str, Any],
    ) -> None:
        """Validate one persisted call before any resumed dispatch."""

        record = self._local_record(call_id)
        if record.status is not LocalToolStatus.SCHEDULED:
            raise ValueError(f"approval ledger call is ambiguous: {call_id}={record.status.value}")
        expected_arguments = json.dumps(record.item.arguments, sort_keys=True, separators=(",", ":"))
        actual_arguments = json.dumps(dict(arguments), sort_keys=True, separators=(",", ":"))
        if record.item.tool_name != tool_name or expected_arguments != actual_arguments:
            raise ValueError("approval request does not match output ledger")

    def has_running_calls(self) -> bool:
        return any(record.status is LocalToolStatus.RUNNING for record in self._local_calls.values())

    def clear_background_wait(self) -> None:
        self._background_wait = False

    def mark_stopped(self) -> None:
        self._stopped = True

    def mark_failed(self) -> None:
        self._failed = True

    def to_snapshot(self) -> dict[str, Any]:
        """Project durable response/tool facts without provider-private data."""

        return {
            "responses": [response.to_dict() for response in self._responses],
            "local_calls": [
                {
                    "call_id": call_id,
                    "tool_name": record.item.tool_name,
                    "arguments": record.item.arguments,
                    "status": record.status.value,
                    "observed_by_model": record.observed_by_model,
                }
                for call_id, record in self._local_calls.items()
            ],
            "taken_local_call_ids": sorted(self._taken_local_call_ids),
            "hosted_tool_count": self._hosted_tool_count,
            "awaiting_approval": self._awaiting_approval,
            "background_wait": self._background_wait,
            "stopped": self._stopped,
            "failed": self._failed,
        }

    @classmethod
    def from_snapshot(cls, payload: Mapping[str, Any] | None) -> "TurnLedger":
        """Restore an exact ledger or reject a contradictory current snapshot."""

        if not isinstance(payload, Mapping):
            raise ValueError("ledger snapshot must be an object")
        ledger = cls()
        # Responses are validated through the same constructor used on the
        # live path, so corrupt snapshots fail closed instead of executing a
        # potentially ambiguous side-effect call.
        try:
            from src.model.output import (
                AssistantMessageItem,
                HostedToolItem,
                LocalToolCallItem,
                NormalizedModelResponse,
                ReasoningItem,
            )

            item_types = {
                "assistant_message": AssistantMessageItem,
                "reasoning": ReasoningItem,
                "local_tool_call": LocalToolCallItem,
                "hosted_tool": HostedToolItem,
            }
            raw_responses = payload.get("responses")
            raw_calls = payload.get("local_calls")
            raw_taken = payload.get("taken_local_call_ids")
            if not isinstance(raw_responses, list) or not isinstance(raw_calls, list) or not isinstance(raw_taken, list):
                raise ValueError("ledger snapshot collections must be lists")
            for raw_response in raw_responses:
                if not isinstance(raw_response, Mapping):
                    raise ValueError("ledger snapshot response must be an object")
                raw_items = raw_response.get("items")
                if not isinstance(raw_items, list):
                    raise ValueError("ledger snapshot response items must be a list")
                items = []
                for raw_item in raw_items:
                    if not isinstance(raw_item, Mapping):
                        raise ValueError("ledger snapshot item must be an object")
                    item_type = str(raw_item.get("type") or raw_item.get("item_type") or "")
                    item_cls = item_types.get(item_type)
                    if item_cls is None:
                        raise ValueError(f"ledger snapshot item type is unsupported: {item_type}")
                    values = dict(raw_item)
                    values.pop("type", None)
                    values.pop("item_type", None)
                    items.append(item_cls(**values))
                ledger.accept_response(NormalizedModelResponse(
                    response_id=raw_response.get("response_id"),
                    items=items,
                    status=raw_response.get("status", "unknown"),
                    provider_payload=raw_response.get("provider_payload"),
                    provider_reference=raw_response.get("provider_reference"),
                    usage=dict(raw_response.get("usage") or {}),
                    finish_reason=raw_response.get("finish_reason"),
                ))
        except (TypeError, ValueError, KeyError) as exc:
            raise ValueError(f"invalid ledger snapshot: {exc}") from exc

        declared_call_ids = set(ledger._local_calls)
        persisted_call_ids: set[str] = set()
        for raw_call in raw_calls:
            if not isinstance(raw_call, Mapping):
                raise ValueError("invalid ledger snapshot local call")
            call_id = raw_call.get("call_id")
            if not isinstance(call_id, str):
                raise ValueError("invalid ledger snapshot local call id")
            if not call_id or call_id in persisted_call_ids:
                raise ValueError("invalid ledger snapshot local call id")
            persisted_call_ids.add(call_id)
            try:
                raw_status = raw_call.get("status")
                if not isinstance(raw_status, str):
                    raise ValueError("ledger snapshot local call status must be a string")
                status = LocalToolStatus(raw_status)
                record = ledger._local_record(call_id)
                expected_arguments = json.dumps(record.item.arguments, sort_keys=True, separators=(",", ":"))
                persisted_arguments = json.dumps(raw_call.get("arguments"), sort_keys=True, separators=(",", ":"))
            except (TypeError, ValueError) as exc:
                raise ValueError(f"invalid ledger snapshot local call: {call_id}") from exc
            if record.item.tool_name != str(raw_call.get("tool_name") or "") or expected_arguments != persisted_arguments:
                raise ValueError(f"ledger snapshot call identity mismatch: {call_id}")
            raw_observed = raw_call.get("observed_by_model")
            if not isinstance(raw_observed, bool):
                raise ValueError("ledger snapshot observed_by_model must be boolean")
            if raw_observed and status is not LocalToolStatus.RESULT_COMMITTED:
                raise ValueError("ledger snapshot cannot observe an uncommitted local call")
            record.status = status
            record.observed_by_model = raw_observed
        if persisted_call_ids != declared_call_ids:
            raise ValueError("ledger snapshot call set does not match responses")
        if any(not isinstance(call_id, str) or not call_id for call_id in raw_taken):
            raise ValueError("ledger snapshot taken call ids must be non-empty strings")
        ledger._taken_local_call_ids = set(raw_taken)
        if len(ledger._taken_local_call_ids) != len(raw_taken):
            raise ValueError("ledger snapshot taken call ids must be unique")
        if not ledger._taken_local_call_ids.issubset(declared_call_ids):
            raise ValueError("ledger snapshot taken call is unknown")
        if any(
            record.status is not LocalToolStatus.SCHEDULED and call_id not in ledger._taken_local_call_ids
            for call_id, record in ledger._local_calls.items()
        ):
            raise ValueError("ledger snapshot active call was never taken")
        persisted_hosted_count = payload.get("hosted_tool_count")
        if (
            isinstance(persisted_hosted_count, bool)
            or not isinstance(persisted_hosted_count, int)
            or persisted_hosted_count < 0
        ):
            raise ValueError("ledger snapshot hosted tool count must be a non-negative integer")
        if persisted_hosted_count != ledger._hosted_tool_count:
            raise ValueError("ledger snapshot hosted tool count mismatch")
        flag_names = ("awaiting_approval", "background_wait", "stopped", "failed")
        flags: dict[str, bool] = {}
        for flag_name in flag_names:
            raw_flag = payload.get(flag_name)
            if not isinstance(raw_flag, bool):
                raise ValueError(f"ledger snapshot {flag_name} must be boolean")
            flags[flag_name] = raw_flag
        if flags["awaiting_approval"] and flags["background_wait"]:
            raise ValueError("ledger snapshot cannot await approval and background together")
        if flags["stopped"] and flags["failed"]:
            raise ValueError("ledger snapshot cannot be stopped and failed together")
        if (flags["stopped"] or flags["failed"]) and (
            flags["awaiting_approval"] or flags["background_wait"]
        ):
            raise ValueError("ledger snapshot terminal state cannot retain waiting flags")
        ledger._awaiting_approval = flags["awaiting_approval"]
        ledger._background_wait = flags["background_wait"]
        ledger._stopped = flags["stopped"]
        ledger._failed = flags["failed"]
        return ledger

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
