"""验证 Responses 模型适配器对消息、工具调用、推理项、用量和响应续接字段的转换。

测试通过 fixture 或辅助函数准备隔离环境，再调用真实服务、路由或运行时，并检查返回值、持久化状态与可观察副作用。
变量约定：tmp_path/monkeypatch 提供隔离环境，client/store/runtime 驱动被测链路，各类 *_id 串联持久化实体，payload 表示输入，response/result 表示实际输出，expected 表示期望值。
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from typing import Any

import pytest

from src.model import gateway as model_gateway
from src.model import request as model_request
from src.model.gateway import ProviderConfig, build_model_call
from src.model.streaming import IncompleteResponse
from src.model.protocols.responses import input_items, response_tools


# 测试替身类：FakeResponses 模拟外部依赖的响应与调用记录，使连接或协议测试无需访问真实服务。
class FakeResponses:
    # 辅助方法：__init__ 实现测试替身在此调用阶段需要的最小行为。
    def __init__(self, streams: list[Callable[[], AsyncIterator[dict[str, Any]]]]) -> None:
        self.streams = streams
        self.requests: list[dict[str, Any]] = []

    # 辅助方法：create 实现测试替身在此调用阶段需要的最小行为。
    async def create(self, **kwargs: Any) -> AsyncIterator[dict[str, Any]]:
        self.requests.append(kwargs)
        return self.streams.pop(0)()


# 辅助函数：install_fake_client 封装本组测试重复使用的输入准备、状态查询或测试替身行为。
def install_fake_client(
    monkeypatch: pytest.MonkeyPatch,
    streams: list[Callable[[], AsyncIterator[dict[str, Any]]]],
) -> FakeResponses:
    responses = FakeResponses(streams)

    # 测试替身类：FakeOpenAI 保存该局部场景的可控状态。
    class FakeOpenAI:
        # 辅助方法：__init__ 实现测试替身在此调用阶段需要的最小行为。
        def __init__(self, **_kwargs: Any) -> None:
            self.responses = responses

        # 辅助方法：close 实现测试替身在此调用阶段需要的最小行为。
        async def close(self) -> None:
            return None

    # 辅助方法：no_pause 实现测试替身在此调用阶段需要的最小行为。
    async def no_pause(*_args: Any, **_kwargs: Any) -> None:
        return None

    monkeypatch.setattr(model_gateway, "get_api_key", lambda _ref: "secret")
    monkeypatch.setattr(model_request, "AsyncOpenAI", FakeOpenAI)
    monkeypatch.setattr(model_request, "pause", no_pause)
    return responses


# 辅助函数：responses_config 封装本组测试重复使用的输入准备、状态查询或测试替身行为。
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
# 测试场景：验证该正常业务场景从输入准备到结果断言的完整链路；函数名 test_responses_uses_native_items_and_stateless_request 精确标识本用例的具体条件。
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

    # 辅助方法：stream 实现测试替身在此调用阶段需要的最小行为。
    async def stream() -> AsyncIterator[dict[str, Any]]:
        yield {"type": "response.output_text.delta", "delta": "完成。"}
        yield {"type": "response.output_item.done", "item": output_item}
        yield {
            "type": "response.completed",
            "response": {
                "status": "completed",
                "output": [output_item],
                "usage": {
                    "input_tokens": 12,
                    "input_tokens_details": {"cached_tokens": 8, "cache_write_tokens": 0},
                    "output_tokens": 3,
                    "total_tokens": 15,
                },
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
    assert response["usage"]["input_tokens"] == 4
    assert response["usage"]["cache_read_tokens"] == 8
    assert response["usage"]["total_tokens"] == 15
    assert deltas == ["完成。"]


@pytest.mark.asyncio
# 测试场景：验证状态能够可靠持久化、重放或在重启后恢复，并保持记录之间的关联；函数名 test_responses_replays_only_completed_items_after_stream_disconnect 精确标识本用例的具体条件。
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

    # 辅助方法：interrupted 实现测试替身在此调用阶段需要的最小行为。
    async def interrupted() -> AsyncIterator[dict[str, Any]]:
        yield {"type": "response.output_item.done", "item": reasoning_item}
        raise ConnectionError("socket closed")

    # 辅助方法：recovered 实现测试替身在此调用阶段需要的最小行为。
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
    assert {key: value for key, value in reasoning_item.items() if key != "status"} in fake.requests[1]["input"]
    assert response["content"] == "恢复完成"
    assert response["_pgagent_provider"]["items"] == [reasoning_item, message_item]
    assert response["usage"]["request_count"] == 2


@pytest.mark.asyncio
# 测试场景：验证该正常业务场景从输入准备到结果断言的完整链路；函数名 test_responses_returns_completed_tool_call_without_resampling 精确标识本用例的具体条件。
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

    # 辅助方法：interrupted 实现测试替身在此调用阶段需要的最小行为。
    async def interrupted() -> AsyncIterator[dict[str, Any]]:
        yield {"type": "response.output_item.done", "item": function_call}
        raise ConnectionError("socket closed")

    fake = install_fake_client(monkeypatch, [interrupted])
    call = build_model_call(responses_config())

    response = await call(messages=[{"role": "user", "content": "查看目录"}], tools=[], mode="auto")

    assert len(fake.requests) == 1
    assert response["tool_calls"][0]["id"] == "call-1"
    assert response["usage"]["request_count"] == 1


# 测试场景：验证该正常业务场景从输入准备到结果断言的完整链路；函数名 test_responses_preserves_native_web_search_tool_definition 精确标识本用例的具体条件。
def test_responses_preserves_native_web_search_tool_definition() -> None:
    tools = [{
        "type": "web_search",
        "search_context_size": "medium",
        "user_location": {"type": "approximate", "country": "US"},
    }, {
        "type": "function",
        "function": {
            "name": "read",
            "description": "读取文件",
            "parameters": {"type": "object", "properties": {}},
        },
    }, {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": "联网搜索",
            "parameters": {"type": "object", "properties": {}},
        },
    }]

    assert response_tools(tools) == [tools[0], {
        "type": "function",
        "name": "read",
        "description": "读取文件",
        "parameters": {"type": "object", "properties": {}},
        "strict": False,
    }, {"type": "web_search"}]


@pytest.mark.asyncio
# 测试场景：验证该正常业务场景从输入准备到结果断言的完整链路；函数名 test_responses_keeps_hosted_web_search_call_out_of_local_dispatch 精确标识本用例的具体条件。
async def test_responses_keeps_hosted_web_search_call_out_of_local_dispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    web_search_call = {
        "type": "web_search_call",
        "id": "ws-1",
        "status": "completed",
        "action": {"type": "search", "query": "latest US news", "sources": [{
            "type": "url", "url": "https://example.com/news",
        }]},
    }
    message = {
        "type": "message",
        "id": "msg-1",
        "status": "completed",
        "role": "assistant",
        "content": [{"type": "output_text", "text": "检索完成。", "annotations": [{
            "type": "url_citation",
            "start_index": 0,
            "end_index": 5,
            "title": "Example News",
            "url": "https://example.com/news",
        }]}],
    }

    # 辅助方法：stream 实现测试替身在此调用阶段需要的最小行为。
    async def stream() -> AsyncIterator[dict[str, Any]]:
        yield {"type": "response.output_item.done", "item": web_search_call}
        yield {"type": "response.output_item.done", "item": message}
        yield {"type": "response.completed", "response": {
            "status": "completed", "output": [web_search_call, message], "usage": {},
        }}

    fake = install_fake_client(monkeypatch, [stream])
    response = await build_model_call(responses_config())(
        messages=[{"role": "user", "content": "搜索新闻"}],
        tools=[{"type": "web_search", "search_context_size": "low"}],
        mode="auto",
    )

    assert fake.requests[0]["tools"] == [{"type": "web_search", "search_context_size": "low"}]
    assert fake.requests[0]["include"] == [
        "reasoning.encrypted_content",
        "web_search_call.action.sources",
    ]
    assert response["content"] == "检索完成。"
    assert response["tool_calls"] == []
    assert response["_pgagent_provider"]["items"] == [web_search_call, message]
    assert input_items([{
        "role": "assistant",
        "content": response["content"],
        "_pgagent_provider": response["_pgagent_provider"],
    }]) == [web_search_call, message]


@pytest.mark.asyncio
# 测试场景：验证状态能够可靠持久化、重放或在重启后恢复，并保持记录之间的关联；函数名 test_responses_replays_completed_hosted_call_after_disconnect 精确标识本用例的具体条件。
async def test_responses_replays_completed_hosted_call_after_disconnect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    web_search_call = {
        "type": "web_search_call", "id": "ws-1", "status": "completed",
        "action": {"type": "search", "query": "latest news"},
    }
    message = {
        "type": "message", "id": "msg-1", "status": "completed", "role": "assistant",
        "content": [{"type": "output_text", "text": "完成。", "annotations": []}],
    }

    # 辅助方法：interrupted 实现测试替身在此调用阶段需要的最小行为。
    async def interrupted() -> AsyncIterator[dict[str, Any]]:
        yield {"type": "response.output_item.done", "item": web_search_call}
        raise ConnectionError("socket closed")

    # 辅助方法：recovered 实现测试替身在此调用阶段需要的最小行为。
    async def recovered() -> AsyncIterator[dict[str, Any]]:
        yield {"type": "response.output_item.done", "item": message}
        yield {"type": "response.completed", "response": {
            "status": "completed", "output": [message], "usage": {},
        }}

    fake = install_fake_client(monkeypatch, [interrupted, recovered])
    response = await build_model_call(responses_config())(
        messages=[{"role": "user", "content": "搜索"}],
        tools=[{"type": "web_search"}],
        mode="auto",
    )

    assert len(fake.requests) == 2
    assert web_search_call in fake.requests[1]["input"]
    assert response["tool_calls"] == []
    assert response["_pgagent_provider"]["items"] == [web_search_call, message]


@pytest.mark.asyncio
@pytest.mark.parametrize(("level", "mode", "expected"), [
    ("auto", "auto", None),
    ("", "auto", None),
    ("medium", "auto", {"effort": "medium"}),
    ("off", "auto", None),
    ("high", "compaction", None),
])
# 测试场景：验证该正常业务场景从输入准备到结果断言的完整链路；函数名 test_responses_reasoning_effort_respects_thinking_and_compaction 精确标识本用例的具体条件。
async def test_responses_reasoning_effort_respects_thinking_and_compaction(
    monkeypatch: pytest.MonkeyPatch, level: str, mode: str, expected: dict | None,
) -> None:
    # 辅助方法：stream 实现测试替身在此调用阶段需要的最小行为。
    async def stream():
        yield {"type": "response.completed", "response": {"output": []}}

    fake = install_fake_client(monkeypatch, [stream])
    config = responses_config()
    config.thinking_level = level
    await build_model_call(config)(messages=[], tools=[], mode=mode)
    assert fake.requests[0].get("reasoning") == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("delivery", ["delta", "partial", "done", "item", "completed"])
# 测试场景：验证该正常业务场景从输入准备到结果断言的完整链路；函数名 test_responses_displays_summary_once_across_completion_events 精确标识本用例的具体条件。
async def test_responses_displays_summary_once_across_completion_events(
    monkeypatch: pytest.MonkeyPatch, delivery: str,
) -> None:
    item = {"type": "reasoning", "id": "rs-1", "summary": [
        {"type": "summary_text", "text": "检查条件。"},
        {"type": "summary_text", "text": "核对结果。"},
    ], "encrypted_content": "opaque-not-displayable"}
    second = {"type": "reasoning", "id": "rs-2", "summary": [
        {"type": "summary_text", "text": "检查条件。"},
    ]}

    # 辅助方法：stream 实现测试替身在此调用阶段需要的最小行为。
    async def stream():
        for current in [item, second]:
            for index, part in enumerate(current["summary"]):
                fields = {"item_id": current["id"], "summary_index": index}
                if delivery in {"delta", "partial"}:
                    yield {"type": "response.reasoning_summary_text.delta", **fields,
                           "delta": part["text"] if delivery == "delta" else part["text"][:2]}
                if delivery in {"delta", "partial", "done"}:
                    yield {"type": "response.reasoning_summary_text.done", **fields, "text": part["text"]}
            if delivery != "completed":
                yield {"type": "response.output_item.done", "item": current}
        yield {"type": "response.output_text.delta", "delta": "答案。"}
        yield {"type": "response.completed", "response": {"output": [item, second, {
            "type": "message", "content": [{"type": "output_text", "text": "答案。"}],
        }]}}

    install_fake_client(monkeypatch, [stream])
    thoughts: list[str] = []
    answers: list[str] = []
    response = await build_model_call(responses_config())(
        messages=[], tools=[], mode="auto", on_thought_delta=thoughts.append, on_delta=answers.append,
    )
    assert "".join(thoughts) == "检查条件。核对结果。检查条件。"
    assert response["reasoning_content"] == "".join(thoughts)
    assert answers == ["答案。"]


@pytest.mark.asyncio
# 测试场景：验证非法、越界或不满足前置条件的操作会被明确拒绝，且不会产生错误状态；函数名 test_responses_encrypted_reasoning_does_not_create_thought_text 精确标识本用例的具体条件。
async def test_responses_encrypted_reasoning_does_not_create_thought_text(monkeypatch):
    item = {"type": "reasoning", "id": "rs-1", "summary": [], "encrypted_content": "opaque"}

    # 辅助方法：stream 实现测试替身在此调用阶段需要的最小行为。
    async def stream():
        yield {"type": "response.output_item.done", "item": item}
        yield {"type": "response.completed", "response": {"output": [item]}}

    install_fake_client(monkeypatch, [stream])
    thoughts: list[str] = []
    await build_model_call(responses_config())(
        messages=[], tools=[], mode="auto", on_thought_delta=thoughts.append,
    )
    assert thoughts == []


@pytest.mark.asyncio
# 测试场景：验证该正常业务场景从输入准备到结果断言的完整链路；函数名 test_responses_nonstream_summary_reaches_thought_callback 精确标识本用例的具体条件。
async def test_responses_nonstream_summary_reaches_thought_callback(monkeypatch):
    fake = install_fake_client(monkeypatch, [])

    # 辅助方法：create 实现测试替身在此调用阶段需要的最小行为。
    async def create(**kwargs):
        return {"status": "completed", "output": [{
            "type": "reasoning", "summary": [{"type": "summary_text", "text": "核对条件。"}],
        }]}

    monkeypatch.setattr(fake, "create", create)
    thoughts: list[str] = []
    await build_model_call(responses_config())(
        messages=[], tools=[], mode="auto", on_thought_delta=thoughts.append,
    )
    assert thoughts == ["核对条件。"]


@pytest.mark.asyncio
# 测试场景：验证该正常业务场景从输入准备到结果断言的完整链路；函数名 test_responses_incomplete_event_is_not_retried 精确标识本用例的具体条件。
async def test_responses_incomplete_event_is_not_retried(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 辅助方法：incomplete 实现测试替身在此调用阶段需要的最小行为。
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


@pytest.mark.asyncio
@pytest.mark.parametrize("delivery", ["item_done", "completed", "nonstream"])
# 测试场景：验证非法、越界或不满足前置条件的操作会被明确拒绝，且不会产生错误状态；函数名 test_responses_sdk_does_not_add_unset_fields_to_native_history 精确标识本用例的具体条件。
async def test_responses_sdk_does_not_add_unset_fields_to_native_history(monkeypatch, delivery):
    from openai.types.responses import (
        Response, ResponseCompletedEvent, ResponseFunctionToolCall,
        ResponseOutputItemDoneEvent, ResponseReasoningItem,
    )

    reasoning = ResponseReasoningItem.model_validate({
        "type": "reasoning", "id": "rs-1", "summary": [], "encrypted_content": "opaque",
    })
    function = ResponseFunctionToolCall.model_validate({
        "type": "function_call", "id": "fc-1", "call_id": "call-1", "name": "websearch",
        "arguments": '{"query":"天气"}', "status": "completed",
    })
    # SDK 未赋值字段仍有默认 None，不能让它们变成下一轮请求的参数。
    assert reasoning.model_dump()["status"] is None
    response = Response.model_construct(status="completed", output=[reasoning, function])

    # 辅助方法：stream 实现测试替身在此调用阶段需要的最小行为。
    async def stream():
        if delivery == "item_done":
            yield ResponseOutputItemDoneEvent.model_construct(type="response.output_item.done", item=reasoning, output_index=0)
            yield ResponseOutputItemDoneEvent.model_construct(type="response.output_item.done", item=function, output_index=1)
            raise ConnectionError("断流后返回已完成工具调用")
        yield ResponseCompletedEvent.model_construct(type="response.completed", response=response)

    fake = install_fake_client(monkeypatch, [stream])
    if delivery == "nonstream":
        # 辅助方法：create 实现测试替身在此调用阶段需要的最小行为。
        async def create(**kwargs):
            return response
        monkeypatch.setattr(fake, "create", create)
    result = await build_model_call(responses_config())(messages=[], tools=[], mode="auto")
    items = result["_pgagent_provider"]["items"]
    assert items == [reasoning.model_dump(exclude_unset=True), function.model_dump(exclude_unset=True)]
    assert "status" not in items[0]
    assert "namespace" not in items[1]
    assert "caller" not in items[1]
    assert items[1]["status"] == "completed"


@pytest.mark.parametrize("status", [None, "completed"])
# 测试场景：验证该正常业务场景从输入准备到结果断言的完整链路；函数名 test_responses_legacy_reasoning_status_is_removed_only_from_request 精确标识本用例的具体条件。
def test_responses_legacy_reasoning_status_is_removed_only_from_request(status):
    from copy import deepcopy

    items = [
        {"type": "reasoning", "id": "rs-1", "status": status,
         "summary": [{"type": "summary_text", "text": "检查天气。"}], "encrypted_content": "opaque"},
        {"type": "message", "id": "msg-1", "role": "assistant", "status": "completed",
         "content": [{"type": "output_text", "text": "准备查询。"}]},
        {"type": "function_call", "id": "fc-1", "call_id": "call-1", "status": "completed",
         "name": "websearch", "arguments": '{"query":"天气"}'},
    ]
    messages = [{"role": "assistant", "_pgagent_provider": {"protocol": "responses", "items": items}},
                {"role": "tool", "tool_call_id": "call-1", "content": "搜索连接超时"}]
    original = deepcopy(messages)
    request_items = input_items(messages)
    assert request_items[0] == {key: value for key, value in items[0].items() if key != "status"}
    assert request_items[1:3] == items[1:]
    assert request_items[3] == {"type": "function_call_output", "call_id": "call-1", "output": "搜索连接超时"}
    assert messages == original
