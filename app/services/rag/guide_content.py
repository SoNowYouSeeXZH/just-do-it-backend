"""攻略正文服务:按 URL 抓取页面正文,带缓存与节流。

为什么单独抽一个服务而不是在 API 层直接调 page_fetcher:

1. **缓存**:抓 wiki 页面是外部请求,同一篇攻略会被多个用户反复打开,
   cache-aside 一层能挡掉绝大多数重复抓取。TTL 给得比搜索长——
   wiki 正文更新频率远低于搜索结果的时效要求。
2. **节流**:page_fetcher 本身没有节流(节流在 wiki_provider 的 API 调用里),
   而 biligame 的 WAF 会对密集请求返回 567。公开接口暴露后必须自己控速,
   这里用「进程内全局锁 + 最小间隔」做最简单的匀速器:多 worker 部署时
   每个 worker 独立限速,对单人项目够用。
3. **失败语义**:FetchError 向上抛,由 API 层翻译成 502——
   「这篇页面现在抓不到」是用户可理解、可重试的临时状态,不是 500。
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time

from app.core import cache, cache_keys
from app.services.rag.page_fetcher import fetch_page

logger = logging.getLogger(__name__)

# 两次外部抓取之间的最小间隔(秒)。与 wiki_provider 的 API 节流同级,
# 但独立一份:它限的是「渲染页抓取」这条路径。
_MIN_FETCH_INTERVAL_SECONDS = 1.2

# 正文缓存时长(秒)。wiki 攻略页更新不频繁,一天失效一次足够新鲜。
_CONTENT_CACHE_TTL_SECONDS = 86400

_fetch_lock = asyncio.Lock()
_last_fetch_at = 0.0


async def _throttle() -> None:
    """匀速器:保证进程内两次外部抓取至少间隔 _MIN_FETCH_INTERVAL_SECONDS。

    用 monotonic 时钟而不是墙上时间:用户改系统时间不应该影响限速。
    """
    global _last_fetch_at
    async with _fetch_lock:
        wait = _MIN_FETCH_INTERVAL_SECONDS - (time.monotonic() - _last_fetch_at)
        if wait > 0:
            await asyncio.sleep(wait)
        _last_fetch_at = time.monotonic()


def _url_fingerprint(url: str) -> str:
    """URL 指纹:key 里不放原始 URL(太长且可能带用户数据,同 rag_search 的理由)。"""
    return hashlib.sha1(url.strip().lower().encode("utf-8")).hexdigest()[:16]


async def get_guide_content(url: str, max_chars: int) -> dict[str, str]:
    """按 URL 取攻略正文(纯文本),带缓存。返回 {url, content}。"""
    key = cache_keys.guide_content(f"{_url_fingerprint(url)}:{max_chars}")

    async def load() -> dict[str, str]:
        await _throttle()
        content = await fetch_page(url, max_chars=max_chars)
        logger.info("攻略正文抓取完成 url=%s chars=%d", url, len(content))
        return {"url": url, "content": content}

    return await cache.cache_aside_async(key, load, ttl=_CONTENT_CACHE_TTL_SECONDS)
