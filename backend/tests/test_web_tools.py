"""验证网页工具的地址安全分类、逐跳重定向校验、正文提取、错误体限制和命令分发。

测试通过 fixture 或辅助函数准备隔离环境，再调用真实服务、路由或运行时，并检查返回值、持久化状态与可观察副作用。
变量约定：tmp_path/monkeypatch 提供隔离环境，client/store/runtime 驱动被测链路，各类 *_id 串联持久化实体，payload 表示输入，response/result 表示实际输出，expected 表示期望值。
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import httpx

from src.tools import builtins
from src.tools.sandbox import WorkspaceSandbox


# 辅助函数：_install_transport 封装本组测试重复使用的输入准备、状态查询或测试替身行为。
def _install_transport(monkeypatch, handler) -> None:
    real_client = httpx.Client

    # 辅助方法：client_factory 实现测试替身在此调用阶段需要的最小行为。
    def client_factory(**kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_client(**kwargs)

    monkeypatch.setattr(builtins.httpx, "Client", client_factory)


# 测试场景：验证非法、越界或不满足前置条件的操作会被明确拒绝，且不会产生错误状态；函数名 test_web_address_classifier_rejects_non_global_ranges 精确标识本用例的具体条件。
def test_web_address_classifier_rejects_non_global_ranges() -> None:
    assert builtins._is_public_ip("8.8.8.8")
    assert not builtins._is_public_ip("127.0.0.1")
    assert not builtins._is_public_ip("100.64.0.1")
    assert not builtins._is_public_ip("169.254.1.1")


# 测试场景：验证该正常业务场景从输入准备到结果断言的完整链路；函数名 test_web_open_extracts_readable_structured_page_and_bounds_content 精确标识本用例的具体条件。
def test_web_open_extracts_readable_structured_page_and_bounds_content(monkeypatch, tmp_path) -> None:
    markup = """
    <html><head><title>Example story</title>
    <meta name="description" content="A concise description">
    <meta property="article:published_time" content="2026-09-02T08:00:00Z">
    <style>hidden css</style><script>hidden script</script></head>
    <body><article><h1>Headline</h1><p>Useful paragraph.</p><p>{body}</p></article></body></html>
    """.format(body="more text " * 2_000)
    monkeypatch.setattr(builtins, "_validate_public_http_url", lambda url: (url, "example.com"))
    _install_transport(
        monkeypatch,
        lambda request: httpx.Response(200, headers={"content-type": "text/html; charset=utf-8"}, text=markup),
    )

    result = builtins.web_open(WorkspaceSandbox(tmp_path), "https://example.com/story", max_chars=2_000)

    assert result.ok and result.tool_name == "web_open"
    payload = json.loads(result.content)
    assert payload["title"] == "Example story"
    assert payload["description"] == "A concise description"
    assert payload["published_at"] == "2026-09-02T08:00:00Z"
    assert "Useful paragraph" in payload["content"]
    assert "hidden script" not in result.content
    assert "<article>" not in result.content
    assert len(payload["content"]) == 2_000
    assert result.metadata["next_offset"] == 2_000


# 测试场景：验证该正常业务场景从输入准备到结果断言的完整链路；函数名 test_web_open_follows_redirect_only_after_validating_each_hop 精确标识本用例的具体条件。
def test_web_open_follows_redirect_only_after_validating_each_hop(monkeypatch, tmp_path) -> None:
    validated: list[str] = []

    # 辅助方法：validate 实现测试替身在此调用阶段需要的最小行为。
    def validate(url: str) -> tuple[str, str]:
        validated.append(url)
        return url, "example.com"

    # 辅助方法：handler 实现测试替身在此调用阶段需要的最小行为。
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/start":
            return httpx.Response(302, headers={"location": "/final"})
        return httpx.Response(200, headers={"content-type": "text/plain"}, text="finished")

    monkeypatch.setattr(builtins, "_validate_public_http_url", validate)
    _install_transport(monkeypatch, handler)

    result = builtins.web_open(WorkspaceSandbox(tmp_path), "https://example.com/start")

    assert result.ok
    assert validated == ["https://example.com/start", "https://example.com/final"]
    assert result.metadata["redirects"] == ["https://example.com/final"]


# 测试场景：验证非法、越界或不满足前置条件的操作会被明确拒绝，且不会产生错误状态；函数名 test_web_open_does_not_return_large_http_error_body 精确标识本用例的具体条件。
def test_web_open_does_not_return_large_http_error_body(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(builtins, "_validate_public_http_url", lambda url: (url, "example.com"))
    _install_transport(
        monkeypatch,
        lambda request: httpx.Response(
            503,
            headers={"content-type": "text/html"},
            text="sensitive error markup " * 10_000,
        ),
    )

    result = builtins.web_open(WorkspaceSandbox(tmp_path), "https://example.com/error")

    assert not result.ok and result.error_code == "http_error"
    assert result.content == "网页请求失败：HTTP 503。错误响应正文未放入上下文。"
    assert len(result.content) < 100


# 测试场景：验证该正常业务场景从输入准备到结果断言的完整链路；函数名 test_web_run_dispatches_codex_command_families 精确标识本用例的具体条件。
def test_web_run_dispatches_codex_command_families(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(builtins, "web_search", lambda *_args, **_kwargs: builtins.ToolResult(
        "websearch", True, "hit", metadata={"results": [{"ref_id": "search1", "url": "https://example.com"}]}
    ))
    monkeypatch.setattr(builtins, "web_open", lambda *_args, **_kwargs: builtins.ToolResult("web_open", True, "page"))
    for name, value in (("web_finance", "finance"), ("web_weather", "weather"), ("web_sports", "sports"), ("web_screenshot", "shot")):
        monkeypatch.setattr(builtins, name, lambda *_args, _value=value, **_kwargs: builtins.ToolResult("web_run", True, _value))
    monkeypatch.setattr(builtins, "get_current_time", lambda **_kwargs: builtins.ToolResult("get_current_time", True, "time"))
    result = builtins.web_run(
        WorkspaceSandbox(tmp_path), search_query=[{"q": "test"}], open=[{"ref_id": "search1"}],
        finance=[{"ticker": "AAPL"}], weather=[{"location": "Shanghai"}],
        sports=[{"league": "football/nfl"}], screenshot=[{"url": "https://example.com"}],
        time=[{"timezone": "Asia/Shanghai"}],
    )
    assert result.ok
    assert {item["type"] for item in json.loads(result.content)} == {"search_query", "open", "finance", "weather", "sports", "screenshot", "time"}


# 测试场景：web_run 聚合天气结果时保留安全来源地址，供 tool_finished 摘要展示。
def test_web_run_weather_propagates_source_url(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(
        builtins,
        "web_weather",
        lambda *_args, **_kwargs: builtins.ToolResult(
            "web_weather", True, "weather payload", metadata={"source_url": "https://api.open-meteo.com/v1/forecast"}
        ),
    )

    result = builtins.web_run(WorkspaceSandbox(tmp_path), weather=[{"location": "New York"}])

    assert result.ok
    assert result.metadata["source_url"] == "https://api.open-meteo.com/v1/forecast"
    assert json.loads(result.content)[0]["source_url"] == "https://api.open-meteo.com/v1/forecast"


# 测试场景：直接使用 open.url 时，空的缓存条目不能把 URL 变成字符串 None。
def test_web_run_opens_direct_url(monkeypatch, tmp_path) -> None:
    opened: list[str] = []

    def fake_open(_sandbox, url, **_kwargs):
        opened.append(url)
        return builtins.ToolResult("web_open", True, "page")

    monkeypatch.setattr(builtins, "web_open", fake_open)
    result = builtins.web_run(WorkspaceSandbox(tmp_path), open=[{"url": "https://example.com/story"}])

    assert result.ok
    assert opened == ["https://example.com/story"]


# 测试场景：直接 URL 兼容 Codex 的 ref 字段，避免模型使用别名时被解析成空 URL。
def test_web_run_accepts_ref_alias_for_open(monkeypatch, tmp_path) -> None:
    opened: list[str] = []

    def fake_open(_sandbox, url, **_kwargs):
        opened.append(url)
        return builtins.ToolResult("web_open", True, "page")

    monkeypatch.setattr(builtins, "web_open", fake_open)
    result = builtins.web_run(WorkspaceSandbox(tmp_path), open=[{"ref": "https://example.com/story"}])

    assert result.ok
    assert opened == ["https://example.com/story"]


# 测试场景：搜索入口严格拒绝包含中文的查询，模型必须先转换为纯英文再调用联网工具。
def test_web_run_rejects_non_english_search_query(monkeypatch, tmp_path) -> None:
    called = False

    def fake_search(*_args, **_kwargs):
        nonlocal called
        called = True
        return builtins.ToolResult("websearch", True, "unexpected")

    monkeypatch.setattr(builtins, "web_search", fake_search)
    result = builtins.web_run(WorkspaceSandbox(tmp_path), search_query=[{"q": "美国 今日 新闻"}])

    assert not result.ok
    assert result.error_code == "invalid_query_language"
    assert not called


# 测试场景：严格来源/时效约束没有召回结果时，自动用宽泛英文查询重试一次。
def test_web_run_retries_failed_search_without_strict_filters(monkeypatch, tmp_path) -> None:
    calls: list[dict] = []

    def fake_search(_sandbox, query, **kwargs):
        calls.append({"query": query, **kwargs})
        if len(calls) == 1:
            return builtins.ToolResult("websearch", False, "no results", error_code="search_provider_unavailable")
        return builtins.ToolResult(
            "websearch", True, "hit",
            metadata={"results": [{"url": "https://example.com/story", "title": "Story"}]},
        )

    monkeypatch.setattr(builtins, "web_search", fake_search)
    result = builtins.web_run(
        WorkspaceSandbox(tmp_path),
        search_query=[{"q": "latest US news", "recency": 1, "domains": ["reuters.com"]}],
    )

    assert result.ok
    assert len(calls) == 2
    assert calls[0]["recency"] == 1 and calls[0]["domains"] == ["reuters.com"]
    assert calls[1]["recency"] is None and calls[1]["domains"] is None


# 测试场景：一次 web_run 应合并去重查询并把时效、来源约束传给搜索后端。
def test_web_run_deduplicates_searches_and_preserves_order(monkeypatch, tmp_path) -> None:
    calls: list[dict] = []

    def fake_search(_sandbox, query, **kwargs):
        calls.append({"query": query, **kwargs})
        return builtins.ToolResult(
            "websearch", True, f"hit:{query}",
            metadata={"results": [{"ref_id": "search1", "title": query, "url": f"https://example.com/{len(calls)}"}]},
        )

    monkeypatch.setattr(builtins, "web_search", fake_search)
    result = builtins.web_run(
        WorkspaceSandbox(tmp_path),
        search_query=[
            {"q": "  latest US news  ", "recency": 1, "domains": ["apnews.com"]},
            {"q": "latest   US news"},
            {"q": "second query"},
        ],
    )

    assert result.ok
    assert [item["query"] for item in calls] == ["latest US news", "second query"]
    assert calls[0]["recency"] == 1
    assert calls[0]["domains"] == ["apnews.com"]
    output = json.loads(result.content)
    assert [item["query"] for item in output] == ["latest US news", "second query"]
    assert len({ref for ref in result.metadata["pages"]}) == 2


# 测试场景：旧模型可能把 schema 的 maxItems 元数据误放到调用参数顶层，兼容入口不能再让整轮搜索失败。
def test_web_run_accepts_legacy_max_items_alias(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(
        builtins,
        "web_search",
        lambda *_args, **_kwargs: builtins.ToolResult(
            "websearch", True, "hit", metadata={"results": []}
        ),
    )

    result = builtins.web_run(
        WorkspaceSandbox(tmp_path),
        search_query=[{"q": "today"}],
        maxItems=4,
    )

    assert result.ok


# 测试场景：批量搜索全部没有结果时，顶层状态必须反映失败，便于模型触发可观测重试。
def test_web_run_reports_all_search_failures(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(
        builtins,
        "web_search",
        lambda *_args, **_kwargs: builtins.ToolResult(
            "websearch", False, "搜索服务当前不可用", error_code="search_provider_unavailable"
        ),
    )

    result = builtins.web_run(
        WorkspaceSandbox(tmp_path),
        search_query=[{"q": "today"}, {"q": "tomorrow"}],
    )

    assert not result.ok
    assert result.error_code == "search_provider_unavailable"


# 测试场景：Bing RSS 的发布时间、摘要和来源必须进入结构化搜索结果，以便模型判断新闻时效。
def test_bing_rss_results_keep_date_description_and_source() -> None:
    markup = """<?xml version="1.0"?><rss><channel><item>
      <title>Headline</title><link>https://www.apnews.com/story</link>
      <description>Summary text</description><pubDate>Wed, 09 Sep 2026 08:20:00 GMT</pubDate>
    </item></channel></rss>"""

    results = builtins._bing_rss_results(markup, 5)

    assert results == [{
        "title": "Headline",
        "url": "https://www.apnews.com/story",
        "description": "Summary text",
        "published_at": "2026-09-09T08:20:00+00:00",
        "source": "apnews.com",
    }]
