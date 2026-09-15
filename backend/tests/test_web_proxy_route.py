"""以系统代理、环境代理和真实 peer 元数据覆盖 unsafe_url 的实际失败路径。"""

import socket
from types import SimpleNamespace
import urllib.request

import httpx
import pytest

from src.tools import builtins as b
from src.tools.sandbox import WorkspaceSandbox


@pytest.fixture
def network(monkeypatch, tmp_path):
    for name in list(b.os.environ):
        if name.lower() in {"http_proxy", "https_proxy", "all_proxy", "no_proxy"}:
            monkeypatch.delenv(name)
    # 模拟 Windows 注册表提供代理，不设置任何代理环境变量。
    monkeypatch.setattr(urllib.request, "getproxies", lambda: {"https": "http://127.0.0.1:7897"})
    monkeypatch.setattr(urllib.request, "proxy_bypass", lambda host: False)
    def dns(host, port, **kw):
        ip = host if host in {"127.0.0.1", "10.0.0.1"} else "93.184.216.34"
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port))]
    monkeypatch.setattr(b.socket, "getaddrinfo", dns)
    clients, requests = [], []
    real_client = httpx.Client
    responses = []
    def client_factory(**kw):
        clients.append({k: kw.get(k) for k in ("proxy", "trust_env")})
        # httpx 的显式 proxy mount 优先于 transport；记录选择后移除，禁止测试触网。
        kw.pop("proxy", None)
        def handler(request):
            requests.append(str(request.url))
            status, peer, headers, body = responses.pop(0)
            stream = SimpleNamespace(get_extra_info=lambda name: peer if name == "server_addr" else None)
            return httpx.Response(status, headers=headers, text=body, extensions={"network_stream": stream})
        return real_client(**{**kw, "transport": httpx.MockTransport(handler)})
    monkeypatch.setattr(b.httpx, "Client", client_factory)
    return WorkspaceSandbox(tmp_path), responses, clients, requests


def test_registry_proxy_is_used_and_http_status_not_masked(network):
    sandbox, responses, clients, _ = network
    responses.append((401, ("127.0.0.1", 7897), {"content-type": "text/html"}, "denied"))
    result = b.web_open(sandbox, "https://example.com/news")
    assert result.error_code == "http_error"
    assert result.metadata["status_code"] == 401
    assert result.metadata["stage"] == "http"
    assert result.metadata["proxy_used"] is True
    assert clients == [{"proxy": "http://127.0.0.1:7897", "trust_env": False}]


def test_no_proxy_uses_direct_connection_and_rejects_private_peer(network, monkeypatch):
    sandbox, responses, clients, _ = network
    monkeypatch.setattr(urllib.request, "proxy_bypass", lambda host: host == "example.com")
    responses.append((200, ("10.0.0.1", 443), {"content-type": "text/plain"}, "not public"))
    result = b.web_open(sandbox, "https://example.com/news")
    assert result.error_code == "unsafe_url"
    assert result.metadata["stage"] == "peer_validation"
    assert clients == [{"proxy": None, "trust_env": False}]


def test_proxy_cannot_authorize_unrelated_private_peer(network):
    sandbox, responses, _, _ = network
    responses.append((200, ("10.0.0.1", 7897), {"content-type": "text/plain"}, "not the proxy"))
    result = b.web_open(sandbox, "https://example.com/")
    assert not result.ok and result.error_code == "unsafe_url"


def test_redirect_recomputes_proxy_route(network, monkeypatch):
    sandbox, responses, clients, _ = network
    monkeypatch.setattr(urllib.request, "proxy_bypass", lambda host: host == "direct.example.com")
    responses.extend([
        (302, ("127.0.0.1", 7897), {"location": "https://direct.example.com/news"}, ""),
        (200, ("93.184.216.34", 443), {"content-type": "text/plain"}, "article"),
    ])
    result = b.web_open(sandbox, "https://example.com/news")
    assert result.ok
    assert clients == [{"proxy": "http://127.0.0.1:7897", "trust_env": False}, {"proxy": None, "trust_env": False}]


def test_redirect_to_private_target_is_blocked_before_request(network):
    sandbox, responses, _, requests = network
    responses.append((302, ("127.0.0.1", 7897), {"location": "http://127.0.0.1/secrets"}, ""))
    result = b.web_open(sandbox, "https://example.com/news")
    assert not result.ok
    assert result.metadata["stage"] == "redirect_validation"
    assert requests == ["https://example.com/news"]


def test_scheme_and_no_proxy_are_respected(monkeypatch):
    monkeypatch.setattr(urllib.request, "getproxies", lambda: {"http": "http://proxy:80", "https": "http://secure:80", "all": "http://other:80"})
    monkeypatch.setattr(urllib.request, "proxy_bypass", lambda host: host == "bypass.test")
    assert b._environment_proxy("http://example.com/") == "http://proxy:80"
    assert b._environment_proxy("https://example.com/") == "http://secure:80"
    assert b._environment_proxy("https://bypass.test/") is None


def test_closed_network_stream_does_not_become_false_unsafe_url():
    class ClosedStream:
        def get_extra_info(self, name):
            raise OSError("socket already closed")

    response = httpx.Response(200, extensions={"network_stream": ClosedStream()})
    assert b._response_peer_is_public(response, proxy_url="http://127.0.0.1:7897", proxy_configured=True)
