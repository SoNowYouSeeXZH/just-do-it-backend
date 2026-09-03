"""游戏 wiki 检索 Provider(默认实现)。

## 为什么默认不是通用网页搜索

设计上一期打算用 DDG(`ddgs` 包)做通用搜索,免注册、无 key。实测结论是它
在当前网络下**不可用**:头一两次请求能成功(1.7~6.7s),之后被限流并且不恢复,
连续 5 次只成功 2 次;把多个后端写在一个列表里反而更糟——`ddgs` 内部用
`return_when=FIRST_EXCEPTION` 并发批量跑,一个秒失败的后端会把另一个
正在返回的请求一起带走。

所以默认换成 **MediaWiki API**:游戏攻略天然沉淀在 wiki 上,而 wiki 提供的是
正规 API 而不是被爬的网页——结构化、有稳定的页面 URL(利于引用溯源)、
不需要注册。对"游戏攻略问答"这个场景,它比通用搜索更对症。

DDG 实现保留在 `search_provider.py` 里,通过 `settings.search_provider` 切换。
这正是当初抽 `SearchProvider` Protocol 的用处——换数据源不动 agent 和 tools。

## 已知限制(不藏着)

- 站点是白名单式的:一个 wiki 对应一个游戏,问到没配的游戏就检索不到。
  这是刻意的取舍——比"什么都搜但一半时间失败"更可预期。
- biligame 有 WAF,请求密了会返回 **567**。所以有退避重试,且检索结果
  必须走 Redis 缓存(见 search_provider.search_guides)。
"""

from __future__ import annotations

import asyncio
import logging
import re
from urllib.parse import quote

import httpx

from app.config import settings
from app.services.rag.search_provider import SearchError, SearchHit

logger = logging.getLogger(__name__)

# MediaWiki 的 search snippet 会用 <span class="searchmatch"> 高亮命中词,
# 直接喂给模型会混进标签噪声。
_TAG = re.compile(r"<[^>]+>")

# WAF / 限流的状态码。567 是 biligame 自定义的拦截码,429 是标准限流。
_RETRYABLE_STATUS = {429, 567, 503}


def _strip_tags(text: str) -> str:
    import html as html_module

    return html_module.unescape(_TAG.sub("", text)).strip()


class MediaWikiProvider:
    """按站点白名单检索 MediaWiki 站群。

    默认指向 biligame 游戏 wiki(`https://wiki.biligame.com/<slug>/api.php`),
    但 base_url 可配——任何 MediaWiki 站点的 api.php 都是同一套接口。
    """

    def __init__(
        self,
        *,
        base_url: str | None = None,
        sites: str | None = None,
    ) -> None:
        self._base = (base_url or settings.wiki_base_url).rstrip("/")
        raw = sites if sites is not None else settings.wiki_sites
        self._sites = [s.strip() for s in raw.split(",") if s.strip()]

    async def search(self, query: str, max_results: int) -> list[SearchHit]:
        if not self._sites:
            raise SearchError("未配置任何 wiki 站点(wiki_sites 为空)")

        hits: list[SearchHit] = []
        errors: list[str] = []

        async with httpx.AsyncClient(
            timeout=settings.wiki_request_timeout_seconds,
            headers={"User-Agent": "JustDoItBot/1.0 (+game guide assistant)"},
            follow_redirects=True,
        ) as client:
            # 逐站点顺序查而不是并发:并发对被 WAF 保护的站点等于自己制造
            # 一次小型压测,更容易触发拦截。攻略问答对 1~2 秒的差别不敏感。
            for slug in self._sites:
                if len(hits) >= max_results:
                    break
                try:
                    hits.extend(
                        await self._search_site(
                            client, slug, query, max_results - len(hits)
                        )
                    )
                except SearchError as exc:
                    # 单站失败不该让整次检索失败——另一个站可能就有答案。
                    errors.append(f"{slug}:{exc}")
                    logger.info("wiki 站点检索失败 slug=%s err=%s", slug, exc)

        if not hits and errors:
            # 全都失败才算失败,并把各站原因合并上报(交给模型当 observation)。
            raise SearchError("所有 wiki 站点检索失败(" + "; ".join(errors) + ")")

        logger.info("wiki 检索完成 query=%s hits=%d", query, len(hits))
        return hits[:max_results]

    async def _search_site(
        self,
        client: httpx.AsyncClient,
        slug: str,
        query: str,
        limit: int,
    ) -> list[SearchHit]:
        params = {
            "action": "query",
            "list": "search",
            "srsearch": query,
            "srlimit": max(1, min(limit, settings.wiki_search_limit_per_site)),
            "srprop": "snippet",
            "format": "json",
            "formatversion": "2",
        }
        payload = await self._get_json(client, f"{self._base}/{slug}/api.php", params)

        results = payload.get("query", {}).get("search", [])
        out: list[SearchHit] = []
        for item in results:
            title = (item.get("title") or "").strip()
            if not title:
                continue
            out.append(
                SearchHit(
                    title=f"{title}（{slug} wiki）",
                    # 页面 URL 要能直接点开,所以用 /wiki 风格的固定链接而不是
                    # api.php 查询串。quote 是必须的:标题里有中文和空格。
                    url=f"{self._base}/{slug}/{quote(title)}",
                    snippet=_strip_tags(item.get("snippet") or ""),
                )
            )
        return out

    async def _get_json(
        self, client: httpx.AsyncClient, url: str, params: dict[str, object]
    ) -> dict:
        """带一次退避重试的 GET。

        只对限流/WAF 类状态码重试:4xx 里的其他码(参数错、页面不存在)
        重试也不会变好,重试只会白等。
        """
        delay = settings.wiki_retry_delay_seconds
        last: str = ""

        for attempt in range(2):
            try:
                response = await client.get(url, params=params)
            except httpx.HTTPError as exc:
                last = type(exc).__name__
            else:
                if response.status_code == 200:
                    try:
                        return response.json()
                    except ValueError as exc:
                        # 被 WAF 拦截时常常返回 200 + HTML 页面,不是 JSON。
                        raise SearchError("wiki 返回的不是 JSON(可能被拦截)") from exc
                last = f"HTTP {response.status_code}"
                if response.status_code not in _RETRYABLE_STATUS:
                    break

            if attempt == 0:
                await asyncio.sleep(delay)

        raise SearchError(last or "请求失败")
