"""Internal normalized output items shared by provider adapters and Turn logic."""

# 文件职责：定义 Responses 与 Chat Completions 共用的模型输出数据结构。
# 逻辑关系：协议适配器产出本模块的 item，Turn 账本按 response 记录并推进工具与终态边界。

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any, ClassVar, Mapping


class OutputPhase(StrEnum):
    """Provider may label assistant text without making a Turn terminal."""

    COMMENTARY = "commentary"
    FINAL_ANSWER = "final_answer"
    UNKNOWN = "unknown"


class ResponseStatus(StrEnum):
    """Lifecycle status of one provider response, not of the user Turn."""

    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"
    INCOMPLETE = "incomplete"
    UNKNOWN = "unknown"


class EndTurn(StrEnum):
    """Explicit provider end-turn hint; unknown is distinct from a false hint."""

    TRUE = "true"
    FALSE = "false"
    UNKNOWN = "unknown"


# end_turn 是 provider 的提示值；缺少该元数据时必须保留 unknown，不能从正文或工具调用猜测。
EndTurnValue = bool | EndTurn


def _serializable(value: Any) -> Any:
    """Convert nested enums and dataclass values to ordinary JSON values."""

    if isinstance(value, StrEnum):
        return value.value
    if isinstance(value, Mapping):
        return {key: _serializable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_serializable(item) for item in value]
    if isinstance(value, set):
        return [_serializable(item) for item in value]
    return value


@dataclass(slots=True)
class OutputItem:
    """Common response identity used to order and correlate normalized items."""

    response_id: str | None = None
    item_id: str | None = None
    output_index: int | None = None
    item_type: ClassVar[str] = "output_item"

    def __post_init__(self) -> None:
        for field_name in ("response_id", "item_id"):
            value = getattr(self, field_name)
            if value is not None and (not isinstance(value, str) or not value.strip()):
                raise ValueError(f"{field_name} must be a non-empty string when provided")
        if self.output_index is not None:
            if isinstance(self.output_index, bool) or not isinstance(self.output_index, int):
                raise TypeError("output_index must be an integer when provided")
            if self.output_index < 0:
                raise ValueError("output_index must be non-negative")

    def to_dict(self) -> dict[str, Any]:
        payload = _serializable(asdict(self))
        return {"type": self.item_type, **payload}

    # 现有后端值对象统一使用 to_dict；as_dict/to_payload 仅提供同一内部数据的便捷别名。
    def as_dict(self) -> dict[str, Any]:
        return self.to_dict()

    def to_payload(self) -> dict[str, Any]:
        return self.to_dict()


@dataclass(slots=True)
class AssistantMessageItem(OutputItem):
    """Visible assistant text kept in one stable message item while streaming."""

    content: str = ""
    phase: OutputPhase | str = OutputPhase.UNKNOWN
    end_turn: EndTurnValue | None = EndTurn.UNKNOWN
    item_type: ClassVar[str] = "assistant_message"

    def __post_init__(self) -> None:
        OutputItem.__post_init__(self)
        if self.content is None:
            self.content = ""
        if not isinstance(self.content, str):
            raise TypeError("assistant message content must be a string")
        try:
            self.phase = OutputPhase(self.phase)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"unsupported assistant message phase: {self.phase!r}") from exc
        if self.end_turn is None:
            self.end_turn = EndTurn.UNKNOWN
        elif isinstance(self.end_turn, bool):
            pass
        elif self.end_turn in {EndTurn.TRUE, EndTurn.FALSE}:
            self.end_turn = self.end_turn == EndTurn.TRUE
        elif self.end_turn == EndTurn.UNKNOWN:
            self.end_turn = EndTurn.UNKNOWN
        else:
            raise ValueError(f"unsupported end_turn value: {self.end_turn!r}")


@dataclass(slots=True)
class ReasoningItem(OutputItem):
    """Non-visible reasoning metadata retained only for continuation or safe summaries."""

    summary: Any = None
    encrypted_content: str | None = None
    provider_data: dict[str, Any] = field(default_factory=dict)
    item_type: ClassVar[str] = "reasoning"

    def __post_init__(self) -> None:
        OutputItem.__post_init__(self)
        if not isinstance(self.provider_data, dict):
            self.provider_data = dict(self.provider_data)


@dataclass(slots=True)
class LocalToolCallItem(OutputItem):
    """A completed local function call eligible for the existing ToolRouter path."""

    call_id: str = ""
    tool_name: str = ""
    arguments: Any = field(default_factory=dict)
    item_type: ClassVar[str] = "local_tool_call"

    def __post_init__(self) -> None:
        OutputItem.__post_init__(self)
        if not isinstance(self.call_id, str) or not self.call_id.strip():
            raise ValueError("call id must be a non-empty string")
        if not isinstance(self.tool_name, str) or not self.tool_name.strip():
            raise ValueError("tool name must be a non-empty string")


@dataclass(slots=True)
class HostedToolItem(OutputItem):
    """Provider-hosted tool activity, which never enters the local ToolRouter."""

    tool_name: str = ""
    status: str = "completed"
    call_id: str | None = None
    details: dict[str, Any] = field(default_factory=dict)
    item_type: ClassVar[str] = "hosted_tool"

    def __post_init__(self) -> None:
        OutputItem.__post_init__(self)
        if not isinstance(self.tool_name, str) or not self.tool_name.strip():
            raise ValueError("hosted tool name must be a non-empty string")
        if not isinstance(self.status, str) or not self.status.strip():
            raise ValueError("hosted tool status must be a non-empty string")
        if self.call_id is not None and (not isinstance(self.call_id, str) or not self.call_id.strip()):
            raise ValueError("call id must be a non-empty string when provided")
        if not isinstance(self.details, dict):
            self.details = dict(self.details)


NormalizedOutputItem = (
    AssistantMessageItem | ReasoningItem | LocalToolCallItem | HostedToolItem
)


@dataclass(slots=True)
class NormalizedModelResponse:
    """Ordered provider response accepted by the response ledger."""

    response_id: str | None = None
    items: list[NormalizedOutputItem] = field(default_factory=list)
    status: ResponseStatus | str = ResponseStatus.UNKNOWN
    provider_payload: Any = None
    provider_reference: str | None = None
    usage: dict[str, Any] = field(default_factory=dict)
    finish_reason: str | None = None
    error: Any = None

    def __post_init__(self) -> None:
        if self.response_id is not None and (
            not isinstance(self.response_id, str) or not self.response_id.strip()
        ):
            raise ValueError("response_id must be a non-empty string when provided")
        try:
            self.status = ResponseStatus(self.status)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"unsupported provider response status: {self.status!r}") from exc
        if not isinstance(self.items, list):
            self.items = list(self.items)
        if not isinstance(self.usage, dict):
            self.usage = dict(self.usage)

        seen_item_ids: set[str] = set()
        seen_call_ids: set[str] = set()
        seen_output_indexes: set[int] = set()
        next_index = 0
        for item in self.items:
            if not isinstance(item, (AssistantMessageItem, ReasoningItem, LocalToolCallItem, HostedToolItem)):
                raise TypeError("normalized response items must be normalized output item objects")
            if item.item_id is not None:
                if item.item_id in seen_item_ids:
                    raise ValueError(f"duplicate item id: {item.item_id}")
                seen_item_ids.add(item.item_id)
            call_id = getattr(item, "call_id", None)
            if call_id is not None:
                if call_id in seen_call_ids:
                    raise ValueError(f"duplicate call id: {call_id}")
                seen_call_ids.add(call_id)
            if item.output_index is None:
                while next_index in seen_output_indexes:
                    next_index += 1
                item.output_index = next_index
            elif item.output_index in seen_output_indexes:
                raise ValueError(f"duplicate output_index: {item.output_index}")
            seen_output_indexes.add(item.output_index)
            next_index = max(next_index, item.output_index + 1)
        self.items.sort(key=lambda item: item.output_index if item.output_index is not None else -1)

    @property
    def is_completed(self) -> bool:
        """Whether the provider response closed successfully."""

        return self.status is ResponseStatus.COMPLETED

    @property
    def completed(self) -> bool:
        return self.is_completed

    @property
    def is_failed(self) -> bool:
        return self.status in {ResponseStatus.FAILED, ResponseStatus.INCOMPLETE}

    def to_dict(self) -> dict[str, Any]:
        return _serializable({
            "response_id": self.response_id,
            "items": [item.to_dict() for item in self.items],
            "status": self.status,
            "provider_payload": self.provider_payload,
            "provider_reference": self.provider_reference,
            "usage": self.usage,
            "finish_reason": self.finish_reason,
            "error": self.error,
        })

    def as_dict(self) -> dict[str, Any]:
        return self.to_dict()

    def to_payload(self) -> dict[str, Any]:
        return self.to_dict()


# 常用别名帮助协议适配层表达“模型 response”，但不引入新的外部 contract。
ModelResponseStatus = ResponseStatus
NormalizedResponse = NormalizedModelResponse


__all__ = [
    "AssistantMessageItem",
    "EndTurn",
    "EndTurnValue",
    "HostedToolItem",
    "LocalToolCallItem",
    "ModelResponseStatus",
    "NormalizedModelResponse",
    "NormalizedOutputItem",
    "NormalizedResponse",
    "OutputItem",
    "OutputPhase",
    "ReasoningItem",
    "ResponseStatus",
]
