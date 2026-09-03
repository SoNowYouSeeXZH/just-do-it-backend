"""检索基建单测:搜索 Provider + 缓存 + 查询归一化。

一条都不打真网。DDG 的实际可达性属于环境问题,不该让 CI 依赖它——
要验证真连通性,跑 `python -m app.services.rag.search_provider` 之类的
手工脚本,而不是写进单测。
"""

import asyncio
from typing import Any

import pytest

from app.core import cache
from app.services.rag import search_provider as sp


@pytest.fixture(autouse=True)
def _reset_provider():
    sp.reset_provider_for_tests()
    yield
    sp.reset_provider_for_tests()


class _FakeDDGS:
    """替身:只实现 text(),记录收到的参数。"""

    instances: list["_FakeDDGS"] = []

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs
        self.calls: list[dict[str, Any]] = []
        _FakeDDGS.instances.append(self)

    def text(self, query: str, **kwargs: Any) -> list[dict[str, Any]]:
        self.calls.append({"query": query, **kwargs})
        return [
            {
                "title": "王国之泪 神庙全解",
                "href": "https://example.com/zelda",
                "body": "152 座神庙位置",
            },
            {"title": "缺少地址的条目", "href": "", "body": "应该被丢掉"},
        ]


def _install_fake_ddgs(monkeypatch: pytest.MonkeyPatch) -> None:
    """DDGProvider 里是延迟 import,所以要在 sys.modules 上装替身。"""
    import sys
    import types

    _FakeDDGS.instances = []
    module = types.ModuleType("ddgs")
    module.DDGS = _FakeDDGS  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "ddgs", module)


def test_search_maps_hits_and_drops_urlless(monkeypatch: pytest.MonkeyPatch) -> None:
    """没有 url 的结果必须丢掉:既抓不到正文,也没法作为来源引用。"""
    _install_fake_ddgs(monkeypatch)

    hits = asyncio.run(sp.DDGProvider().search("塞尔达 神庙", 5))

    assert len(hits) == 1
    assert hits[0].url == "https://example.com/zelda"
    assert hits[0].title == "王国之泪 神庙全解"
    assert _FakeDDGS.instances[0].calls[0]["max_results"] == 5


def test_search_passes_region(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_ddgs(monkeypatch)

    asyncio.run(sp.DDGProvider(region="jp-jp").search("ゼルダ", 3))

    assert _FakeDDGS.instances[0].calls[0]["region"] == "jp-jp"


def test_search_wraps_provider_exception(monkeypatch: pytest.MonkeyPatch) -> None:
    """三方库异常要收口成 SearchError,且不把原始文本外传。

    抓取型库的报错里可能带完整请求 URL 和内部堆栈,直接交给模型
    就等于把它写进了上下文,再被模型复述给用户。
    """
    import sys
    import types

    class _Boom:
        def __init__(self, **kwargs: Any) -> None:
            pass

        def text(self, *args: Any, **kwargs: Any) -> Any:
            raise ValueError("https://internal.example/secret?token=abc 连接失败")

    module = types.ModuleType("ddgs")
    module.DDGS = _Boom  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "ddgs", module)

    with pytest.raises(sp.SearchError) as excinfo:
        asyncio.run(sp.DDGProvider().search("x", 3))

    assert "token=abc" not in str(excinfo.value)
    assert "ValueError" in str(excinfo.value)


def test_search_error_is_not_app_error() -> None:
    """SearchError 不能是 AppError——否则会被全局处理器变成 5xx,
    而设计上搜索失败应该降级成一条 observation 交回模型。"""
    from app.core.exceptions import AppError

    assert not issubclass(sp.SearchError, AppError)


def test_unknown_provider_raises() -> None:
    """配错 Provider 要明确报错,不能静默回退到 DDG——
    那样会让人以为换 Provider 生效了,实际还在打 DDG。"""
    from app.config import settings

    original = settings.search_provider
    settings.search_provider = "tavily"
    try:
        with pytest.raises(sp.SearchError, match="未知的 search_provider"):
            sp.get_provider()
    finally:
        settings.search_provider = original


@pytest.mark.parametrize(
    ("left", "right", "same"),
    [
        ("  塞尔达  神庙 ", "塞尔达 神庙", True),
        ("Zelda Shrine", "zelda shrine", True),
        ("塞尔达\t神庙", "塞尔达 神庙", True),
        ("塞尔达 神庙", "塞尔达 支线", False),
    ],
)
def test_query_fingerprint_normalization(left: str, right: str, same: bool) -> None:
    """归一化只做去空白和转小写。

    不做去标点/分词这类激进处理:归一化过度会让本该不同的查询撞进
    同一个缓存,那是答错,比缓存没命中严重得多。
    """
    assert (sp.query_fingerprint(left) == sp.query_fingerprint(right)) is same


def test_search_guides_uses_cache(
    monkeypatch: pytest.MonkeyPatch, cache_client
) -> None:
    """同一查询第二次应命中缓存,不再打外部搜索。"""
    calls: list[str] = []

    class _CountingProvider:
        async def search(self, query: str, max_results: int) -> list[sp.SearchHit]:
            calls.append(query)
            return [sp.SearchHit(title="t", url="https://e.com/a", snippet="s")]

    monkeypatch.setattr(sp, "get_provider", lambda: _CountingProvider())

    first = asyncio.run(sp.search_guides("塞尔达 神庙", 3))
    second = asyncio.run(sp.search_guides(" 塞尔达   神庙 ", 3))

    assert len(calls) == 1, "归一化后相同的查询应该只打一次外部搜索"
    assert [h.url for h in first] == [h.url for h in second]
    assert first[0].title == "t"


def test_search_guides_cache_key_includes_limit(
    monkeypatch: pytest.MonkeyPatch, cache_client
) -> None:
    """要 3 条和要 10 条不能共用缓存,否则第二次只能拿到 3 条。"""
    calls: list[int] = []

    class _CountingProvider:
        async def search(self, query: str, max_results: int) -> list[sp.SearchHit]:
            calls.append(max_results)
            return [sp.SearchHit(title="t", url="https://e.com/a", snippet="s")]

    monkeypatch.setattr(sp, "get_provider", lambda: _CountingProvider())

    asyncio.run(sp.search_guides("同一个问题", 3))
    asyncio.run(sp.search_guides("同一个问题", 10))

    assert calls == [3, 10]


def test_search_guides_works_without_redis(monkeypatch: pytest.MonkeyPatch) -> None:
    """Redis 不可用时只是每次都真查,功能不能因此挂掉(fail-open)。"""
    monkeypatch.setattr(cache, "get_client", lambda: None)
    calls: list[str] = []

    class _CountingProvider:
        async def search(self, query: str, max_results: int) -> list[sp.SearchHit]:
            calls.append(query)
            return [sp.SearchHit(title="t", url="https://e.com/a", snippet="s")]

    monkeypatch.setattr(sp, "get_provider", lambda: _CountingProvider())

    asyncio.run(sp.search_guides("问题", 3))
    asyncio.run(sp.search_guides("问题", 3))

    assert len(calls) == 2, "没有缓存时应该每次都真查,而不是报错"
