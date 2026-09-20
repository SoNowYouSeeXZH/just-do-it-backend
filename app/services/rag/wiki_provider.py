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

- 游戏子站是白名单式的:一个 wiki 对应一个游戏。没配的游戏走不到子站,
  兜底由 search_provider 的 `_fallback_search` 处理:先查中心站(见下),
  再退到 DDG(国内网络不可达,海外部署时自动生效)。
- 中心站(`wiki.biligame.com/wiki`,slug 固定为 `wiki`)收录各游戏的
  **门户页**(如「塞尔达传说：织梦岛WIKI」)。门户页正文里的「进入WIKI」
  链接是普通 <a>,可以解析出真实子站 slug(如 dream),所以未预置的游戏
  也能搜到**真攻略页**;解析失败时降级为门户入口页本身。
- 中心站直接全文搜索会命中大量目录垃圾页(首页/New 等页面正文里列着
  全站游戏名),必须用 `intitle:` 限定标题匹配,并对纯数字查询诚实返回空。
- biligame 有 WAF,请求密了会返回 **567**。所以有退避重试,且检索结果
  必须走 Redis 缓存(见 search_provider.search_guides)。
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
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

# biligame 跨游戏中心站的 slug(固定):收录全站游戏的门户页,
# 是「没预置的游戏」检索兜底的入口。见 search_center。
_CENTER_SITE_SLUG = "wiki"

# 中心站自身的保留路径:解析门户页「进入WIKI」链接时要排除,
# 否则可能把导航链接误判成游戏子站。
_CENTER_RESERVED_SLUGS = {"wiki", "tools", "api"}


def _strip_tags(text: str) -> str:
    import html as html_module

    return html_module.unescape(_TAG.sub("", text)).strip()


# 上一次对 wiki 发请求的时刻(单调时钟)。用来做最小间隔节流。
#
# 为什么需要:实测背靠背两次请求,第二次必定被 WAF 拦(567)。而一次攻略问答
# 在 Agent 循环里可能连着发 4~5 个请求(搜索 + 抓页),不节流就必然被拦。
# 用 monotonic 而不是 time.time():后者会被系统时间调整影响,可能算出负间隔。
#
# 局限说清楚:这是**单进程内**的节流。多 worker 部署时各进程各算一份,
# 实际速率是 worker 数的倍数。真要严格限速得把状态放 Redis,
# 当前单实例部署下不值得,但上多副本前必须回来处理。
_last_request_at: float = 0.0
_throttle_lock: asyncio.Lock | None = None


def _get_lock() -> asyncio.Lock:
    # 惰性创建:Lock 会绑定创建时所在的事件循环,模块导入期还没有循环。
    global _throttle_lock
    if _throttle_lock is None:
        _throttle_lock = asyncio.Lock()
    return _throttle_lock


async def _throttle() -> None:
    """确保两次 wiki 请求之间至少隔 wiki_min_interval_seconds。"""
    global _last_request_at

    interval = settings.wiki_min_interval_seconds
    if interval <= 0:
        return

    # 加锁是必须的:并发请求同时读到同一个 _last_request_at,
    # 各自算出"不用等",然后一起发出去——节流就白做了。
    async with _get_lock():
        elapsed = time.monotonic() - _last_request_at
        if elapsed < interval:
            await asyncio.sleep(interval - elapsed)
        _last_request_at = time.monotonic()


def reset_throttle_for_tests() -> None:
    global _last_request_at, _throttle_lock
    _last_request_at = 0.0
    _throttle_lock = None


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
                try:
                    site_hits = await self._search_site(client, slug, query, max_results)
                except SearchError as exc:
                    # 单站失败不该让整次检索失败——另一个站可能就有答案。
                    errors.append(f"{slug}:{exc}")
                    logger.info("wiki 站点检索失败 slug=%s err=%s", slug, exc)
                    continue

                if site_hits:
                    # **命中即停,不跨站凑数。** 实测过一个反面案例:问原神的问题,
                    # ys 站只返回 2 条,于是继续查 sr(崩坏星穹铁道)把结果补到 5 条,
                    # 结果来源列表里混进了另一个游戏的页面——凑够条数反而污染了答案依据。
                    # 少几条同游戏的结果,好过多几条别的游戏的结果。
                    hits = site_hits
                    break

        if not hits and errors:
            # 全都失败才算失败,并把各站原因合并上报(交给模型当 observation)。
            raise SearchError("所有 wiki 站点检索失败(" + "; ".join(errors) + ")")

        logger.info("wiki 检索完成 query=%s hits=%d", query, len(hits))
        return hits[:max_results]

    async def search_center(self, query: str, max_results: int) -> list[SearchHit]:
        """跨游戏检索:没预置的游戏走这里。

        三层策略(2026-09-20 重做,旧版直接搜中心站会命中大量目录垃圾页——
        中心站的「首页/New」等页面的正文里列着全站几百个游戏名,模糊搜索
        谁都匹配得上,搜「2077」出来的是「首页(全站目录)」这种牛头不对马嘴):

        1. `intitle:` 限定标题匹配——游戏门户页的标题就是「XXXWIKI」,
           标题匹配基本只命中正确的游戏入口(实测 intitle:赛博朋克 → 赛博朋克2077WIKI)。
        2. 命中门户页后,从页面里的「进入WIKI」链接解析出真实子站 slug
           (如织梦岛门户 → wiki.biligame.com/dream),再用原始关键词搜该子站,
           拿到的是真正的攻略页而不是门户目录。
        3. 任何一步失败都降级:第 2 步失败退回门户页本身,第 1 步就失败返回空。

        纯数字/过短的查询(如「2077」)在标题索引里可能搜不到(分词器忽略纯数字),
        此时诚实返回空——用户换个更完整的游戏名就能命中,好过返回垃圾结果。
        """
        async with httpx.AsyncClient(
            timeout=settings.wiki_request_timeout_seconds,
            headers={"User-Agent": "JustDoItBot/1.0 (+game guide assistant)"},
            follow_redirects=True,
        ) as client:
            try:
                portals = await self._search_site(
                    client, _CENTER_SITE_SLUG, f"intitle:{query}", max_results,
                    site_label="B站游戏Wiki",
                )
            except SearchError as exc:
                logger.info("中心站 intitle 检索失败 query=%s err=%s", query, exc)
                return []

            if not portals:
                return []

            # 门户页只是入口,尝试解析真实子站并搜出真攻略
            try:
                slug = await self._resolve_wiki_slug(client, portals[0].url)
            except SearchError as exc:
                logger.info("门户页子站解析失败 url=%s err=%s", portals[0].url, exc)
                slug = None

            if slug:
                try:
                    real = await self._search_site(
                        client, slug, query, max_results,
                        site_label=f"{slug} wiki",
                    )
                except SearchError as exc:
                    logger.info("子站攻略检索失败 slug=%s err=%s", slug, exc)
                    real = []
                if real:
                    logger.info(
                        "中心站命中并解析子站 query=%s slug=%s hits=%d", query, slug, len(real)
                    )
                    return real[:max_results]

        logger.info("中心站检索完成(仅门户入口) query=%s hits=%d", query, len(portals))
        return portals[:max_results]

    async def _resolve_wiki_slug(self, client: httpx.AsyncClient, portal_url: str) -> str | None:
        """从中心站门户页的「进入WIKI」链接解析真实游戏子站 slug。

        门户页正文里的游戏入口是普通 <a>(非 JS 渲染,实测可解析)。
        只认「进入WIKI」文字的锚点,避免把导航/静态资源链接误当子站。
        解析不出返回 None,调用方降级为门户页本身。
        """
        await _throttle()
        response = await client.get(portal_url)
        if response.status_code != 200:
            return None
        match = re.search(
            r'href="(https?://[^"]*?biligame\.com/([A-Za-z0-9_]{2,32}))/?["][^>]*>[^<]*进入[^<]*WIKI',
            response.text,
        )
        if match is None:
            return None
        slug = match.group(2).lower()
        # 防御:排除中心站自身的保留路径,避免把导航链接当子站
        if slug in _CENTER_RESERVED_SLUGS:
            return None
        return slug

    async def _search_site(
        self,
        client: httpx.AsyncClient,
        slug: str,
        query: str,
        limit: int,
        site_label: str | None = None,
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
                    # 中心站的页面标题自带「XXXWIKI」后缀,再拼 slug 会变成
                    # 「（wiki wiki）」这种怪话,所以允许调用方自定义来源标签。
                    title=f"{title}（{site_label or f'{slug} wiki'}）",
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
            await _throttle()
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
