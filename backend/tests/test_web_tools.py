from __future__ import annotations

import json

import httpx

from src.tools import builtins
from src.tools.sandbox import WorkspaceSandbox


def _install_transport(monkeypatch, handler) -> None:
    real_client = httpx.Client

    def client_factory(**kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_client(**kwargs)

    monkeypatch.setattr(builtins.httpx, "Client", client_factory)


def test_web_address_classifier_rejects_non_global_ranges() -> None:
    assert builtins._is_public_ip("8.8.8.8")
    assert not builtins._is_public_ip("127.0.0.1")
    assert not builtins._is_public_ip("100.64.0.1")
    assert not builtins._is_public_ip("169.254.1.1")


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


def test_web_open_follows_redirect_only_after_validating_each_hop(monkeypatch, tmp_path) -> None:
    validated: list[str] = []

    def validate(url: str) -> tuple[str, str]:
        validated.append(url)
        return url, "example.com"

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


def test_web_run_dispatches_codex_command_families(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(builtins, "web_search", lambda *_args, **_kwargs: builtins.ToolResult(
        "websearch", True, "hit", metadata={"results": [{"ref_id": "search1", "url": "https://example.com"}]}
    ))
    monkeypatch.setattr(builtins, "web_open", lambda *_args, **_kwargs: builtins.ToolResult("web_open", True, "page"))
    for name, value in (("web_finance", "finance"), ("web_weather", "weather"), ("web_sports", "sports"), ("web_screenshot", "shot")):
        monkeypatch.setattr(builtins, name, lambda *_args, _value=value, **_kwargs: builtins.ToolResult("web.run", True, _value))
    monkeypatch.setattr(builtins, "get_current_time", lambda **_kwargs: builtins.ToolResult("get_current_time", True, "time"))
    result = builtins.web_run(
        WorkspaceSandbox(tmp_path), search_query=[{"q": "test"}], open=[{"ref_id": "search1"}],
        finance=[{"ticker": "AAPL"}], weather=[{"location": "Shanghai"}],
        sports=[{"league": "football/nfl"}], screenshot=[{"url": "https://example.com"}],
        time=[{"timezone": "Asia/Shanghai"}],
    )
    assert result.ok
    assert {item["type"] for item in json.loads(result.content)} == {"search_query", "open", "finance", "weather", "sports", "screenshot", "time"}
