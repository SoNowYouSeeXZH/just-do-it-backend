"""搜索 Provider:把"一句查询"变成"一组候选来源"。

为什么要抽一层 Protocol 而不是直接调 ddgs:

一期用 DDG 是因为它免注册、无 API Key、无调用费用——对个人项目来说
「不用注册账号」这件事本身就有价值。但它是**抓取型**来源,有两个已知风险:
频控(请求密了会被限)和区域可达性(某些网络环境直连不通)。真遇到时
需要换成 key 型服务(Tavily / 博查),而换的时候不应该动 agent 和 tools。

所以边界画在这里:上层只认识 `SearchHit`,不知道下面是 DDG 还是别的。
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
from dataclasses import dataclass
from typing import Any, Protocol

from app.config import settings
from app.core import cache, cache_keys

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SearchHit:
    """一条搜索结果。

    刻意只保留三个字段:标题、地址、摘要。不同 Provider 返回的额外字段
    (排名分数、发布时间、图片)各不相同,放进来就等于把某个 Provider 的
    形状泄漏成公共契约,换 Provider 时上层要跟着改。
    """

    title: str
    url: str
    snippet: str

    def to_dict(self) -> dict[str, str]:
        """给缓存序列化和喂给模型用。缓存里存 dict 而不是对象,是因为
        Redis 只认字符串,而 dataclass 不能直接 json.dumps。"""
        return {"title": self.title, "url": self.url, "snippet": self.snippet}


class SearchError(RuntimeError):
    """搜索失败。

    刻意不用 AppError:搜索失败**不应该**让整个请求变成 5xx。
    调用方(tools.py)会把它转成一条 observation 交回给模型,
    让模型换个关键词重试,或者声明检索失败后凭常识作答。
    "外部依赖挂了" 和 "这次请求失败了" 是两件事。
    """


class SearchProvider(Protocol):
    """搜索能力的最小契约。"""

    async def search(self, query: str, max_results: int) -> list[SearchHit]: ...


class DDGProvider:
    """DuckDuckGo 实现,基于 `ddgs` 包(原 duckduckgo_search 更名而来)。

    两个必须知道的细节:

    1. `ddgs` 的 API 是**同步阻塞**的。直接在 async 函数里调它会把整个
       事件循环卡住——同一进程里其他请求全部跟着等。所以用
       `asyncio.to_thread` 丢到线程池里跑,并在外面套 `wait_for` 兜总超时。

    2. 它的 `backend` 默认是 `auto`,会把所有引擎打乱顺序全试一遍。
       实测下来这会让一次搜索耗 30~50 秒(多数引擎在开发网络里不可达),
       所以这里显式传配置里的引擎顺序,让不可达的引擎快速失败。
    """

    def __init__(self, *, region: str | None = None, backends: str | None = None) -> None:
        self._region = region if region is not None else settings.search_region
        self._backends = backends if backends is not None else settings.search_backends

    async def search(self, query: str, max_results: int) -> list[SearchHit]:
        try:
            raw = await asyncio.wait_for(
                asyncio.to_thread(self._search_blocking, query, max_results),
                timeout=settings.search_timeout_seconds,
            )
        except TimeoutError as exc:
            raise SearchError(f"搜索超时({settings.search_timeout_seconds}s)") from exc
        except Exception as exc:  # noqa: BLE001 - 三方库异常类型不稳定,统一收口
            # 不把原始异常文本直接往上抛给模型/用户:抓取型库的报错里
            # 可能带上完整请求 URL 和内部堆栈。只保留类型名。
            raise SearchError(f"搜索失败({type(exc).__name__})") from exc

        return [hit for hit in (_to_hit(item) for item in raw) if hit is not None]

    def _search_blocking(self, query: str, max_results: int) -> list[dict[str, Any]]:
        # 延迟到调用时才 import:让"没装 ddgs 也能 import 本模块"成立,
        # 测试和 rag_enabled=False 的部署都不必装这个依赖。
        from ddgs import DDGS

        # 这里的 timeout 是**单个引擎**的 HTTP 超时,不是总预算;
        # 总预算由外层 wait_for 控制。两者分开才能实现"快速失败 + 换下一个"。
        return DDGS(timeout=int(settings.search_request_timeout_seconds)).text(
            query,
            max_results=max_results,
            region=self._region,
            backend=self._backends,
        )


def _to_hit(item: dict[str, Any]) -> SearchHit | None:
    """把 ddgs 的 dict 映射成 SearchHit;缺 url 的条目直接丢掉。

    没有 url 的结果对 RAG 没有意义——既没法抓正文,也没法作为来源引用。
    宁可少一条,也不要往上下文里塞一条无法溯源的内容。
    """
    url = (item.get("href") or "").strip()
    if not url:
        return None
    return SearchHit(
        title=(item.get("title") or "").strip(),
        url=url,
        snippet=(item.get("body") or "").strip(),
    )


_WHITESPACE = re.compile(r"\s+")


def query_fingerprint(query: str) -> str:
    """把查询归一化后哈希,作为缓存 key 的一部分。

    归一化做三件事:去首尾空白、把连续空白压成一个空格、转小写。
    这样「  塞尔达  神庙 」和「塞尔达 神庙」命中同一份缓存。
    不做更激进的处理(比如去标点、分词):归一化过度会让本该不同的
    查询撞进同一个缓存,那是答错,比缓存没命中严重得多。

    用 sha1 而不是 md5 没有安全考虑(这里不涉及对抗),只是取个定长摘要;
    截断到 16 位足够避免碰撞,又让 key 短一些便于人工排查。
    """
    normalized = _WHITESPACE.sub(" ", query.strip()).lower()
    return hashlib.sha1(normalized.encode("utf-8")).hexdigest()[:16]


_provider: SearchProvider | None = None


def get_provider() -> SearchProvider:
    """按配置返回 Provider 实例(惰性 + 进程内复用)。

    命名和 core/cache.py、services/llm.py 的 get_client 保持一致。
    """
    global _provider
    if _provider is None:
        name = settings.search_provider.lower()
        if name == "wiki":
            # 局部 import 避免循环依赖:wiki_provider 要用本模块的 SearchHit。
            from app.services.rag.wiki_provider import MediaWikiProvider

            _provider = MediaWikiProvider()
        elif name == "ddg":
            _provider = DDGProvider()
        else:
            # 明确报错而不是静默回退到默认实现:配错了却"看起来在工作"
            # 会让人以为换 Provider 生效了,实际用的还是旧的。
            raise SearchError(f"未知的 search_provider: {settings.search_provider}")
    return _provider


def reset_provider_for_tests() -> None:
    global _provider
    _provider = None


def _filter_noise(hits: list[SearchHit], query: str) -> list[SearchHit]:
    """过滤子站全文搜索的噪音结果。

    纯数字/超短的查询(如「2077」)会匹配到数值表格页——原神 wiki 的怪物
    属性表里到处是四位数字,标题却是「水萤」「可莉」,和查询毫无关系。
    对这类查询要求命中词出现在**标题**里(数值表标题不含 2077,被滤掉);
    正常长度的查询(「纳塔 地灵龛之钥」)不做过滤,避免误杀标题不含原词的
    合法结果(比如标题是「XX入口」正文讲「怎么进」的页面)。
    """
    normalized = query.strip().lower()
    is_noisy = normalized.isdigit() or len(normalized) <= 2
    if not is_noisy:
        return hits
    return [hit for hit in hits if normalized in hit.title.lower()]


async def _search_bing_externals(query: str, limit: int) -> list[SearchHit]:
    """Bing 国内版外链搜索:论坛/攻略站(游民/3DM/知乎/贴吧等)。

    2026-09-20 检索重设计:产品形态收敛为「搜出外链,用户跳走」,所以
    通用网页搜索从「可选兜底」升级为「主来源之一」。query 拼上「攻略」
    提高命中率;域名白名单在 BingCnProvider 内部过滤。
    任何失败静默返回空——抓取型来源挂了不能拖垮整个搜索。
    """
    # 局部 import:bing_cn 反向依赖本模块的 SearchHit,顶层导入会循环。
    from app.services.rag.bing_cn import BingCnProvider

    try:
        hits = await BingCnProvider().search(f"{query} 攻略", limit)
        if hits:
            logger.info("bing 外链命中 query=%s hits=%d", query, len(hits))
        return hits
    except SearchError as exc:
        logger.warning("bing 外链搜索失败 query=%s reason=%s", query, exc)
        return []


async def search_guides(query: str, max_results: int | None = None) -> list[SearchHit]:
    """攻略搜索的统一入口(公开接口与 Agent 工具共用)。

    三路来源合并(2026-09-20 重设计,取代原「子站→中心站→DDG」瀑布):
    1. **biligame 子站白名单**(精准):预置游戏的攻略页,站点内搜索。
    2. **中心站深挖**(未预置游戏):intitle 找游戏门户 → 解析「进入WIKI」
       得到真实子站 → 用原关键词搜子站,拿到真攻略页;失败降级为门户入口。
    3. **Bing 外链**(论坛/攻略站):游民/3DM/知乎/贴吧等,域名白名单过滤,
       满足「帮用户搜出其他攻略网站的链接,点击跳走」的产品形态。

    合并顺序:外链在前(用户要的就是跳走的论坛攻略),wiki 页在后,按 URL 去重。
    任一路失败都静默降级为空,不影响其他路;全部为空就诚实返回空。
    缓存 fail-open:Redis 挂了只是每次真查,功能不不可用。
    """
    limit = max_results or settings.rag_search_max_results
    key = cache_keys.rag_search(f"{query_fingerprint(query)}:{limit}")

    async def load() -> list[dict[str, str]]:
        # 1) biligame 子站(预置游戏)
        try:
            wiki_hits = _filter_noise(await get_provider().search(query, limit), query)
        except SearchError as exc:
            logger.info("子站检索失败(继续其他来源) query=%s err=%s", query, exc)
            wiki_hits = []

        # 2) 中心站深挖(未预置游戏;预置游戏命中时它通常为空,无害)
        center = getattr(get_provider(), "search_center", None)
        center_hits: list[SearchHit] = []
        if center is not None:
            try:
                center_hits = await center(query, limit)
            except SearchError as exc:
                logger.info("中心站检索失败 query=%s err=%s", query, exc)

        # 3) Bing 外链(论坛/攻略站),拼「攻略」提高相关性
        externals = await _search_bing_externals(query, limit)

        # 合并去重:外链在前(用户要的就是跳转),wiki 攻略页在后
        seen: set[str] = set()
        merged: list[SearchHit] = []
        for hit in [*externals, *wiki_hits, *center_hits]:
            if hit.url in seen:
                continue
            seen.add(hit.url)
            merged.append(hit)
            if len(merged) >= limit:
                break

        logger.info("搜索完成 query=%s hits=%d (外链%d wiki%d 中心站%d)",
                    query, len(merged), len(externals), len(wiki_hits), len(center_hits))
        return [hit.to_dict() for hit in merged]

    # 缓存里存的是 dict 列表(可 JSON 序列化),取回来再还原成 SearchHit。
    # 直接缓存对象需要 pickle,而 pickle 反序列化不可信数据是安全隐患。
    raw = await cache.cache_aside_async(
        key, load, ttl=settings.rag_query_cache_ttl_seconds
    )
    return [
        SearchHit(
            title=item.get("title", ""),
            url=item.get("url", ""),
            snippet=item.get("snippet", ""),
        )
        for item in raw or []
    ]
