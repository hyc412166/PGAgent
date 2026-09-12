"""验证模型输出项的统一结构、顺序与 provider response 边界。"""

from __future__ import annotations

import pytest

from src.model import (
    AssistantMessageItem,
    EndTurn,
    HostedToolItem,
    LocalToolCallItem,
    NormalizedModelResponse,
    OutputPhase,
    ReasoningItem,
    ResponseStatus,
    normalize_end_turn,
)


def test_normalized_response_keeps_four_item_kinds_ordered_and_serializable() -> None:
    assistant = AssistantMessageItem(
        response_id="resp-1",
        item_id="msg-1",
        output_index=0,
        content="准备执行。",
        phase=OutputPhase.COMMENTARY,
        end_turn=False,
    )
    reasoning = ReasoningItem(
        response_id="resp-1",
        item_id="reasoning-1",
        output_index=1,
        summary="检查工具参数",
    )
    local_call = LocalToolCallItem(
        response_id="resp-1",
        item_id="function-1",
        output_index=2,
        call_id="call-1",
        tool_name="read_file",
        arguments={"path": "README.md"},
    )
    hosted = HostedToolItem(
        response_id="resp-1",
        item_id="hosted-1",
        output_index=3,
        tool_name="web_search",
        status="completed",
    )

    response = NormalizedModelResponse(
        response_id="resp-1",
        items=[hosted, local_call, reasoning, assistant],
        status=ResponseStatus.COMPLETED,
        provider_payload={"protocol": "responses"},
        usage={"input_tokens": 10, "output_tokens": 4},
        finish_reason="stop",
    )

    assert response.items == [assistant, reasoning, local_call, hosted]
    assert response.status == ResponseStatus.COMPLETED
    assert response.is_completed is True
    assert response.to_dict() == {
        "response_id": "resp-1",
        "items": [
            {
                "type": "assistant_message",
                "response_id": "resp-1",
                "item_id": "msg-1",
                "output_index": 0,
                "content": "准备执行。",
                "phase": "commentary",
                "end_turn": False,
            },
            {
                "type": "reasoning",
                "response_id": "resp-1",
                "item_id": "reasoning-1",
                "output_index": 1,
                "summary": "检查工具参数",
                "encrypted_content": None,
                "provider_data": {},
            },
            {
                "type": "local_tool_call",
                "response_id": "resp-1",
                "item_id": "function-1",
                "output_index": 2,
                "call_id": "call-1",
                "tool_name": "read_file",
                "arguments": {"path": "README.md"},
            },
            {
                "type": "hosted_tool",
                "response_id": "resp-1",
                "item_id": "hosted-1",
                "output_index": 3,
                "tool_name": "web_search",
                "status": "completed",
                "call_id": None,
                "details": {},
            },
        ],
        "status": "completed",
        "provider_payload": {"protocol": "responses"},
        "provider_reference": None,
        "usage": {"input_tokens": 10, "output_tokens": 4},
        "finish_reason": "stop",
        "error": None,
    }


def test_missing_phase_and_end_turn_are_unknown_without_inference() -> None:
    item = AssistantMessageItem(
        response_id="chat-1",
        item_id="message-0",
        output_index=0,
        content="模型调用了工具，但 phase 仍未知。",
    )

    assert item.phase == OutputPhase.UNKNOWN
    assert item.end_turn == "unknown"
    assert item.to_dict()["phase"] == "unknown"
    assert item.to_dict()["end_turn"] == "unknown"


@pytest.mark.parametrize(
    ("raw_value", "expected"),
    [
        (True, EndTurn.TRUE),
        (False, EndTurn.FALSE),
        ("true", EndTurn.TRUE),
        ("false", EndTurn.FALSE),
        (None, EndTurn.UNKNOWN),
    ],
)
def test_end_turn_uses_one_enum_representation_and_unknown_is_not_truthy(
    raw_value: object,
    expected: EndTurn,
) -> None:
    item = AssistantMessageItem(
        response_id="resp-1",
        item_id="message-0",
        output_index=0,
        end_turn=raw_value,  # type: ignore[arg-type]
    )

    assert item.end_turn is expected
    assert bool(item.end_turn) is (expected is EndTurn.TRUE)
    assert normalize_end_turn(raw_value) is expected


def test_duplicate_item_id_is_rejected_before_response_is_accepted() -> None:
    first = ReasoningItem(response_id="resp-1", item_id="same", output_index=0)
    second = AssistantMessageItem(
        response_id="resp-1", item_id="same", output_index=1, content="重复。"
    )

    with pytest.raises(ValueError, match="item id"):
        NormalizedModelResponse(response_id="resp-1", items=[first, second])


def test_duplicate_local_call_id_is_rejected_before_dispatch() -> None:
    first = LocalToolCallItem(
        response_id="resp-1",
        item_id="function-1",
        output_index=0,
        call_id="same-call",
        tool_name="read_file",
    )
    second = LocalToolCallItem(
        response_id="resp-1",
        item_id="function-2",
        output_index=1,
        call_id="same-call",
        tool_name="write_file",
    )

    with pytest.raises(ValueError, match="call id"):
        NormalizedModelResponse(response_id="resp-1", items=[first, second])


@pytest.mark.parametrize(
    ("status", "is_completed"),
    [
        (ResponseStatus.IN_PROGRESS, False),
        (ResponseStatus.COMPLETED, True),
        (ResponseStatus.FAILED, False),
        (ResponseStatus.INCOMPLETE, False),
    ],
)
def test_response_status_only_marks_provider_response_completion(
    status: ResponseStatus,
    is_completed: bool,
) -> None:
    response = NormalizedModelResponse(
        response_id="resp-1",
        items=[],
        status=status,
    )

    assert response.status == status
    assert response.is_completed is is_completed
