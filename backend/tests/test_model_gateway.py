from __future__ import annotations

import asyncio

import pytest

from app.services import model_gateway
from app.services.model_gateway import ModelConfigurationError, ProviderConfig, _litellm_model, build_model_call


def test_gateway_keeps_namespaced_openrouter_models_on_openrouter() -> None:
    assert _litellm_model("openrouter", "anthropic/claude-sonnet") == "openrouter/anthropic/claude-sonnet"
    assert _litellm_model("openai_compatible", "vendor/model") == "openai/vendor/model"


@pytest.mark.asyncio
async def test_gateway_maps_openai_compatible_connection_and_thinking(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict = {}

    async def fake_completion(**kwargs):  # type: ignore[no-untyped-def]
        captured.update(kwargs)
        return {
            "choices": [{"message": {"content": "ok"}}],
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
async def test_gateway_leaves_output_uncapped_and_disables_compaction_reasoning(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict = {}

    async def fake_completion(**kwargs):  # type: ignore[no-untyped-def]
        captured.update(kwargs)
        return {
            "choices": [{"message": {"content": "{}"}}],
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
async def test_gateway_aggregates_streamed_text_and_usage(monkeypatch: pytest.MonkeyPatch) -> None:
    async def chunks():  # type: ignore[no-untyped-def]
        yield {"choices": [{"delta": {"content": "hello "}}]}
        yield {"choices": [{"delta": {"content": "world"}}]}
        yield {
            "choices": [],
            "usage": {"prompt_tokens": 8, "completion_tokens": 2, "total_tokens": 10},
        }

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
async def test_gateway_streams_reasoning_separately_from_answer(monkeypatch: pytest.MonkeyPatch) -> None:
    async def chunks():  # type: ignore[no-untyped-def]
        yield {"choices": [{"delta": {"reasoning_content": "先检查"}}]}
        yield {"choices": [{"delta": {"reasoning": "天气来源。"}}]}
        yield {"choices": [{"delta": {"content": "今天晴。"}}]}

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
async def test_gateway_aggregates_streamed_tool_call_fragments(monkeypatch: pytest.MonkeyPatch) -> None:
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

    response = await call(messages=[], tools=[], mode="auto")

    assert response["choices"][0]["message"]["tool_calls"] == [{
        "id": "call-7",
        "type": "function",
        "function": {"name": "run_command", "arguments": "{\"command\":\"echo hi\"}"},
    }]


@pytest.mark.asyncio
async def test_gateway_falls_back_when_provider_explicitly_rejects_streaming(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[bool] = []

    class UnsupportedStreamError(RuntimeError):
        status_code = 400

    async def fake_completion(**kwargs):  # type: ignore[no-untyped-def]
        calls.append(kwargs["stream"])
        if kwargs["stream"]:
            raise UnsupportedStreamError("stream is not supported by this provider")
        assert "stream_options" not in kwargs
        return {
            "choices": [{"message": {"role": "assistant", "content": "one shot"}}],
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
async def test_gateway_preserves_cancellation_after_visible_stream_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first_delta = asyncio.Event()

    async def chunks():  # type: ignore[no-untyped-def]
        yield {"choices": [{"delta": {"content": "started"}}]}
        await asyncio.Event().wait()

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
