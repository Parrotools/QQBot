"""Fetcher 测试：内容类型白名单、大小上限、redirect 逐跳 SSRF 校验。"""

import httpx
import pytest

from app.security.ssrf import SSRFBlockedError
from app.services.web.fetcher import (
    PageTooLargeError,
    TooManyRedirectsError,
    UnsupportedContentTypeError,
    WebFetcher,
)

HTML = b"<html><title>t</title><body>" + b"x" * 100 + b"</body></html>"


def _resp(status=200, content=b"", headers=None, request=None):
    return httpx.Response(status, content=content, headers=headers or {}, request=request)


async def test_fetch_html_ok():
    def handler(request: httpx.Request) -> httpx.Response:
        return _resp(200, HTML, {"content-type": "text/html; charset=utf-8"}, request)

    f = WebFetcher(transport=httpx.MockTransport(handler))
    result = await f.fetch("https://example.com/page")
    assert result.status_code == 200
    assert "xxxx" in result.text
    assert result.final_url == "https://example.com/page"
    await f.aclose()


async def test_content_type_whitelist():
    def handler(request: httpx.Request) -> httpx.Response:
        return _resp(200, b"binary", {"content-type": "application/octet-stream"}, request)

    f = WebFetcher(transport=httpx.MockTransport(handler))
    with pytest.raises(UnsupportedContentTypeError):
        await f.fetch("https://example.com/file.bin")
    await f.aclose()


async def test_max_bytes_enforced():
    def handler(request: httpx.Request) -> httpx.Response:
        return _resp(200, b"a" * 1000, {"content-type": "text/html"}, request)

    f = WebFetcher(max_bytes=500, transport=httpx.MockTransport(handler))
    with pytest.raises(PageTooLargeError):
        await f.fetch("https://example.com/big")
    await f.aclose()


async def test_redirect_revalidates_ssrf_each_hop(monkeypatch):
    """原始 URL 是公网，重定向跳到内网 —— 第二跳必须被 SSRF 拒绝。"""
    from app.security import ssrf

    resolved_hosts: list[str] = []

    async def fake_resolve(host: str):
        resolved_hosts.append(host)
        if host == "example.com":
            return [__import__("ipaddress").ip_address("93.184.216.34")]
        return [__import__("ipaddress").ip_address("10.0.0.5")]  # 内网

    monkeypatch.setattr(ssrf, "resolve_host", fake_resolve)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "example.com":
            return _resp(302, b"", {"location": "http://internal.example/x"}, request)
        return _resp(200, HTML, {"content-type": "text/html"}, request)

    f = WebFetcher(transport=httpx.MockTransport(handler))
    with pytest.raises(SSRFBlockedError):
        await f.fetch("https://example.com/redirect")
    # 证明第二跳确实做了 DNS 解析 + 校验
    assert "internal.example" in resolved_hosts
    await f.aclose()


async def test_too_many_redirects(monkeypatch):
    import ipaddress

    from app.security import ssrf

    async def fake_resolve(host):
        return [ipaddress.ip_address("93.184.216.34")]

    monkeypatch.setattr(ssrf, "resolve_host", fake_resolve)

    def handler(request: httpx.Request) -> httpx.Response:
        return _resp(302, b"", {"location": "https://example.com/loop"}, request)

    f = WebFetcher(max_redirects=3, transport=httpx.MockTransport(handler))
    with pytest.raises(TooManyRedirectsError):
        await f.fetch("https://example.com/start")
    await f.aclose()
