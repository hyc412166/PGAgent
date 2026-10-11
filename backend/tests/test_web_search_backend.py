"""验证能力证据、工具授权与 provider 调用边界，避免搜索被静默换执行方。"""
from dataclasses import replace
import json

import httpx
import pytest

from src.model.config import ProviderConfig
from src.model.citations import render_url_citations
from src.model.protocols.responses import normalize_response, response_tools
from src.model.web_search import probe_hosted_web_search, supports_hosted_web_search
from src.tools import create_default_registry
from src.agent import AgentRuntime
from src.agent.turn import TurnLedger
from src.agent.engine import provider_web_search_calls


def config(**kwargs):
    return ProviderConfig(provider="openai_compatible", base_url="https://relay.test/v1",
                          secret_ref="test", model_id="gpt-5.5", api_protocol="responses", **kwargs)


def verified_config():
    return config(capabilities={"hosted_web_search": {"base_url": "https://relay.test/v1", "models": {
        "gpt-5.5": {"supported": True, "category": "ok"},
    }}})


def test_search_evidence_belongs_to_endpoint_model_and_protocol():
    verified = verified_config()
    assert supports_hosted_web_search(verified)
    assert not supports_hosted_web_search(config())
    assert not supports_hosted_web_search(replace(verified, model_id="other-model"))
    assert not supports_hosted_web_search(replace(verified, base_url="https://other.test/v1"))
    assert not supports_hosted_web_search(replace(verified, api_protocol="chat_completions"))
    assert supports_hosted_web_search(replace(config(), base_url="https://api.openai.com/v1"))
    assert not supports_hosted_web_search(replace(config(), base_url="https://api.openai.com.fake.test/v1"))


@pytest.mark.asyncio
async def test_hosted_search_keeps_auxiliary_commands_and_rejects_historical_local_calls(tmp_path):
    registry = create_default_registry(str(tmp_path), allowed_tool_names=["tool_search", "web_run", "web_open"])
    registry.configure_web_search(hosted=True)
    assert "web_search" in registry.model_visible_tool_names
    assert "web_open" not in registry.model_visible_tool_names
    properties = registry.schema_for("web_run")["parameters"]["properties"]
    assert {"screenshot", "finance", "weather", "sports", "time"} <= properties.keys()
    assert not {"search_query", "open", "click", "find"}.intersection(properties)
    discovered = await registry.execute_async("tool_search", {"query": "select:web_open"})
    assert discovered.metadata["activated_tools"] == []
    rejected = await registry.execute_async("web_run", {"search_query": [{"q": "OpenAI"}]})
    assert not rejected.ok and rejected.error_code == "tool_not_offered"
    result = await registry.execute_async("web_run", {"time": [{"utc_offset": "+00:00"}]})
    assert result.ok
    wire_tools = response_tools(registry.schemas, hosted_web_search=True)
    assert {"type": "web_search"} in wire_tools


def test_no_search_authorization_is_not_expanded(tmp_path):
    registry = create_default_registry(str(tmp_path), allowed_tool_names=["read", "tool_search"])
    registry.configure_web_search(hosted=True)
    assert "web_search" not in registry.enabled_tool_names
    assert "没有搜索权限" in registry.web_search_instructions


def test_unknown_responses_provider_uses_local_function_search(tmp_path):
    registry = create_default_registry(str(tmp_path), allowed_tool_names=["web_search"])
    registry.configure_web_search(hosted=False)
    tools = response_tools(registry.schemas)
    assert tools[0]["type"] == "function" and tools[0]["name"] == "web_search"


def test_hosted_timeline_preserves_queries_and_page_actions():
    calls = provider_web_search_calls({"items": [
        {"type": "web_search_call", "id": "ws-1", "status": "completed", "action": {
            "type": "search", "queries": ["OpenAI website", "OpenAI docs"],
        }},
        {"type": "web_search_call", "id": "ws-2", "status": "completed", "action": {
            "type": "open_page", "url": "https://openai.com",
        }},
    ]})
    assert calls[0]["query"] == "OpenAI website; OpenAI docs"
    assert calls[1]["query"] == "https://openai.com"


@pytest.mark.parametrize("events,supported", [
    ([{"type": "response.completed", "response": {"output": [
        {"type": "web_search_call", "status": "completed"},
    ]}}], True),
    ([{"type": "response.output_item.done", "item": {"type": "web_search_call", "status": "completed"}},
      {"type": "response.completed", "response": {"output": []}}], True),
    ([{"type": "response.completed", "response": {"output": []}}], False),
    ([{"type": "response.output_item.done", "item": {"type": "web_search_call", "status": "completed"}}], False),
    ([{"type": "response.failed", "response": {"error": {"code": "search_unavailable"}}}], None),
    ([{"type": "response.failed", "response": {"error": "quota"}}], None),
])
def test_probe_requires_real_completed_hosted_search(monkeypatch, events, supported):
    def handle(request):
        body = json.loads(request.content)
        assert body["tools"] == [{"type": "web_search"}]
        assert body["tool_choice"] == {"type": "web_search"}
        assert request.url.path == "/v1/responses"
        return httpx.Response(200, text="\n\n".join("data: " + json.dumps(event) for event in events))

    original = httpx.Client
    monkeypatch.setattr("src.model.web_search.httpx.Client", lambda **kwargs: original(transport=httpx.MockTransport(handle), **kwargs))
    result = probe_hosted_web_search("https://relay.test/v1", "test-key", "gpt-5.5")
    assert result["supported"] is supported
    if supported is None:
        assert result["error_code"] in {"search_unavailable", "response.failed"}


@pytest.mark.parametrize("event", [[], {"type": "response.completed", "response": "invalid"},
                                    {"type": "response.completed", "response": {"output": [1]}}])
def test_probe_reports_malformed_third_party_events(monkeypatch, event):
    original = httpx.Client
    monkeypatch.setattr("src.model.web_search.httpx.Client", lambda **kwargs: original(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, text="data: " + json.dumps(event))), **kwargs))
    result = probe_hosted_web_search("https://relay.test/v1", "test-key", "gpt-5.5")
    assert result["supported"] is None and result["category"] == "invalid_response"


@pytest.mark.parametrize("status,supported", [(400, False), (404, False), (429, None), (503, None)])
def test_probe_keeps_provider_errors_distinct_from_unsupported(monkeypatch, status, supported):
    original = httpx.Client
    monkeypatch.setattr("src.model.web_search.httpx.Client", lambda **kwargs: original(
        transport=httpx.MockTransport(lambda request: httpx.Response(status)), **kwargs))
    assert probe_hosted_web_search("https://relay.test/v1", "test-key", "gpt-5.5")["supported"] is supported


def test_citations_are_clickable_without_rewriting_provider_text():
    native = {"id": "resp-1", "status": "completed", "output": [{"type": "message", "id": "msg-1", "content": [
        {"type": "output_text", "text": "搜索结果。", "annotations": [
            {"type": "url_citation", "start_index": 0, "end_index": 5,
             "url": "https://example.com/a(b)", "title": '中文来源 "标题"'},
        ]},
    ]}]}
    response = normalize_response(native)
    item = response.items[0]
    assert item.content == "搜索结果。"
    assert "[1](<https://example.com/a(b)>" in item.display_content
    assert response.provider_payload["items"] == native["output"]
    assert render_url_citations("原文", [{"end_index": 2, "url": "javascript:alert(1)"}]) == "原文"


@pytest.mark.asyncio
async def test_hosted_search_runtime_delivers_citations_and_never_dispatches_local_search(tmp_path):
    native = {"id": "resp-hosted", "status": "completed", "output": [
        {"type": "web_search_call", "id": "ws-1", "status": "completed", "action": {
            "type": "search", "query": "OpenAI website", "sources": [{"url": "https://openai.com"}],
        }},
        {"type": "message", "id": "msg-hosted", "phase": "final_answer", "content": [
            {"type": "output_text", "text": "已核实官网。", "annotations": [
                {"type": "url_citation", "start_index": 0, "end_index": 6,
                 "url": "https://openai.com", "title": "OpenAI"},
            ]},
        ]},
    ]}
    response = normalize_response(native)
    registry = create_default_registry(str(tmp_path), allowed_tool_names=["web_run", "tool_search"])
    registry.configure_web_search(hosted=True)

    async def model_call(**kwargs):
        assert {"type": "web_search"} in response_tools(kwargs["tools"], hosted_web_search=True)
        return {"_pgagent_normalized_response": response}

    outcome = await AgentRuntime(model_call=model_call, tool_registry=registry).run(
        system_prompt="联网核实官网", recent_messages=[{"role": "user", "content": "查询官网"}])
    assert outcome.status == "completed"
    assert outcome.tool_calls == 1 and outcome.output_ledger.local_call_count == 0
    assert "[1](<https://openai.com>" in outcome.output
    assert "[1](<https://openai.com>" in next(event for event in outcome.events
                                             if event["type"] == "assistant_message_completed")["content"]
    assert outcome.transcript_delta[-1]["_pgagent_provider"]["items"] == native["output"]
    restored = TurnLedger.from_snapshot(outcome.output_ledger.to_snapshot())
    assert "[1](<https://openai.com>" in restored.responses[0].items[-1].display_content
