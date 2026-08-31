from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from typing import Any

import pytest

from src.model import gateway as model_gateway
from src.model import request as model_request
from src.model.gateway import ProviderConfig, build_model_call
from src.model.streaming import IncompleteResponse


class FakeResponses:
    def __init__(self, streams: list[Callable[[], AsyncIterator[dict[str, Any]]]]) -> None:
        self.streams = streams
        self.requests: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> AsyncIterator[dict[str, Any]]:
        self.requests.append(kwargs)
        return self.streams.pop(0)()


def install_fake_client(
    monkeypatch: pytest.MonkeyPatch,
    streams: list[Callable[[], AsyncIterator[dict[str, Any]]]],
) -> FakeResponses:
    responses = FakeResponses(streams)

    class FakeOpenAI:
        def __init__(self, **_kwargs: Any) -> None:
            self.responses = responses

        async def close(self) -> None:
            return None

    async def no_pause(*_args: Any, **_kwargs: Any) -> None:
        return None

    monkeypatch.setattr(model_gateway, "get_api_key", lambda _ref: "secret")
    monkeypatch.setattr(model_request, "AsyncOpenAI", FakeOpenAI)
    monkeypatch.setattr(model_request, "pause", no_pause)
    return responses


def responses_config() -> ProviderConfig:
    return ProviderConfig(
        provider="openai_compatible",
        base_url="https://provider.test/v1",
        secret_ref="credential:test",
        model_id="gpt-test",
        model_connection_id="connection-1",
        thinking_level="high",
        api_protocol="responses",
    )


@pytest.mark.asyncio
async def test_responses_uses_native_items_and_stateless_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output_item = {
        "type": "message",
        "id": "msg-2",
        "status": "completed",
        "role": "assistant",
        "content": [{"type": "output_text", "text": "完成。", "annotations": []}],
    }

    async def stream() -> AsyncIterator[dict[str, Any]]:
        yield {"type": "response.output_text.delta", "delta": "完成。"}
        yield {"type": "response.output_item.done", "item": output_item}
        yield {
            "type": "response.completed",
            "response": {
                "status": "completed",
                "output": [output_item],
                "usage": {"input_tokens": 12, "output_tokens": 3, "total_tokens": 15},
            },
        }

    fake = install_fake_client(monkeypatch, [stream])
    call = build_model_call(responses_config())
    native_call = {
        "type": "function_call",
        "id": "fc-1",
        "call_id": "call-1",
        "name": "bash",
        "arguments": '{"command":"echo ok"}',
        "status": "completed",
    }
    deltas: list[str] = []

    response = await call(
        messages=[
            {"role": "user", "content": "运行命令"},
            {
                "role": "assistant",
                "content": "",
                "_pgagent_provider": {"protocol": "responses", "items": [native_call]},
            },
            {"role": "tool", "tool_call_id": "call-1", "content": "ok"},
        ],
        tools=[{
            "type": "function",
            "function": {
                "name": "bash",
                "description": "运行命令",
                "parameters": {"type": "object", "properties": {}},
            },
        }],
        mode="auto",
        on_delta=deltas.append,
    )

    request = fake.requests[0]
    assert request["store"] is False
    assert request["include"] == ["reasoning.encrypted_content"]
    assert request["reasoning"] == {"effort": "high"}
    assert request["input"][1] == native_call
    assert request["input"][2] == {
        "type": "function_call_output",
        "call_id": "call-1",
        "output": "ok",
    }
    assert request["tools"][0]["name"] == "bash"
    assert response["content"] == "完成。"
    assert response["_pgagent_provider"]["items"] == [output_item]
    assert response["usage"]["request_count"] == 1
    assert deltas == ["完成。"]


@pytest.mark.asyncio
async def test_responses_replays_only_completed_items_after_stream_disconnect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reasoning_item = {
        "type": "reasoning",
        "id": "rs-1",
        "status": "completed",
        "summary": [],
        "encrypted_content": "opaque",
    }
    message_item = {
        "type": "message",
        "id": "msg-2",
        "status": "completed",
        "role": "assistant",
        "content": [{"type": "output_text", "text": "恢复完成", "annotations": []}],
    }

    async def interrupted() -> AsyncIterator[dict[str, Any]]:
        yield {"type": "response.output_item.done", "item": reasoning_item}
        raise ConnectionError("socket closed")

    async def recovered() -> AsyncIterator[dict[str, Any]]:
        yield {"type": "response.output_item.done", "item": message_item}
        yield {
            "type": "response.completed",
            "response": {"status": "completed", "output": [message_item], "usage": {}},
        }

    fake = install_fake_client(monkeypatch, [interrupted, recovered])
    call = build_model_call(responses_config())

    response = await call(messages=[{"role": "user", "content": "继续"}], tools=[], mode="auto")

    assert len(fake.requests) == 2
    assert reasoning_item in fake.requests[1]["input"]
    assert response["content"] == "恢复完成"
    assert response["_pgagent_provider"]["items"] == [reasoning_item, message_item]
    assert response["usage"]["request_count"] == 2


@pytest.mark.asyncio
async def test_responses_returns_completed_tool_call_without_resampling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    function_call = {
        "type": "function_call",
        "id": "fc-1",
        "call_id": "call-1",
        "name": "bash",
        "arguments": '{"command":"pwd"}',
        "status": "completed",
    }

    async def interrupted() -> AsyncIterator[dict[str, Any]]:
        yield {"type": "response.output_item.done", "item": function_call}
        raise ConnectionError("socket closed")

    fake = install_fake_client(monkeypatch, [interrupted])
    call = build_model_call(responses_config())

    response = await call(messages=[{"role": "user", "content": "查看目录"}], tools=[], mode="auto")

    assert len(fake.requests) == 1
    assert response["tool_calls"][0]["id"] == "call-1"
    assert response["usage"]["request_count"] == 1


@pytest.mark.asyncio
async def test_responses_incomplete_event_is_not_retried(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def incomplete() -> AsyncIterator[dict[str, Any]]:
        yield {
            "type": "response.incomplete",
            "response": {"status": "incomplete", "incomplete_details": {"reason": "max_output_tokens"}},
        }

    fake = install_fake_client(monkeypatch, [incomplete])
    call = build_model_call(responses_config())

    with pytest.raises(IncompleteResponse, match="max_output_tokens"):
        await call(messages=[{"role": "user", "content": "回答"}], tools=[], mode="auto")
    assert len(fake.requests) == 1
