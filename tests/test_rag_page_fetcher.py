"""页面抓取单测,重点是 SSRF 防护。

这个文件里的用例基本都是"确认某个恶意 URL 被拒绝"。它们比功能用例更重要:
`fetch_page` 的 URL 参数最终来自模型输出,而模型输出受用户输入影响。
一旦放开,用户就能诱导服务器去读云元数据服务的临时凭证或本机 Redis。

所有用例都不打真网:能拒的在 DNS 校验阶段就拒了;需要真发请求的用
httpx 的 MockTransport。
"""

import asyncio

import httpx
import pytest

from app.services.rag import page_fetcher as pf


def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# 第一道防线:协议白名单
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "ftp://example.com/x",
        "gopher://example.com:6379/_INFO",
        "data:text/html,<h1>x</h1>",
        "//example.com/no-scheme",
    ],
)
def test_rejects_non_http_schemes(url: str) -> None:
    """file:// 能直接读本地文件,gopher:// 历史上被用来打 Redis。"""
    with pytest.raises(pf.FetchError):
        _run(pf.fetch_page(url))


# ---------------------------------------------------------------------------
# 第二道防线:目标 IP 校验
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "why"),
    [
        ("http://127.0.0.1:6379/", "回环地址:本机 Redis / Adminer 都在这"),
        ("http://localhost:8000/api/health", "localhost 解析到回环"),
        ("http://169.254.169.254/latest/meta-data/", "云元数据服务,能读实例临时凭证"),
        ("http://10.0.0.5/internal", "私网 A 段"),
        ("http://172.16.3.4/internal", "私网 B 段"),
        ("http://192.168.1.1/admin", "私网 C 段,家用路由器后台"),
        ("http://0.0.0.0/", "未指定地址"),
        ("http://[::1]:6379/", "IPv6 回环"),
        ("http://[fd00::1]/", "IPv6 私网"),
    ],
)
def test_rejects_internal_targets(url: str, why: str) -> None:
    with pytest.raises(pf.FetchError, match="内网或保留地址段"):
        _run(pf.fetch_page(url))


def test_checks_every_resolved_ip(monkeypatch: pytest.MonkeyPatch) -> None:
    """一个域名可以同时解析出公网 IP 和 127.0.0.1。

    只看第一条结果就会放过去——这是真实存在的绕过手法,
    所以校验必须遍历 getaddrinfo 的全部返回。
    """

    def fake_getaddrinfo(host: str, port):
        return [
            (2, 1, 6, "", ("93.184.216.34", 0)),  # 公网,看起来没问题
            (2, 1, 6, "", ("127.0.0.1", 0)),  # 混在后面的回环
        ]

    monkeypatch.setattr(pf.socket, "getaddrinfo", fake_getaddrinfo)

    with pytest.raises(pf.FetchError, match="内网或保留地址段"):
        _run(pf.fetch_page("http://evil.example.com/"))


def test_unresolvable_host_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    import socket as socket_module

    def boom(host: str, port):
        raise socket_module.gaierror("nope")

    monkeypatch.setattr(pf.socket, "getaddrinfo", boom)

    with pytest.raises(pf.FetchError, match="域名无法解析"):
        _run(pf.fetch_page("http://does-not-exist.invalid/"))


# ---------------------------------------------------------------------------
# 第三道防线:重定向逐跳校验
# ---------------------------------------------------------------------------


def _public_dns(monkeypatch: pytest.MonkeyPatch, *, blocked_hosts: set[str]) -> None:
    """让指定域名解析成回环、其余解析成公网,用于构造重定向绕过场景。"""

    def fake_getaddrinfo(host: str, port):
        if host in blocked_hosts:
            return [(2, 1, 6, "", ("127.0.0.1", 0))]
        return [(2, 1, 6, "", ("93.184.216.34", 0))]

    monkeypatch.setattr(pf.socket, "getaddrinfo", fake_getaddrinfo)


def _patch_transport(monkeypatch: pytest.MonkeyPatch, handler) -> None:
    """把 AsyncClient 换成带 MockTransport 的版本,不发真请求。"""
    original = httpx.AsyncClient

    def factory(**kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return original(**kwargs)

    monkeypatch.setattr(pf.httpx, "AsyncClient", factory)


def test_redirect_to_internal_is_blocked(monkeypatch: pytest.MonkeyPatch) -> None:
    """公网域名 302 到 127.0.0.1 是最经典的 SSRF 绕过手法。

    如果用 httpx 的 follow_redirects=True,中间跳转不经过我们的校验,
    就直接打进内网了。所以必须手动逐跳跟随。
    """
    _public_dns(monkeypatch, blocked_hosts={"inner.example.com"})

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            302, headers={"location": "http://inner.example.com/secret"}
        )

    _patch_transport(monkeypatch, handler)

    with pytest.raises(pf.FetchError, match="内网或保留地址段"):
        _run(pf.fetch_page("http://outer.example.com/start"))


def test_redirect_loop_hits_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    _public_dns(monkeypatch, blocked_hosts=set())

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "http://a.example.com/next"})

    _patch_transport(monkeypatch, handler)

    with pytest.raises(pf.FetchError, match="重定向次数超过上限"):
        _run(pf.fetch_page("http://a.example.com/start"))


def test_redirect_without_location_is_reported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _public_dns(monkeypatch, blocked_hosts=set())
    _patch_transport(monkeypatch, lambda request: httpx.Response(301))

    with pytest.raises(pf.FetchError, match="Location"):
        _run(pf.fetch_page("http://a.example.com/"))


def test_relative_redirect_is_resolved_and_followed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """相对跳转要先拼成绝对地址再校验,否则 urlparse 拿不到 host。"""
    _public_dns(monkeypatch, blocked_hosts=set())
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if request.url.path == "/start":
            return httpx.Response(302, headers={"location": "/final"})
        return httpx.Response(
            200,
            headers={"content-type": "text/html; charset=utf-8"},
            text="<p>神庙在这里</p>",
        )

    _patch_transport(monkeypatch, handler)

    assert "神庙在这里" in _run(pf.fetch_page("http://a.example.com/start"))
    assert seen[-1] == "http://a.example.com/final"


# ---------------------------------------------------------------------------
# 第四道防线:内容类型、体积、错误状态
# ---------------------------------------------------------------------------


def test_rejects_non_text_content_type(monkeypatch: pytest.MonkeyPatch) -> None:
    _public_dns(monkeypatch, blocked_hosts=set())
    _patch_transport(
        monkeypatch,
        lambda request: httpx.Response(
            200, headers={"content-type": "application/pdf"}, content=b"%PDF-"
        ),
    )

    with pytest.raises(pf.FetchError, match="不支持的内容类型"):
        _run(pf.fetch_page("http://a.example.com/doc"))


def test_reports_http_error_status(monkeypatch: pytest.MonkeyPatch) -> None:
    _public_dns(monkeypatch, blocked_hosts=set())
    _patch_transport(monkeypatch, lambda request: httpx.Response(404))

    with pytest.raises(pf.FetchError, match="404"):
        _run(pf.fetch_page("http://a.example.com/missing"))


def test_transport_error_does_not_leak_url(monkeypatch: pytest.MonkeyPatch) -> None:
    """httpx 的报错会带上完整 URL,而 query 里可能有敏感参数。"""
    _public_dns(monkeypatch, blocked_hosts=set())

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connect to http://a.example.com/?token=secret failed")

    _patch_transport(monkeypatch, handler)

    with pytest.raises(pf.FetchError) as excinfo:
        _run(pf.fetch_page("http://a.example.com/?token=secret"))

    assert "token=secret" not in str(excinfo.value)


def test_truncates_to_max_chars(monkeypatch: pytest.MonkeyPatch) -> None:
    """不截断的话一个长攻略页就能顶满上下文窗口,把后面的检索结果挤掉。"""
    _public_dns(monkeypatch, blocked_hosts=set())
    _patch_transport(
        monkeypatch,
        lambda request: httpx.Response(
            200,
            headers={"content-type": "text/html"},
            text="<p>" + "神庙" * 5000 + "</p>",
        ),
    )

    text = _run(pf.fetch_page("http://a.example.com/long", max_chars=100))

    assert len(text) == 100


def test_empty_body_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    _public_dns(monkeypatch, blocked_hosts=set())
    _patch_transport(
        monkeypatch,
        lambda request: httpx.Response(
            200, headers={"content-type": "text/html"}, text="<html><body></body></html>"
        ),
    )

    with pytest.raises(pf.FetchError, match="没有可提取的正文"):
        _run(pf.fetch_page("http://a.example.com/blank"))


# ---------------------------------------------------------------------------
# 正文抽取
# ---------------------------------------------------------------------------


def test_extract_text_drops_script_and_style_content() -> None:
    """顺序很重要:先整段删掉 script/style 的内容,再删标签。

    反过来先删标签的话,JS 代码会变成正文里的乱码。
    """
    html = """
    <html><head><style>.a{color:red}</style>
    <script>var token = "secret";</script></head>
    <body><p>神庙在东边</p></body></html>
    """

    text = pf.extract_text(html)

    assert "神庙在东边" in text
    assert "secret" not in text
    assert "color:red" not in text


def test_extract_text_keeps_paragraph_breaks() -> None:
    """块级标签要换成换行,否则段落全黏成一行,模型很难读。"""
    text = pf.extract_text("<p>第一段</p><p>第二段</p><li>要点</li>")

    assert "第一段" in text and "第二段" in text
    assert "\n" in text


def test_extract_text_unescapes_entities() -> None:
    assert "a<b & c" in pf.extract_text("<p>a&lt;b &amp; c</p>")
