from __future__ import annotations

from typing import Any, AsyncIterator

import pytest

from src.model.output import (
    AssistantMessageItem,
    EndTurn,
    HostedToolItem,
    LocalToolCallItem,
    NormalizedModelResponse,
    OutputPhase,
    ResponseStatus,
)
from src.model.protocols import chat_completions, responses
from src.model.config import ProviderConfig
from src.model.streaming import StreamInterrupted


def _config() -> ProviderConfig:
    return ProviderConfig(
        provider="openai_compatible",
        base_url="https://provider.test/v1",
        secret_ref="credential:test",
        model_id="gpt-test",
        api_protocol="chat_completions",
    )


@pytest.mark.asyncio
async def test_responses_consume_normalizes_items_and_deduplicates_completed_output() -> None:
    message = {
        "type": "message", "id": "msg-1", "output_index": 0,
        "status": "completed", "role": "assistant",
        "phase": "commentary", "end_turn": False,
        "content": [{"type": "output_text", "text": "你好"}],
    }
    hosted = {"type": "web_search_call", "id": "host-1", "output_index": 1, "status": "completed"}
    local = {
        "type": "function_call", "id": "fn-1", "output_index": 2,
        "status": "completed", "call_id": "call-1", "name": "bash", "arguments": "{}",
    }

    async def stream() -> AsyncIterator[dict[str, Any]]:
        yield {"type": "response.output_text.delta", "item_id": "msg-1", "delta": "你好"}
        yield {"type": "response.output_item.done", "output_index": 0, "item": message}
        yield {"type": "response.output_item.done", "output_index": 1, "item": hosted}
        yield {"type": "response.output_item.done", "output_index": 2, "item": local}
        yield {"type": "response.completed", "response": {
            "id": "resp-1", "status": "completed", "output": [message, hosted, local], "usage": {},
        }}

    result = await responses.consume(stream(), idle_seconds=1)
    assert isinstance(result, NormalizedModelResponse)
    assert result.response_id == "resp-1"
    assert result.status is ResponseStatus.COMPLETED
    assert [type(item) for item in result.items] == [AssistantMessageItem, HostedToolItem, LocalToolCallItem]
    assert result.items[0].phase is OutputPhase.COMMENTARY
    assert result.items[0].end_turn is EndTurn.FALSE
    assert result.provider_payload["items"] == [message, hosted, local]


@pytest.mark.asyncio
async def test_responses_interruption_keeps_only_completed_items() -> None:
    unfinished = {"type": "function_call", "id": "fn-1", "call_id": "call-1", "name": "bash", "arguments": "{"}

    async def stream() -> AsyncIterator[dict[str, Any]]:
        yield {"type": "response.output_item.added", "item": unfinished}
        raise ConnectionError("closed")

    with pytest.raises(Exception) as captured:
        await responses.consume(stream(), idle_seconds=1)
    assert getattr(captured.value, "completed_items", []) == []


@pytest.mark.asyncio
async def test_responses_interruption_preserves_completed_item_and_rejects_duplicate_done() -> None:
    local = {"type": "function_call", "id": "fn-1", "call_id": "call-1", "name": "bash", "arguments": "{}", "status": "completed"}

    async def interrupted() -> AsyncIterator[dict[str, Any]]:
        yield {"type": "response.output_item.done", "item": local}
        yield {"type": "response.output_item.done", "item": local}
        raise StreamInterrupted("closed")

    with pytest.raises(Exception) as captured:
        await responses.consume(interrupted(), idle_seconds=1)
    assert getattr(captured.value, "completed_items", []) == [local]


@pytest.mark.asyncio
async def test_responses_rejects_duplicate_ids_inside_completed_output() -> None:
    item = {"type": "message", "id": "msg-1", "status": "completed", "content": []}

    async def stream() -> AsyncIterator[dict[str, Any]]:
        yield {"type": "response.completed", "response": {
            "id": "resp-1", "status": "completed", "output": [item, item],
        }}

    with pytest.raises(ValueError, match="duplicate item id"):
        await responses.consume(stream(), idle_seconds=1)


@pytest.mark.asyncio
async def test_assistant_item_callback_emits_each_content_state_once() -> None:
    item = {"type": "message", "id": "msg-1", "status": "completed", "content": [{"type": "output_text", "text": "ab"}]}

    async def stream() -> AsyncIterator[dict[str, Any]]:
        yield {"type": "response.output_text.delta", "item_id": "msg-1", "delta": "a"}
        yield {"type": "response.output_text.delta", "item_id": "msg-1", "delta": "b"}
        yield {"type": "response.output_item.done", "item": item}
        yield {"type": "response.completed", "response": {"status": "completed", "output": [item]}}

    seen: list[str] = []
    await responses.consume(stream(), idle_seconds=1, on_assistant_item=lambda value: seen.append(value.content))
    assert seen == ["a", "ab"]


@pytest.mark.asyncio
async def test_chat_completions_normalizes_content_and_tool_fragments_with_unknown_hints() -> None:
    async def stream() -> AsyncIterator[dict[str, Any]]:
        yield {"id": "chat-1", "choices": [{"delta": {"content": "答"}}]}
        yield {"choices": [{"delta": {"tool_calls": [{
            "index": 0, "id": "call-1", "type": "function",
            "function": {"name": "run", "arguments": "{}"},
        }]}}]}
        yield {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]}

    result = await chat_completions.consume(
        stream(), config=_config(), model="gpt-test", idle_seconds=1,
        on_delta=None, on_thought_delta=None, on_activity=None,
    )
    assert isinstance(result, NormalizedModelResponse)
    assistant = next(item for item in result.items if isinstance(item, AssistantMessageItem))
    assert assistant.content == "答"
    assert assistant.phase is OutputPhase.UNKNOWN
    assert assistant.end_turn is EndTurn.UNKNOWN
    assert any(isinstance(item, LocalToolCallItem) for item in result.items)


@pytest.mark.asyncio
async def test_chat_completions_preserves_confirmed_finish_when_usage_tail_breaks() -> None:
    async def stream() -> AsyncIterator[dict[str, Any]]:
        yield {"id": "chat-1", "choices": [{"delta": {"content": "完成"}}]}
        yield {"choices": [{"delta": {}, "finish_reason": "stop"}]}
        raise ConnectionError("usage tail closed")

    result = await chat_completions.consume(
        stream(), config=_config(), model="gpt-test", idle_seconds=1,
        on_delta=None, on_thought_delta=None, on_activity=None,
    )
    assert result.status is ResponseStatus.COMPLETED
    assert result.finish_reason == "stop"
    assert result.items[0].content == "完成"
