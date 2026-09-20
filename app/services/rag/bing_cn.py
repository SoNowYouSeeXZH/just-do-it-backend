"""Bing 国内版搜索 Provider:抓取 cn.bing.com 结果页,产出游戏攻略站外链。

为什么需要它(2026-09-20 检索重设计):攻略检索收敛为「直接给用户外链跳走」,
需要 biligame wiki 之外的**论坛/攻略站**链接(游民星空/3DM/知乎/贴吧等)。
DDG 国内不可达;Baidu 有安全验证;Bing 国内版可达、免 key,实测对
「游戏名 + 攻略」类查询能返回游民/3DM/知乎等高质量攻略页。

已知限制(不藏着):
- 抓取型来源:页面改版会静默失效(正则解析不到就返回空,不抛错)。
- Bing 对部分多词中文查询有分词问题(实测「塞尔达 神庙」被拆成单字字典页),
  所以**必须**配域名白名单过滤——字典站(hanyuguoxue/gushici 之类)直接丢弃。
- 高频请求可能触发验证码。本 Provider 只做兜底补充,且调用方有 Redis 缓存护栏。
"""

from __future__ import annotations

import re
from typing import Any

import httpx

from app.config import settings
from app.services.rag.search_provider import SearchError, SearchHit

# 允许出现在结果里的游戏攻略相关站点(域名子串匹配)。
# 涵盖:综合攻略站 / 厂商社区 / 贴吧 / 知乎 / biligame wiki。
# 白名单的意义:宁可少给,也不给字典页/内容农场。
_ALLOWED_DOMAINS = (
    "bilibili.com",
    "biligame.com",
    "gamersky.com",
    "ali213.net",
    "3dmgame.com",
    "gamekee.com",
    "pvp.qq.com",
    "zhihu.com",
    "tieba.baidu.com",
    "baike.baidu.com",
    "ngabbs.com",
    "bbs.nga.cn",
)

_UA = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    ),
    "Accept-Language": "zh-CN,zh;q=0.9",
}

# 一条结果块:<li class="b_algo">…<h2><a href="URL">标题</a></h2>…<p>摘要</p>…</li>
_BLOCK = re.compile(r'<li class="b_algo".*?</li>', re.S)
_LINK = re.compile(r'<h2[^>]*><a[^>]*href="([^"]+)"[^>]*>(.*?)</a></h2>', re.S)
_TAG = re.compile(r"<[^>]+>")


def _strip_tags(text: str) -> str:
    import html as html_module

    return html_module.unescape(_TAG.sub("", text)).strip()


class BingCnProvider:
    """抓取 cn.bing.com 的 SERP 并按白名单过滤,契约与 wiki provider 一致。"""

    async def search(self, query: str, max_results: int) -> list[SearchHit]:
        # 调用方传入的 query 已拼「攻略」;相关性过滤用去掉「攻略」后的核心词
        core_tokens = [
            token.strip()
            for token in query.replace("攻略", " ").split()
            if len(token.strip()) >= 2
        ]

        try:
            async with httpx.AsyncClient(
                timeout=settings.search_request_timeout_seconds,
                headers=_UA,
                follow_redirects=True,
            ) as client:
                response = await client.get(
                    "https://cn.bing.com/search",
                    params={"q": query, "mkt": "zh-CN", "count": "20"},
                )
        except httpx.HTTPError as exc:
            # 抓取失败对兜底来源是常态:静默返回空,不打断主流程。
            raise SearchError(f"bing 抓取失败({type(exc).__name__})") from exc

        if response.status_code != 200:
            raise SearchError(f"bing 返回 {response.status_code}")

        hits: list[SearchHit] = []
        for block in _BLOCK.findall(response.text)[: max_results * 3]:
            link = _LINK.search(block)
            if link is None:
                continue
            url = link.group(1).strip()
            title = _strip_tags(link.group(2))
            if not url.startswith("http"):
                continue
            # 域名白名单:只保留游戏攻略相关站点,字典页/内容农场直接丢弃
            if not any(domain in url for domain in _ALLOWED_DOMAINS):
                continue
            # 摘要取块内第一个 <p>;没有就空着(标题+链接已可用)
            para = re.search(r"<p[^>]*>(.*?)</p>", block, re.S)
            snippet = _strip_tags(para.group(1)) if para else ""
            # 相关性过滤:Bing 对部分多词中文查询有分词问题(实测「纳塔 攻略」
            # 被拆成单字,返回「纳」的字典页——而字典站恰好在白名单里)。
            # 要求标题/摘要至少包含一个核心词,字典垃圾在这里被拦下。
            if core_tokens:
                haystack = (title + snippet).lower()
                if not any(token.lower() in haystack for token in core_tokens):
                    continue
            hits.append(SearchHit(title=title, url=url, snippet=snippet))
            if len(hits) >= max_results:
                break

        return hits
