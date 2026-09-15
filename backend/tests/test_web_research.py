"""源码研究的真实工具链；仅替换外部 HTTP，不替换页面解析和导航逻辑。"""

import json
import asyncio

import httpx
import pytest

from src.agent.engine import AgentRuntime
from src.tools import builtins
from src.tools.registry import create_default_registry
from src.tools.sandbox import WorkspaceSandbox


@pytest.fixture
def web(monkeypatch, tmp_path):
    documents = {}
    requests = []
    real_client = httpx.Client

    def handler(request):
        requests.append(str(request.url))
        mime, text = documents[str(request.url)]
        return httpx.Response(200, headers={"content-type": mime}, text=text)

    monkeypatch.setattr(builtins, "_validate_public_http_url", lambda url: (url, "example.com"))
    monkeypatch.setattr(builtins, "_response_peer_is_public", lambda *a, **kw: True)
    def client_factory(**kw):
        kw.pop("proxy", None)
        return real_client(**{**kw, "transport": httpx.MockTransport(handler)})
    monkeypatch.setattr(builtins.httpx, "Client", client_factory)
    return WorkspaceSandbox(tmp_path), documents, requests


def command(result):
    assert result.ok, result.content
    return json.loads(result.content)[0]


def test_find_reads_beyond_initial_excerpt_and_open_reuses_document(web):
    sandbox, documents, requests = web
    url = "https://example.com/source.ts"
    documents[url] = ("text/plain", "\n".join(f"const value{i} = {i};" for i in range(1200)))
    opened = builtins.web_run(sandbox, open=[{"ref_id": url}], response_length="short")
    page = command(opened)
    assert "L0: const value0 = 0;" in page["content"]
    assert page["total_lines"] == 1200
    assert page["truncated"] and page["next_lineno"] > 0
    assert "value1100" not in page["content"]
    found = builtins.web_run(sandbox, find=[{"ref_id": page["ref_id"], "pattern": "value1100"}], pages=opened.metadata["pages"])
    hit = command(found)
    assert hit["matches"][0]["lineno"] == 1100
    assert "L1100: const value1100 = 1100;" in hit["content"]
    documents[url] = ("text/plain", "changed upstream")
    continued = builtins.web_run(sandbox, open=[{"ref_id": page["ref_id"], "lineno": 1099}], pages=found.metadata["pages"])
    assert "L1099: const value1099 = 1099;" in command(continued)["content"]
    assert requests == [url]


def test_click_resolves_relative_link_and_preserves_preformatted_source(web):
    sandbox, documents, requests = web
    url = "https://example.com/docs/index.html"
    documents[url] = ("text/html", '<html><title>Guide</title><script>not visible</script><pre>def run():\n    return 42\n</pre><a href="../source.py">Read code</a></html>')
    documents["https://example.com/source.py"] = ("text/plain", "print(42)")
    opened = builtins.web_run(sandbox, open=[{"ref_id": url}])
    page = command(opened)
    assert "    return 42" in page["content"]
    assert "not visible" not in page["content"]
    assert page["links"][0]["url"] == "https://example.com/source.py"
    clicked = builtins.web_run(sandbox, click=[{"ref_id": page["ref_id"], "id": page["links"][0]["id"]}], pages=opened.metadata["pages"])
    assert "print(42)" in command(clicked)["content"]
    assert len(requests) == 2


def test_find_accepts_unopened_url(web):
    sandbox, documents, _ = web
    documents["https://example.com/a"] = ("text/plain", "alpha\nbeta\ngamma")
    found = builtins.web_run(sandbox, find=[{"ref_id": "https://example.com/a", "pattern": "beta"}])
    assert command(found)["matches"][0]["lineno"] == 1


def test_github_blob_is_read_as_raw_without_guessing_branch_split(web):
    sandbox, documents, requests = web
    raw = "https://raw.githubusercontent.com/owner/repo/refs/heads/feature/test/src/a.ts"
    documents[raw] = ("text/plain", "export const answer = 42;")
    opened = builtins.web_run(sandbox, open=[{"ref_id": "https://github.com/owner/repo/blob/refs/heads/feature/test/src/a.ts#L1"}])
    assert "export const" in command(opened)["content"]
    assert requests == [raw]


def test_acquisition_truncation_is_not_reported_as_complete(web, monkeypatch):
    sandbox, documents, _ = web
    monkeypatch.setattr(builtins, "MAX_WEB_RESPONSE_BYTES", 1024)
    documents["https://example.com/big"] = ("text/plain", "A" * 2000 + "needle")
    opened = builtins.web_run(sandbox, open=[{"ref_id": "https://example.com/big"}])
    page = command(opened)
    assert page["source_truncated"] is True
    found = builtins.web_run(sandbox, find=[{"ref_id": page["ref_id"], "pattern": "needle"}], pages=opened.metadata["pages"])
    item = json.loads(found.content)[0]
    assert item["error_code"] == "pattern_not_found"
    assert item["source_truncated"] is True


def test_long_single_line_can_continue_without_repeating_prefix(web):
    sandbox, documents, _ = web
    documents["https://example.com/line"] = ("text/plain", "a" * 8000 + "END")
    opened = builtins.web_run(sandbox, open=[{"ref_id": "https://example.com/line", "max_chars": 1000}])
    page = command(opened)
    assert page["next_offset"] == 1000
    continued = builtins.web_run(sandbox, open=[{"ref_id": page["ref_id"], "offset": 8000}], pages=opened.metadata["pages"])
    assert "END" in command(continued)["content"]
    assert command(continued)["next_offset"] is None


def test_model_observation_excludes_full_page_cache(web):
    sandbox, documents, _ = web
    documents["https://example.com/source"] = ("text/plain", "start\n" + "x" * 15000 + "CACHE_ONLY_SUFFIX")
    result = builtins.web_run(sandbox, open=[{"ref_id": "https://example.com/source", "max_chars": 1000}])
    runtime = AgentRuntime(model_call=lambda **kw: None, tool_registry=create_default_registry(str(sandbox.root)))
    message, _ = runtime._prepare_tool_result_message(tool_call_id="one", tool_name="web_run", result=result, artifact_refs=[])
    assert "CACHE_ONLY_SUFFIX" not in message["content"]
    assert "pages" not in json.loads(message["content"])["metadata"]
    assert "CACHE_ONLY_SUFFIX" in json.dumps(result.metadata["pages"])


@pytest.mark.parametrize("arguments,code", [
    ({"open": [{"ref_id": "missing"}]}, "page_not_found"),
    ({"open": [{"ref_id": "https://example.com/a", "lineno": -1}]}, "invalid_arguments"),
    ({"find": [{"ref_id": "missing", "pattern": ""}]}, "invalid_arguments"),
    ({"click": [{"ref_id": "missing", "id": 1}]}, "page_not_found"),
])
def test_navigation_errors_are_explicit(web, arguments, code):
    sandbox, _, requests = web
    result = builtins.web_run(sandbox, **arguments)
    assert not result.ok
    assert json.loads(result.content)[0]["error_code"] == code
    assert requests == []


def test_registry_exposes_usable_source_navigation(web):
    sandbox, documents, _ = web
    documents["https://example.com/a"] = ("text/plain", "first\nsecond\nthird")
    registry = create_default_registry(str(sandbox.root), allowed_tool_names=["web_run"], permission_mode="full")
    result = registry.execute("web_run", {"open": [{"ref_id": "https://example.com/a", "lineno": 1}], "response_length": "short"})
    assert "L1: second" in command(result)["content"]
    assert "L0:" not in command(result)["content"]


def test_parallel_calls_keep_distinct_page_references(web):
    sandbox, documents, _ = web
    documents["https://example.com/a"] = ("text/plain", "document A")
    documents["https://example.com/b"] = ("text/plain", "document B")
    registry = create_default_registry(str(sandbox.root), allowed_tool_names=["web_run"], permission_mode="full")
    runtime = AgentRuntime(model_call=lambda **kw: None, tool_registry=registry)
    async def invoke():
        return await asyncio.gather(*(
            runtime._dispatch_tool("web_run", {"open": [{"ref_id": f"https://example.com/{name}"}]}, approved=True, call_id=name, web_pages={})
            for name in ("a", "b")
        ))
    first, second = asyncio.run(invoke())
    assert command(first)["ref_id"] != command(second)["ref_id"]
    pages = {**first.metadata["pages"], **second.metadata["pages"]}
    for result, expected in ((first, "document A"), (second, "document B")):
        opened = builtins.web_run(sandbox, open=[{"ref_id": command(result)["ref_id"]}], pages=pages)
        assert expected in command(opened)["content"]


def test_search_reference_does_not_reuse_provider_id(web, monkeypatch):
    sandbox, _, _ = web
    monkeypatch.setattr(builtins, "web_search", lambda *a, **kw: builtins.ToolResult(
        "websearch", True, "hit", metadata={"results": [{"ref_id": "search1", "url": "https://example.com/new"}]},
    ))
    result = builtins.web_run(sandbox, search_query=[{"q": "new"}], pages={"search1": {"url": "https://example.com/old"}})
    assert command(result)["results"][0]["ref_id"] == "search2"
    assert result.metadata["pages"]["search1"]["url"] == "https://example.com/old"
