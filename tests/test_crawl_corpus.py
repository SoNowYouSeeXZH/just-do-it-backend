"""爬虫脚本的单元测试:不真连 wiki,用假 client 驱动编排逻辑。

IO 外壳(真的抓取)靠人工冒烟验证;这里测的是:
continuation 拼接、max_pages 截断、失败页跳过、嵌入失败不中断、入库覆盖。
"""

import asyncio

import pytest
from sqlmodel import Session, select

from app.crawl_corpus import (
    collect_titles_by_category,
    crawl_site,
    fetch_wikitext,
    list_page_titles,
)
from app.models.guide_chunk import GuideChunk
from app.repositories import guide_chunk as chunk_repo


class FakeResponse:
    def __init__(self, payload: dict | None = None, status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code

    def json(self) -> dict:
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


class FakeClient:
    """按请求参数分发的假 httpx 客户端,记录请求次数供节流断言。"""

    def __init__(self, responses: list[FakeResponse]) -> None:
        self._responses = list(responses)
        self.calls: list[dict] = []

    async def get(self, url: str, params: dict) -> FakeResponse:
        self.calls.append(dict(params))
        return self._responses.pop(0)


def _allpages_response(titles: list[str], continue_from: str | None) -> dict:
    payload: dict = {
        "query": {"allpages": [{"title": title, "ns": 0} for title in titles]}
    }
    if continue_from:
        payload["continue"] = {"apcontinue": continue_from, "continue": "continue"}
    return payload


def _no_throttle(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _instant() -> None:
        return None

    monkeypatch.setattr("app.services.rag.wiki_provider._throttle", _instant)


def test_list_page_titles_follows_continuation_and_caps_max(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _no_throttle(monkeypatch)
    client = FakeClient(
        [
            FakeResponse(_allpages_response(["A", "B"], "C")),
            FakeResponse(_allpages_response(["C", "D"], "E")),
            FakeResponse(_allpages_response(["E", "F"], None)),
        ]
    )

    titles = asyncio.run(list_page_titles(client, "https://x/api.php", max_pages=5))

    assert titles == ["A", "B", "C", "D", "E"]  # 5 为上限,E、F 那批只取到上限
    # 第二次请求必须带上 continuation 的 apcontinue
    assert client.calls[1]["apcontinue"] == "C"


def test_fetch_wikitext_returns_none_for_missing_page(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _no_throttle(monkeypatch)
    client = FakeClient([FakeResponse({})])

    assert asyncio.run(fetch_wikitext(client, "https://x/api.php", "不存在")) is None


def _parse_payload(wikitext: str) -> dict:
    return {"parse": {"title": "t", "wikitext": wikitext}}


_WIKITEXT = (
    "== 配队推荐 ==\n" + "使用火系主C与后台水系角色打蒸发反应,输出非常稳定。" * 20
)


def test_crawl_site_writes_chunks_without_embedding(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    _no_throttle(monkeypatch)
    client = FakeClient(
        [
            FakeResponse(_allpages_response(["刻晴"], None)),
            FakeResponse(_parse_payload(_WIKITEXT)),
        ]
    )

    pages, chunks = asyncio.run(
        crawl_site(
            client, session, slug="ys", max_pages=5, do_embed=False, strategy="allpages"
        )
    )

    assert (pages, chunks) == (1, 1)
    stored = session.exec(select(GuideChunk)).all()
    assert len(stored) == 1
    assert stored[0].game_slug == "ys"
    assert stored[0].embedding is None  # --no-embed: 嵌入列留空
    assert stored[0].source_url.endswith("/ys/%E5%88%BB%E6%99%B4")  # 标题被 URL 编码


def test_crawl_site_replaces_old_chunks_on_recrawl(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    _no_throttle(monkeypatch)
    client = FakeClient(
        [
            FakeResponse(_allpages_response(["刻晴"], None)),
            FakeResponse(_parse_payload(_WIKITEXT)),
            FakeResponse(_allpages_response(["刻晴"], None)),
            FakeResponse(_parse_payload(_WIKITEXT + "追加内容")),
        ]
    )

    asyncio.run(crawl_site(
            client, session, slug="ys", max_pages=5, do_embed=False, strategy="allpages"
        ))
    asyncio.run(crawl_site(
            client, session, slug="ys", max_pages=5, do_embed=False, strategy="allpages"
        ))

    stored = session.exec(select(GuideChunk)).all()
    assert len(stored) == 1  # 整页覆盖,不累积
    assert "追加内容" in stored[0].chunk_text  # 留下的是第二次爬的新内容


def test_crawl_site_embedding_failure_skips_page_not_batch(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    _no_throttle(monkeypatch)

    async def _failing(texts: list[str]) -> list[list[float]]:
        raise RuntimeError("429 余额不足")

    monkeypatch.setattr("app.crawl_corpus.embedding.embed_texts", _failing)
    client = FakeClient(
        [
            FakeResponse(_allpages_response(["刻晴", "钟离"], None)),
            FakeResponse(_parse_payload(_WIKITEXT)),
            FakeResponse(_parse_payload(_WIKITEXT)),
        ]
    )

    pages, chunks = asyncio.run(
        crawl_site(
            client, session, slug="ys", max_pages=5, do_embed=True, strategy="allpages"
        )
    )

    # 两个页的嵌入都失败:0 页入库,但流程没有抛异常中断
    assert (pages, chunks) == (0, 0)


# ===== 分类采集策略 =====


def _category_payload(titles: list[str], continue_from: str | None = None) -> dict:
    payload: dict = {
        "query": {"categorymembers": [{"title": t, "ns": 0} for t in titles]}
    }
    if continue_from:
        payload["continue"] = {"cmcontinue": continue_from, "continue": "continue"}
    return payload


def test_collect_titles_by_category_dedups_and_respects_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """同一页可能同时属于多个分类;预算用完必须停,靠前的分类优先。"""
    _no_throttle(monkeypatch)
    client = FakeClient(
        [
            FakeResponse(_category_payload(["刻晴", "钟离"])),  # 角色
            FakeResponse(_category_payload(["钟离", "护摩之杖"])),  # 武器,钟离重复
            FakeResponse(_category_payload(["超载反应"])),  # 机制攻略
        ]
    )

    titles = asyncio.run(
        collect_titles_by_category(client, "https://x/api.php", max_pages=4)
    )

    assert titles == ["刻晴", "钟离", "护摩之杖", "超载反应"]  # 去重且保序


def test_collect_titles_skips_missing_category(monkeypatch: pytest.MonkeyPatch) -> None:
    """各站分类命名不一致,取不到某个分类不该让整站采集失败。"""
    _no_throttle(monkeypatch)
    client = FakeClient(
        [
            FakeResponse(None, status_code=404),  # 第一个分类失败(404 不重试)
            FakeResponse(_category_payload(["护摩之杖", "天空之刃"])),  # 第二个补满预算
        ]
    )

    titles = asyncio.run(
        collect_titles_by_category(client, "https://x/api.php", max_pages=2)
    )

    assert titles == ["护摩之杖", "天空之刃"]


def test_fetch_wikitext_skips_maintenance_pages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """「待完善」「施工中」页面的正文多是模板占位,嵌进语料只占 top_k 名额。"""
    _no_throttle(monkeypatch)
    client = FakeClient(
        [
            FakeResponse(
                {
                    "parse": {
                        "wikitext": _WIKITEXT,
                        "categories": [{"category": "待完善"}],
                    }
                }
            )
        ]
    )

    assert asyncio.run(fetch_wikitext(client, "https://x/api.php", "半成品")) is None


def test_fetch_wikitext_keeps_normal_pages(monkeypatch: pytest.MonkeyPatch) -> None:
    _no_throttle(monkeypatch)
    client = FakeClient(
        [
            FakeResponse(
                {"parse": {"wikitext": _WIKITEXT, "categories": [{"category": "角色"}]}}
            )
        ]
    )

    assert asyncio.run(fetch_wikitext(client, "https://x/api.php", "刻晴")) == _WIKITEXT
