"""语料爬虫:把 wiki 页面抓进 guide_chunks 表,可选同时算嵌入。

用法(在仓库根目录):
    # 默认按分类采集(推荐)
    PYTHONPATH=. .venv/bin/python -m app.crawl_corpus --sites ys --max-pages 200
    # 只爬语料不算嵌入(不花钱,用于调分块规则)
    PYTHONPATH=. .venv/bin/python -m app.crawl_corpus --sites ys,sr,zzz --no-embed
    # 退回全站枚举(不推荐,见下)
    PYTHONPATH=. .venv/bin/python -m app.crawl_corpus --strategy allpages

设计取舍:
- **页面来源默认走分类(categorymembers)而不是全站枚举(allpages)**。
  实测 allpages 按字母序返回,前几页是「0人」「1.1版本更新专题」这类页面,
  其中一个页面的正文还是用户写的伤害计算公式(MATLAB 风格)。语料库的价值
  取决于内容质量,不是页数——按「角色/武器/机制攻略」这类分类采集,
  同样的抓取预算能换来完全不同的语料。allpages 保留为 --strategy 兜底。
- 复用 wiki_provider 的节流(1.5s 间隔)与重试——爬虫比在线问答更容易触发 WAF,
  一次跑几百页必须把礼貌限速当默认行为。实测背靠背请求会返回 200 + HTML 拦截页。
- --no-embed 支持先把语料爬进来、之后单独补嵌入:嵌入要花钱,抓取不用,
  分开跑能反复重爬调规则而不重复计费。
- 页与页之间独立提交:某页失败只丢这一页,不让整个批次回滚。
"""

from __future__ import annotations

import argparse
import asyncio
import logging
from urllib.parse import quote

import httpx
from sqlmodel import Session

from app.config import settings
from app.db import engine
from app.models.guide_chunk import GuideChunk
from app.repositories import guide_chunk as chunk_repo
from app.services import embedding
from app.services.corpus import JUNK_CATEGORIES, chunk_page
from app.services.rag import wiki_provider

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("crawl_corpus")

# api.php 的 list 上限是 500;取小一点减少单请求被拦时的重试成本。
_PAGE_BATCH = 100

# 按优先级排列的种子分类。前面的先采,预算(max_pages)用完就停,
# 所以顺序即优先级:角色/武器是最常被问的实体,攻略类是成体系的长文。
# 各站分类名不完全一致,取不到的分类会被跳过(记一条 info,不算失败)。
SEED_CATEGORIES = ("角色", "武器", "机制攻略", "攻略", "探索攻略", "怪物", "圣遗物套装")


async def _get_json(client: httpx.AsyncClient, url: str, params: dict) -> dict:
    """带节流与一次退避重试的 api.php GET。直接借用 wiki_provider 的实现。"""
    from app.services.rag.search_provider import SearchError

    last = ""
    for attempt in range(2):
        try:
            await wiki_provider._throttle()
            response = await client.get(url, params=params)
        except httpx.HTTPError as exc:
            last = type(exc).__name__
        else:
            if response.status_code == 200:
                try:
                    return response.json()
                except ValueError as exc:
                    raise SearchError("wiki 返回的不是 JSON(可能被拦截)") from exc
            last = f"HTTP {response.status_code}"
            if response.status_code not in wiki_provider._RETRYABLE_STATUS:
                break
        if attempt == 0:
            await asyncio.sleep(settings.wiki_retry_delay_seconds)
    raise RuntimeError(f"api.php 请求失败: {last}")


async def list_page_titles(
    client: httpx.AsyncClient, api_url: str, max_pages: int
) -> list[str]:
    """allpages 枚举正文空间(namespace 0)的页面标题,带 continuation。"""
    titles: list[str] = []
    params: dict = {
        "action": "query",
        "list": "allpages",
        "apnamespace": 0,
        "aplimit": min(_PAGE_BATCH, max_pages),
        "format": "json",
        "formatversion": "2",
    }
    while len(titles) < max_pages:
        payload = await _get_json(client, api_url, params)
        batch = payload.get("query", {}).get("allpages", [])
        if not batch:
            break
        titles.extend(item["title"] for item in batch if item.get("title"))
        continuation = payload.get("continue")
        if not continuation:
            break
        # continuation 里的键必须原样回传(如 apcontinue),不能只挑一个。
        params.update(continuation)
        params["aplimit"] = min(_PAGE_BATCH, max_pages - len(titles))
        logger.info("allpages 已枚举 %d 页,继续…", len(titles))
    return titles[:max_pages]


async def list_category_titles(
    client: httpx.AsyncClient, api_url: str, category: str, limit: int
) -> list[str]:
    """取单个分类下的正文页标题(namespace 0)。分类不存在时返回空列表。"""
    titles: list[str] = []
    params: dict = {
        "action": "query",
        "list": "categorymembers",
        "cmtitle": f"Category:{category}",
        "cmnamespace": 0,
        "cmlimit": min(_PAGE_BATCH, limit),
        "format": "json",
        "formatversion": "2",
    }
    while len(titles) < limit:
        payload = await _get_json(client, api_url, params)
        batch = payload.get("query", {}).get("categorymembers", [])
        if not batch:
            break
        titles.extend(item["title"] for item in batch if item.get("title"))
        continuation = payload.get("continue")
        if not continuation:
            break
        params.update(continuation)
        params["cmlimit"] = min(_PAGE_BATCH, limit - len(titles))
    return titles[:limit]


async def collect_titles_by_category(
    client: httpx.AsyncClient, api_url: str, max_pages: int
) -> list[str]:
    """按 SEED_CATEGORIES 顺序采集页面标题,去重后截断到 max_pages。

    顺序即优先级:预算用完就停,所以靠前的分类一定被覆盖到。
    """
    seen: set[str] = set()
    titles: list[str] = []
    for category in SEED_CATEGORIES:
        if len(titles) >= max_pages:
            break
        remaining = max_pages - len(titles)
        try:
            batch = await list_category_titles(client, api_url, category, remaining)
        except RuntimeError as exc:
            # 单个分类取不到不算失败:各站分类命名不完全一致。
            logger.info("分类枚举失败(跳过) category=%s err=%s", category, exc)
            continue
        added = 0
        for title in batch:
            if title not in seen:
                seen.add(title)
                titles.append(title)
                added += 1
        logger.info("[分类] %s 贡献 %d 页(累计 %d)", category, added, len(titles))
    return titles


async def fetch_wikitext(
    client: httpx.AsyncClient, api_url: str, title: str
) -> str | None:
    """取单页 wikitext。页面不存在/为空/属于维护类分类时返回 None。

    一次 parse 请求就顺带拿到分类,不用为了过滤再发一次请求。
    """
    payload = await _get_json(
        client,
        api_url,
        {
            "action": "parse",
            "page": title,
            "prop": "wikitext|categories",
            "format": "json",
            "formatversion": "2",
        },
    )
    parse = payload.get("parse")
    if not isinstance(parse, dict):
        return None

    categories = {
        (item.get("category") or "").replace("_", " ")
        for item in parse.get("categories", [])
        if isinstance(item, dict)
    }
    if categories & JUNK_CATEGORIES:
        # 「待完善」「施工中」这类页面的正文常常只有模板占位,
        # 嵌进语料库只会占 top_k 名额。
        logger.info("跳过维护类页面 title=%s cats=%s", title, categories & JUNK_CATEGORIES)
        return None

    return parse.get("wikitext") or None


async def crawl_site(
    client: httpx.AsyncClient,
    session: Session,
    *,
    slug: str,
    max_pages: int,
    do_embed: bool,
    strategy: str = "category",
) -> tuple[int, int]:
    """爬一个站点。返回 (成功页数, 成功块数)。"""
    api_url = f"{settings.wiki_base_url.rstrip('/')}/{slug}/api.php"
    if strategy == "allpages":
        titles = await list_page_titles(client, api_url, max_pages)
    else:
        titles = await collect_titles_by_category(client, api_url, max_pages)
    logger.info("[%s] 待抓取 %d 个页面(strategy=%s)", slug, len(titles), strategy)

    ok_pages = ok_chunks = 0
    for index, title in enumerate(titles, start=1):
        try:
            wikitext = await fetch_wikitext(client, api_url, title)
        except RuntimeError as exc:
            logger.warning("[%s] 抓取失败 title=%s err=%s", slug, title, exc)
            continue
        if not wikitext:
            continue

        chunk_texts = chunk_page(title, wikitext)
        if not chunk_texts:
            continue

        vectors: list[list[float]] | None = None
        if do_embed:
            try:
                vectors = await embedding.embed_texts(chunk_texts)
            except Exception as exc:  # noqa: BLE001 - 计费/网络错误都只该跳过本页
                logger.error("[%s] 嵌入失败 title=%s err=%s", slug, title, exc)
                continue

        rows = [
            GuideChunk(
                game_slug=slug,
                source_url=f"{settings.wiki_base_url.rstrip('/')}/{slug}/{quote(title)}",
                title=title[:200],
                chunk_index=i,
                chunk_text=text,
                embedding=vec if vectors is not None else None,
            )
            for i, (text, vec) in enumerate(
                zip(chunk_texts, vectors or [None] * len(chunk_texts), strict=True)
            )
        ]
        chunk_repo.replace_page_chunks(session, rows)
        session.commit()
        ok_pages += 1
        ok_chunks += len(rows)
        if index % 10 == 0 or index == len(titles):
            logger.info("[%s] 进度 %d/%d 页,累计 %d 块", slug, index, len(titles), ok_chunks)

    return ok_pages, ok_chunks


async def main() -> None:
    parser = argparse.ArgumentParser(description="爬 wiki 语料进 guide_chunks")
    parser.add_argument("--sites", default=settings.wiki_sites, help="逗号分隔的站点 slug")
    parser.add_argument("--max-pages", type=int, default=50, help="每站最多爬多少页")
    parser.add_argument(
        "--strategy",
        choices=("category", "allpages"),
        default="category",
        help="页面来源:category 按种子分类采集(默认,质量高);allpages 全站字母序枚举",
    )
    parser.add_argument(
        "--no-embed",
        action="store_true",
        help="只爬语料不算嵌入(之后可通过重跑补上)",
    )
    args = parser.parse_args()

    sites = [s.strip() for s in args.sites.split(",") if s.strip()]
    do_embed = not args.no_embed
    if do_embed and not settings.embedding_api_key:
        raise SystemExit("未配置 EMBEDDING_API_KEY;先爬语料请加 --no-embed")

    logger.info(
        "开始爬取 sites=%s max_pages=%d strategy=%s embed=%s",
        sites,
        args.max_pages,
        args.strategy,
        do_embed,
    )
    async with httpx.AsyncClient(
        timeout=settings.wiki_request_timeout_seconds,
        headers={"User-Agent": "JustDoItBot/1.0 (+game guide assistant)"},
        follow_redirects=True,
    ) as client:
        with Session(engine) as session:
            total_pages = total_chunks = 0
            for slug in sites:
                pages, chunks = await crawl_site(
                    client,
                    session,
                    slug=slug,
                    max_pages=args.max_pages,
                    do_embed=do_embed,
                    strategy=args.strategy,
                )
                total_pages += pages
                total_chunks += chunks
                logger.info("[%s] 完成: %d 页 %d 块", slug, pages, chunks)

    with Session(engine) as session:
        embedded = chunk_repo.count_chunks(session, embedded_only=True)
        total = chunk_repo.count_chunks(session)
    logger.info(
        "全部完成: 本次 %d 页 %d 块;库内共 %d 块(其中已嵌入 %d)",
        total_pages,
        total_chunks,
        total,
        embedded,
    )


if __name__ == "__main__":
    asyncio.run(main())
