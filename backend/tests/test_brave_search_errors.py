"""Brave 查询约束和服务失败必须可观察，索引日期不等于新闻发布日期。"""

import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from src.tools import builtins as b
from src.tools.sandbox import WorkspaceSandbox


@pytest.fixture
def brave(monkeypatch, tmp_path):
    monkeypatch.setattr(b.settings, "brave_search_api_key", "unit-test-secret")
    monkeypatch.setattr(b, "_validate_public_http_url", lambda url: (url, "example.com"))
    real_client = httpx.Client
    calls, replies = [], []
    def handler(request):
        calls.append(request)
        reply = replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply
    def client(**kw):
        kw.pop("proxy", None)
        return real_client(**{**kw, "transport": httpx.MockTransport(handler)})
    monkeypatch.setattr(b.httpx, "Client", client)
    # 覆盖旧实现的顶层 get；实现迁移后仍只隔离网络边界。
    monkeypatch.setattr(b.httpx, "get", lambda url, **kw: handler(httpx.Request("GET", url, params=kw.get("params"), headers=kw.get("headers"))))
    return WorkspaceSandbox(tmp_path), calls, replies


def test_brave_domain_union_is_filtered_before_count_and_date_is_not_verified(brave):
    sandbox, calls, replies = brave
    replies.append(httpx.Response(200, json={"web": {"results": [
        {"title": "bad", "url": "https://notexample.com/a", "page_age": "2026-09-14"},
        {"title": "Article", "url": "https://example.com/article", "description": "news", "page_age": "2026-09-14"},
        {"title": "Other", "url": "https://other.org/article"},
    ]}}))
    result = b.web_run(sandbox, search_query=[{"q": "news", "domains": ["example.com", "other.org"], "limit": 2, "recency": 3}])
    data = json.loads(result.content)[0]
    assert data["ok"]
    assert [r["url"] for r in data["results"]] == ["https://example.com/article", "https://other.org/article"]
    assert calls[0].url.params["q"] == "news (site:example.com OR site:other.org)"
    today = datetime.now(timezone.utc).date()
    assert calls[0].url.params["freshness"] == f"{today - timedelta(days=3)}to{today}"
    assert data["results"][0]["published_at"] is None
    assert data["results"][0]["page_age"] == "2026-09-14"
    assert data["results"][0]["opened"] is False
    assert "unit-test-secret" not in result.content


def test_brave_empty_results_do_not_silently_switch_to_bing(brave):
    sandbox, calls, replies = brave
    replies.append(httpx.Response(200, json={"web": {"results": []}}))
    result = b.web_run(sandbox, search_query=[{"q": "nothing", "domains": ["example.com"]}])
    assert not result.ok
    assert json.loads(result.content)[0]["provider"] == "brave"
    assert len(calls) == 1


@pytest.mark.parametrize("status", [401, 403, 429])
def test_brave_auth_and_quota_errors_preserve_status_without_fallback(brave, status):
    sandbox, calls, replies = brave
    replies.append(httpx.Response(status, text="unit-test-secret"))
    result = b.web_run(sandbox, search_query=[{"q": "news"}])
    data = json.loads(result.content)[0]
    assert not result.ok
    assert data["status_code"] == status
    assert data["provider"] == "brave"
    assert len(calls) == 1
    assert "unit-test-secret" not in result.content


def test_brave_server_error_fallback_is_visible(brave):
    sandbox, calls, replies = brave
    replies.extend([
        httpx.Response(503, text="outage"),
        httpx.Response(200, headers={"content-type": "application/xml"}, text='<rss><channel><item><title>Other source</title><link>https://example.com/article</link></item></channel></rss>'),
    ])
    result = b.web_run(sandbox, search_query=[{"q": "news"}])
    data = json.loads(result.content)[0]
    assert result.ok
    assert data["provider"] == "bing_rss"
    assert data["fallback_from"] == "brave"
    assert data["initial_error_code"] == "brave_http_503"
    assert len(calls) == 2


def test_same_query_with_different_filters_is_not_deduplicated(brave):
    sandbox, calls, replies = brave
    replies.extend([httpx.Response(200, json={"web": {"results": []}}) for _ in range(2)])
    result = b.web_run(sandbox, search_query=[{"q": "news", "domains": ["a.org"]}, {"q": "news", "domains": ["b.org"]}])
    assert len(json.loads(result.content)) == 2
    assert len(calls) == 2


def test_brave_and_bing_failures_preserve_both_diagnostics(brave):
    sandbox, calls, replies = brave
    replies.extend([
        httpx.Response(503, text="brave outage"),
        httpx.ConnectError("bing unavailable"),
    ])

    result = b.web_search(sandbox, "news", domains=["example.com"])

    assert not result.ok
    assert result.error_code == "search_connection_error"
    assert result.metadata["provider"] == "bing_rss"
    assert result.metadata["fallback_from"] == "brave"
    assert result.metadata["initial_error_code"] == "brave_http_503"
    assert result.metadata["initial_status_code"] == 503
    assert len(calls) == 2


def test_brave_oversized_response_is_rejected_without_fallback(brave, monkeypatch):
    sandbox, calls, replies = brave
    monkeypatch.setattr(b, "MAX_WEB_RESPONSE_BYTES", 1_024)
    replies.extend([
        httpx.Response(200, content=b"{" + b"x" * 2_000 + b"}"),
        httpx.Response(200, text="<rss><channel></channel></rss>"),
    ])

    result = b.web_search(sandbox, "news")

    assert not result.ok
    assert result.error_code == "brave_response_too_large"
    assert result.metadata["provider"] == "brave"
    assert len(calls) == 1


def test_brave_and_bing_http_errors_keep_distinct_status_codes(brave):
    sandbox, calls, replies = brave
    replies.extend([
        httpx.Response(503, text="brave outage"),
        httpx.Response(502, text="bing outage"),
    ])

    result = b.web_search(sandbox, "news", domains=["example.com"])

    assert not result.ok
    assert result.error_code == "search_http_error"
    assert result.metadata["status_code"] == 502
    assert result.metadata["initial_status_code"] == 503
    assert result.metadata["initial_error_code"] == "brave_http_503"
    assert len(calls) == 2


@pytest.mark.parametrize("status", [401, 403, 429])
def test_brave_auth_status_wins_over_large_error_body(brave, monkeypatch, status):
    sandbox, calls, replies = brave
    monkeypatch.setattr(b, "MAX_WEB_RESPONSE_BYTES", 1_024)
    replies.append(httpx.Response(status, content=b"x" * 2_048))

    result = b.web_search(sandbox, "news")

    assert not result.ok
    assert result.error_code == f"brave_http_{status}"
    assert result.metadata["status_code"] == status
    assert len(calls) == 1
