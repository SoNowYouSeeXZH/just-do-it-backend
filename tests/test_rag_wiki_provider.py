"""wiki Provider 单测。不打真网,用 httpx.MockTransport 构造响应。

真站可达性属于环境问题,不该让 CI 依赖它——biligame 有 WAF,
把真请求写进单测等于让 CI 时不时红一次,而且红的原因和代码无关。
"""

import asyncio

import httpx
import pytest

from app.services.rag import wiki_provider as wp
from app.services.rag.search_provider import SearchError


def _patch_transport(monkeypatch: pytest.MonkeyPatch, handler) -> None:
    original = httpx.AsyncClient

    def factory(**kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return original(**kwargs)

    monkeypatch.setattr(wp.httpx, "AsyncClient", factory)


@pytest.fixture(autouse=True)
def _no_throttle(monkeypatch: pytest.MonkeyPatch):
    """默认关掉节流,否则每个用例都要白等 1.5 秒。

    节流本身由 test_throttle_* 专门验证。
    """
    monkeypatch.setattr(wp.settings, "wiki_min_interval_seconds", 0.0)
    monkeypatch.setattr(wp.settings, "wiki_retry_delay_seconds", 0.0)
    wp.reset_throttle_for_tests()
    yield
    wp.reset_throttle_for_tests()


def _search_payload(titles: list[str]) -> dict:
    return {
        "query": {
            "search": [
                {"title": t, "snippet": f'包含 <span class="searchmatch">关键词</span> 的 {t}'}
                for t in titles
            ]
        }
    }


def test_search_maps_results_with_clickable_urls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """URL 必须是能直接点开的页面地址,不是 api.php 查询串——
    它要作为引用来源展示给用户。"""
    _patch_transport(
        monkeypatch,
        lambda request: httpx.Response(200, json=_search_payload(["纳塔"])),
    )

    hits = asyncio.run(
        wp.MediaWikiProvider(base_url="https://wiki.example.com", sites="ys").search(
            "纳塔", 3
        )
    )

    assert len(hits) == 1
    # 中文标题必须 percent-encode,否则拼出来的 URL 不合法
    assert hits[0].url == "https://wiki.example.com/ys/%E7%BA%B3%E5%A1%94"
    assert "ys wiki" in hits[0].title


def test_snippet_tags_are_stripped(monkeypatch: pytest.MonkeyPatch) -> None:
    """MediaWiki 的 snippet 带 <span class="searchmatch"> 高亮标签,
    直接喂给模型会混进标签噪声。"""
    _patch_transport(
        monkeypatch,
        lambda request: httpx.Response(200, json=_search_payload(["纳塔"])),
    )

    hits = asyncio.run(
        wp.MediaWikiProvider(base_url="https://w.example.com", sites="ys").search("x", 3)
    )

    assert "<span" not in hits[0].snippet
    assert "关键词" in hits[0].snippet


def test_stops_early_once_enough_hits(monkeypatch: pytest.MonkeyPatch) -> None:
    """够数就不再查后面的站点:每多打一个站就多一次被 WAF 拦的机会。"""
    visited: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        visited.append(request.url.path)
        return httpx.Response(200, json=_search_payload(["A", "B"]))

    _patch_transport(monkeypatch, handler)

    hits = asyncio.run(
        wp.MediaWikiProvider(base_url="https://w.example.com", sites="ys,sr,zzz").search(
            "x", 2
        )
    )

    assert len(hits) == 2
    assert len(visited) == 1, "第一个站就够了,不该继续查"


def test_single_site_failure_does_not_fail_whole_search(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """一个站挂了,另一个站可能就有答案,不该整次失败。"""

    def handler(request: httpx.Request) -> httpx.Response:
        if "/ys/" in str(request.url):
            return httpx.Response(500)
        return httpx.Response(200, json=_search_payload(["星穹页面"]))

    _patch_transport(monkeypatch, handler)

    hits = asyncio.run(
        wp.MediaWikiProvider(base_url="https://w.example.com", sites="ys,sr").search(
            "x", 3
        )
    )

    assert len(hits) == 1
    assert "星穹页面" in hits[0].title


def test_all_sites_failing_raises_with_reasons(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """全失败才算失败,并且要带上各站原因——这条消息会作为 observation
    交给模型,让它知道是检索不到而不是没有这个内容。"""
    _patch_transport(monkeypatch, lambda request: httpx.Response(500))

    with pytest.raises(SearchError) as excinfo:
        asyncio.run(
            wp.MediaWikiProvider(base_url="https://w.example.com", sites="ys,sr").search(
                "x", 3
            )
        )

    message = str(excinfo.value)
    assert "ys" in message and "sr" in message


@pytest.mark.parametrize("status", [429, 567, 503])
def test_retries_on_rate_limit_statuses(
    monkeypatch: pytest.MonkeyPatch, status: int
) -> None:
    """567 是 biligame 的 WAF 拦截码,429/503 是标准限流,这些值得重试一次。"""
    monkeypatch.setattr(wp.settings, "wiki_retry_delay_seconds", 0.0)
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(status)
        return httpx.Response(200, json=_search_payload(["恢复了"]))

    _patch_transport(monkeypatch, handler)

    hits = asyncio.run(
        wp.MediaWikiProvider(base_url="https://w.example.com", sites="ys").search("x", 3)
    )

    assert calls["n"] == 2
    assert "恢复了" in hits[0].title


def test_does_not_retry_client_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    """404 / 400 重试也不会变好,重试只会让用户白等。"""
    monkeypatch.setattr(wp.settings, "wiki_retry_delay_seconds", 0.0)
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(404)

    _patch_transport(monkeypatch, handler)

    with pytest.raises(SearchError):
        asyncio.run(
            wp.MediaWikiProvider(base_url="https://w.example.com", sites="ys").search(
                "x", 3
            )
        )

    assert calls["n"] == 1, "客户端错误不该重试"


def test_html_instead_of_json_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    """被 WAF 拦截时常常是 200 + HTML 页面,不是 JSON。

    如果不处理,json() 会抛 ValueError 冒到上层变成 500,
    而这本该是一次"检索失败"的降级。
    """
    _patch_transport(
        monkeypatch,
        lambda request: httpx.Response(
            200, text="<html><body>访问被拦截</body></html>"
        ),
    )

    with pytest.raises(SearchError, match="不是 JSON"):
        asyncio.run(
            wp.MediaWikiProvider(base_url="https://w.example.com", sites="ys").search(
                "x", 3
            )
        )


def test_empty_site_list_is_rejected() -> None:
    with pytest.raises(SearchError, match="未配置任何 wiki 站点"):
        asyncio.run(
            wp.MediaWikiProvider(base_url="https://w.example.com", sites=" , ").search(
                "x", 3
            )
        )


def test_provider_factory_returns_wiki_by_default() -> None:
    """默认必须是 wiki:DDG 实测不可靠,不能当主路径。"""
    from app.config import settings
    from app.services.rag import search_provider as sp

    assert settings.search_provider == "wiki"
    sp.reset_provider_for_tests()
    try:
        assert isinstance(sp.get_provider(), wp.MediaWikiProvider)
    finally:
        sp.reset_provider_for_tests()


# ---------------------------------------------------------------------------
# 节流
# ---------------------------------------------------------------------------


def test_throttle_spaces_out_consecutive_requests(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """实测背靠背两次请求第二次必被 WAF 拦,所以必须强制留间隔。

    这里不真等 1.5 秒,而是把 sleep 换成记录器——断言"该等多久",
    比断言墙上时钟更快也更稳。
    """
    monkeypatch.setattr(wp.settings, "wiki_min_interval_seconds", 1.5)
    slept: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        slept.append(seconds)
        # 让单调时钟"前进"这么多,模拟真的等过了
        base = wp.time.monotonic()
        monkeypatch.setattr(wp.time, "monotonic", lambda: base + seconds)

    monkeypatch.setattr(wp.asyncio, "sleep", fake_sleep)
    _patch_transport(
        monkeypatch,
        lambda request: httpx.Response(200, json=_search_payload(["A"])),
    )

    async def two_searches() -> None:
        provider = wp.MediaWikiProvider(base_url="https://w.example.com", sites="ys")
        await provider.search("x", 1)
        await provider.search("y", 1)

    asyncio.run(two_searches())

    assert slept, "第二次请求必须先等待,否则会被 WAF 拦"
    assert slept[-1] <= 1.5


def test_throttle_can_be_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """间隔配 0 时不该有任何等待——本地调试和单测都需要这个逃生口。"""
    monkeypatch.setattr(wp.settings, "wiki_min_interval_seconds", 0.0)
    slept: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        slept.append(seconds)

    monkeypatch.setattr(wp.asyncio, "sleep", fake_sleep)
    _patch_transport(
        monkeypatch,
        lambda request: httpx.Response(200, json=_search_payload(["A"])),
    )

    asyncio.run(
        wp.MediaWikiProvider(base_url="https://w.example.com", sites="ys").search("x", 1)
    )

    assert slept == []
