"""验证模型网关的请求构造、提供商选择、重试、错误转换、流式与非流式响应处理。

测试通过 fixture 或辅助函数准备隔离环境，再调用真实服务、路由或运行时，并检查返回值、持久化状态与可观察副作用。
变量约定：tmp_path/monkeypatch 提供隔离环境，client/store/runtime 驱动被测链路，各类 *_id 串联持久化实体，payload 表示输入，response/result 表示实际输出，expected 表示期望值。
"""

from __future__ import annotations

import asyncio
import json

import pytest

from src.model import gateway as model_gateway
from src.model.gateway import ModelConfigurationError, ProviderConfig, _litellm_model, bind_attachment_store, build_model_call


# 测试场景：验证接口或资源生命周期操作会返回正确结果并同步持久化状态；函数名 test_gateway_keeps_namespaced_openrouter_models_on_openrouter 精确标识本用例的具体条件。
def test_gateway_keeps_namespaced_openrouter_models_on_openrouter() -> None:
    assert _litellm_model("openrouter", "anthropic/claude-sonnet") == "openrouter/anthropic/claude-sonnet"
    assert _litellm_model("openai_compatible", "vendor/model") == "openai/vendor/model"


# 测试场景：验证接口或资源生命周期操作会返回正确结果并同步持久化状态；函数名 test_gateway_routes_deepseek_vision_through_openai_compatible_transport 精确标识本用例的具体条件。
def test_gateway_routes_deepseek_vision_through_openai_compatible_transport() -> None:
    assert _litellm_model(
        "deepseek",
        "deepseek-v4-flash-vision-exp",
    ) == "openai/deepseek-v4-flash-vision-exp"
    assert _litellm_model("deepseek", "deepseek-v4-flash") == "deepseek/deepseek-v4-flash"


@pytest.mark.asyncio
# 测试场景：验证该正常业务场景从输入准备到结果断言的完整链路；函数名 test_gateway_maps_openai_compatible_connection_and_thinking 精确标识本用例的具体条件。
async def test_gateway_maps_openai_compatible_connection_and_thinking(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict = {}

    # 局部测试函数：fake_completion 模拟该步骤的返回结果或异常。
    async def fake_completion(**kwargs):  # type: ignore[no-untyped-def]
        captured.update(kwargs)
        return {
            "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
            "usage": {
                "prompt_tokens": 100,
                "completion_tokens": 20,
                "total_tokens": 120,
                "prompt_tokens_details": {"cached_tokens": 30},
            },
        }

    monkeypatch.setattr(model_gateway, "get_api_key", lambda _ref: "secret")
    monkeypatch.setattr(model_gateway.litellm, "acompletion", fake_completion)
    monkeypatch.setattr(model_gateway.litellm, "completion_cost", lambda **_kwargs: 0.0042)
    call = build_model_call(ProviderConfig(
        provider="openai_compatible",
        base_url="https://relay.test/v1/",
        secret_ref="credential:test",
        model_id="custom-model",
        model_connection_id="connection-1",
        thinking_level="high",
        custom_headers={"X-Relay": "one"},
    ))

    response = await call(messages=[{"role": "user", "content": "hi"}], tools=[], mode="auto")
    assert captured["model"] == "openai/custom-model"
    assert captured["api_base"] == "https://relay.test/v1"
    assert captured["api_key"] == "secret"
    assert captured["reasoning_effort"] == "high"
    assert captured["max_retries"] == 0
    assert captured["extra_headers"] == {"X-Relay": "one"}
    assert response["usage"] == {
        "request_count": 1,
        "input_tokens": 70,
        "output_tokens": 20,
        "cache_creation_tokens": 0,
        "cache_read_tokens": 30,
        "total_tokens": 120,
        "cost_usd": 0.0042,
        "model_connection_id": "connection-1",
        "model_id": "custom-model",
        "provider": "openai_compatible",
    }


@pytest.mark.asyncio
# 测试场景：验证权限、审批或敏感数据边界在完整调用链路中保持有效；函数名 test_gateway_hydrates_private_image_refs_only_for_the_provider_call 精确标识本用例的具体条件。
async def test_gateway_hydrates_private_image_refs_only_for_the_provider_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict = {}

    # 测试替身类：AttachmentStore 保存该局部场景的可控状态。
    class AttachmentStore:
        # 辅助方法：data_url 实现测试替身在此调用阶段需要的最小行为。
        @staticmethod
        def data_url(attachment_id: str) -> str | None:
            return "data:image/png;base64,cHJpdmF0ZQ==" if attachment_id == "image-1" else None

    # 局部测试函数：fake_completion 模拟该步骤的返回结果或异常。
    async def fake_completion(**kwargs):  # type: ignore[no-untyped-def]
        captured.update(kwargs)
        return {
            "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 4, "completion_tokens": 1, "total_tokens": 5},
        }

    monkeypatch.setattr(model_gateway, "get_api_key", lambda _ref: "secret")
    monkeypatch.setattr(model_gateway.litellm, "acompletion", fake_completion)
    monkeypatch.setattr(model_gateway.litellm, "completion_cost", lambda **_kwargs: 0.0)
    source_messages = [{
        "role": "user",
        "content": [
            {"type": "text", "text": "inspect"},
            {"type": "pgagent_image_ref", "attachment_id": "image-1"},
        ],
    }]
    call = bind_attachment_store(
        build_model_call(ProviderConfig(
            provider="openai_compatible",
            base_url="https://relay.test/v1",
            secret_ref="credential:test",
            model_id="vision-model",
        )),
        AttachmentStore(),
    )

    await call(messages=source_messages, tools=[], mode="auto")

    assert captured["messages"][0]["content"][1] == {
        "type": "image_url",
        "image_url": {"url": "data:image/png;base64,cHJpdmF0ZQ=="},
    }
    assert source_messages[0]["content"][1]["type"] == "pgagent_image_ref"


@pytest.mark.asyncio
# 测试场景：验证该正常业务场景从输入准备到结果断言的完整链路；函数名 test_gateway_injects_rendered_pdf_page_after_contiguous_tool_results 精确标识本用例的具体条件。
async def test_gateway_injects_rendered_pdf_page_after_contiguous_tool_results() -> None:
    captured: dict = {}

    # 测试替身类：AttachmentStore 保存该局部场景的可控状态。
    class AttachmentStore:
        # 辅助方法：data_url 实现测试替身在此调用阶段需要的最小行为。
        @staticmethod
        def data_url(attachment_id: str) -> str | None:
            return "data:image/png;base64,cGFnZQ==" if attachment_id == "page-1" else None

    # 辅助方法：raw_model_call 实现测试替身在此调用阶段需要的最小行为。
    async def raw_model_call(**kwargs):  # type: ignore[no-untyped-def]
        captured.update(kwargs)
        return {"choices": [{"message": {"content": "ok"}}]}

    messages = [
        {"role": "assistant", "content": "", "tool_calls": [{"id": "call-1"}, {"id": "call-2"}]},
        {
            "role": "tool",
            "tool_call_id": "call-1",
            "content": json.dumps({
                "ok": True,
                "metadata": {"model_image_ref": {"id": "page-1"}},
            }),
        },
        {
            "role": "tool",
            "tool_call_id": "call-2",
            "content": json.dumps({"ok": True, "metadata": {}}),
        },
    ]

    await bind_attachment_store(raw_model_call, AttachmentStore())(
        messages=messages,
        tools=[],
        mode="auto",
    )

    hydrated = captured["messages"]
    assert [message["role"] for message in hydrated] == ["assistant", "tool", "tool", "user"]
    assert hydrated[-1]["content"][1] == {
        "type": "image_url",
        "image_url": {"url": "data:image/png;base64,cGFnZQ=="},
    }


@pytest.mark.asyncio
# 测试场景：验证该正常业务场景从输入准备到结果断言的完整链路；函数名 test_gateway_leaves_output_uncapped_and_disables_compaction_reasoning 精确标识本用例的具体条件。
async def test_gateway_leaves_output_uncapped_and_disables_compaction_reasoning(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict = {}

    # 局部测试函数：fake_completion 模拟该步骤的返回结果或异常。
    async def fake_completion(**kwargs):  # type: ignore[no-untyped-def]
        captured.update(kwargs)
        return {
            "choices": [{"message": {"content": "{}"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 4, "completion_tokens": 1, "total_tokens": 5},
        }

    monkeypatch.setattr(model_gateway, "get_api_key", lambda _ref: "secret")
    monkeypatch.setattr(model_gateway.litellm, "acompletion", fake_completion)
    monkeypatch.setattr(model_gateway.litellm, "completion_cost", lambda **_kwargs: 0.0)
    call = build_model_call(ProviderConfig(
        provider="deepseek",
        base_url="https://api.test/v1",
        secret_ref="credential:test",
        model_id="chat",
        thinking_level="high",
        max_output_tokens=8_000,
    ))

    await call(messages=[{"role": "user", "content": "compact"}], tools=[], mode="compaction")
    assert "max_tokens" not in captured
    assert "reasoning_effort" not in captured


@pytest.mark.asyncio
# 测试场景：验证非法、越界或不满足前置条件的操作会被明确拒绝，且不会产生错误状态；函数名 test_gateway_rejects_missing_secret 精确标识本用例的具体条件。
async def test_gateway_rejects_missing_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(model_gateway, "get_api_key", lambda _ref: None)
    call = build_model_call(ProviderConfig(
        provider="deepseek",
        base_url="https://api.deepseek.com/v1",
        secret_ref="missing",
        model_id="deepseek-chat",
    ))
    with pytest.raises(ModelConfigurationError, match="API Key"):
        await call(messages=[], tools=[], mode="auto")


@pytest.mark.asyncio
# 测试场景：验证该正常业务场景从输入准备到结果断言的完整链路；函数名 test_gateway_aggregates_streamed_text_and_usage 精确标识本用例的具体条件。
async def test_gateway_aggregates_streamed_text_and_usage(monkeypatch: pytest.MonkeyPatch) -> None:
    # 辅助方法：chunks 实现测试替身在此调用阶段需要的最小行为。
    async def chunks():  # type: ignore[no-untyped-def]
        yield {"choices": [{"delta": {"content": "hello "}}]}
        yield {"choices": [{"delta": {"content": "world"}}]}
        yield {"choices": [{"delta": {}, "finish_reason": "stop"}]}
        yield {
            "choices": [],
            "usage": {"prompt_tokens": 8, "completion_tokens": 2, "total_tokens": 10},
        }

    # 局部测试函数：fake_completion 模拟该步骤的返回结果或异常。
    async def fake_completion(**kwargs):  # type: ignore[no-untyped-def]
        assert kwargs["stream"] is True
        assert kwargs["stream_options"] == {"include_usage": True}
        return chunks()

    monkeypatch.setattr(model_gateway, "get_api_key", lambda _ref: "secret")
    monkeypatch.setattr(model_gateway.litellm, "acompletion", fake_completion)
    monkeypatch.setattr(model_gateway.litellm, "completion_cost", lambda **_kwargs: 0.01)
    call = build_model_call(ProviderConfig(
        provider="deepseek",
        base_url="https://api.test/v1",
        secret_ref="credential:test",
        model_id="chat",
    ))
    deltas: list[str] = []

    response = await call(messages=[], tools=[], mode="auto", on_delta=deltas.append)

    assert response["choices"][0]["message"]["content"] == "hello world"
    assert response["usage"]["input_tokens"] == 8
    assert response["usage"]["output_tokens"] == 2
    assert response["usage"]["cost_usd"] == 0.01
    assert deltas == ["hello ", "world"]


@pytest.mark.asyncio
# 测试场景：验证该正常业务场景从输入准备到结果断言的完整链路；函数名 test_gateway_streams_reasoning_separately_from_answer 精确标识本用例的具体条件。
async def test_gateway_streams_reasoning_separately_from_answer(monkeypatch: pytest.MonkeyPatch) -> None:
    # 辅助方法：chunks 实现测试替身在此调用阶段需要的最小行为。
    async def chunks():  # type: ignore[no-untyped-def]
        yield {"choices": [{"delta": {"reasoning_content": "先检查"}}]}
        yield {"choices": [{"delta": {"reasoning": "天气来源。"}}]}
        yield {"choices": [{"delta": {"content": "今天晴。"}}]}
        yield {"choices": [{"delta": {}, "finish_reason": "stop"}]}

    # 局部测试函数：fake_completion 模拟该步骤的返回结果或异常。
    async def fake_completion(**_kwargs):  # type: ignore[no-untyped-def]
        return chunks()

    monkeypatch.setattr(model_gateway, "get_api_key", lambda _ref: "secret")
    monkeypatch.setattr(model_gateway.litellm, "acompletion", fake_completion)
    monkeypatch.setattr(model_gateway.litellm, "completion_cost", lambda **_kwargs: 0.0)
    call = build_model_call(ProviderConfig(
        provider="deepseek",
        base_url="https://api.test/v1",
        secret_ref="credential:test",
        model_id="reasoner",
    ))
    answer_deltas: list[str] = []
    thought_deltas: list[str] = []

    response = await call(
        messages=[],
        tools=[],
        mode="auto",
        on_delta=answer_deltas.append,
        on_thought_delta=thought_deltas.append,
    )

    message = response["choices"][0]["message"]
    assert message["content"] == "今天晴。"
    assert message["reasoning_content"] == "先检查天气来源。"
    assert answer_deltas == ["今天晴。"]
    assert thought_deltas == ["先检查", "天气来源。"]


@pytest.mark.asyncio
# 测试场景：验证该正常业务场景从输入准备到结果断言的完整链路；函数名 test_gateway_aggregates_streamed_tool_call_fragments 精确标识本用例的具体条件。
async def test_gateway_aggregates_streamed_tool_call_fragments(monkeypatch: pytest.MonkeyPatch) -> None:
    # 辅助方法：chunks 实现测试替身在此调用阶段需要的最小行为。
    async def chunks():  # type: ignore[no-untyped-def]
        yield {
            "choices": [{"delta": {"tool_calls": [{
                "index": 0,
                "id": "call-7",
                "type": "function",
                "function": {"name": "run_", "arguments": "{\"command\":\"ec"},
            }]}}],
        }
        yield {
            "choices": [{"delta": {"tool_calls": [{
                "index": 0,
                "function": {"name": "command", "arguments": "ho hi\"}"},
            }]}}],
        }
        yield {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]}

    # 局部测试函数：fake_completion 模拟该步骤的返回结果或异常。
    async def fake_completion(**_kwargs):  # type: ignore[no-untyped-def]
        return chunks()

    monkeypatch.setattr(model_gateway, "get_api_key", lambda _ref: "secret")
    monkeypatch.setattr(model_gateway.litellm, "acompletion", fake_completion)
    monkeypatch.setattr(model_gateway.litellm, "completion_cost", lambda **_kwargs: 0.0)
    call = build_model_call(ProviderConfig(
        provider="openai_compatible",
        base_url="https://relay.test/v1",
        secret_ref="credential:test",
        model_id="custom-model",
    ))

    activity_count = 0

    # 辅助方法：on_activity 实现测试替身在此调用阶段需要的最小行为。
    def on_activity() -> None:
        nonlocal activity_count
        activity_count += 1

    response = await call(messages=[], tools=[], mode="auto", on_activity=on_activity)

    assert response["choices"][0]["message"]["tool_calls"] == [{
        "id": "call-7",
        "type": "function",
        "function": {"name": "run_command", "arguments": "{\"command\":\"echo hi\"}"},
    }]
    assert activity_count == 3


@pytest.mark.asyncio
# 测试场景：验证非法、越界或不满足前置条件的操作会被明确拒绝，且不会产生错误状态；函数名 test_gateway_falls_back_when_provider_explicitly_rejects_streaming 精确标识本用例的具体条件。
async def test_gateway_falls_back_when_provider_explicitly_rejects_streaming(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[bool] = []

    # 测试替身类：UnsupportedStreamError 保存该局部场景的可控状态。
    class UnsupportedStreamError(RuntimeError):
        status_code = 400

    # 局部测试函数：fake_completion 模拟该步骤的返回结果或异常。
    async def fake_completion(**kwargs):  # type: ignore[no-untyped-def]
        calls.append(kwargs["stream"])
        if kwargs["stream"]:
            raise UnsupportedStreamError("stream is not supported by this provider")
        assert "stream_options" not in kwargs
        return {
            "choices": [{"message": {"role": "assistant", "content": "one shot"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
        }

    monkeypatch.setattr(model_gateway, "get_api_key", lambda _ref: "secret")
    monkeypatch.setattr(model_gateway.litellm, "acompletion", fake_completion)
    monkeypatch.setattr(model_gateway.litellm, "completion_cost", lambda **_kwargs: 0.0)
    call = build_model_call(ProviderConfig(
        provider="openai_compatible",
        base_url="https://relay.test/v1",
        secret_ref="credential:test",
        model_id="custom-model",
    ))
    deltas: list[str] = []

    response = await call(messages=[], tools=[], mode="auto", on_delta=deltas.append)

    assert calls == [True, False]
    assert response["choices"][0]["message"]["content"] == "one shot"
    assert deltas == ["one shot"]


@pytest.mark.asyncio
# 测试场景：验证取消或终止请求会收敛相关运行状态，并正确清理或保留应有资源；函数名 test_gateway_preserves_cancellation_after_visible_stream_output 精确标识本用例的具体条件。
async def test_gateway_preserves_cancellation_after_visible_stream_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first_delta = asyncio.Event()

    # 辅助方法：chunks 实现测试替身在此调用阶段需要的最小行为。
    async def chunks():  # type: ignore[no-untyped-def]
        yield {"choices": [{"delta": {"content": "started"}}]}
        await asyncio.Event().wait()

    # 局部测试函数：fake_completion 模拟该步骤的返回结果或异常。
    async def fake_completion(**_kwargs):  # type: ignore[no-untyped-def]
        return chunks()

    monkeypatch.setattr(model_gateway, "get_api_key", lambda _ref: "secret")
    monkeypatch.setattr(model_gateway.litellm, "acompletion", fake_completion)
    call = build_model_call(ProviderConfig(
        provider="deepseek",
        base_url="https://api.test/v1",
        secret_ref="credential:test",
        model_id="chat",
    ))

    task = asyncio.create_task(call(
        messages=[],
        tools=[],
        mode="auto",
        on_delta=lambda _delta: first_delta.set(),
    ))
    await asyncio.wait_for(first_delta.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
