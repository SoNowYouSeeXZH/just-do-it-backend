"""游戏攻略搜索与正文接口。"""

from fastapi import APIRouter, HTTPException, Query

from app.schemas.guide import GuideContent, GuideSearchHit
from app.services.rag import search_provider
from app.services.rag.guide_content import get_guide_content

router = APIRouter(prefix="/api/guides", tags=["guides"])


@router.get("/search", response_model=list[GuideSearchHit])
async def search_guides(
    q: str = Query(min_length=1, max_length=100),
    limit: int = Query(default=5, ge=1, le=10),
) -> list[GuideSearchHit]:
    """公开搜索攻略外链,不触发大模型调用。

    2026-09-20 检索重设计:产品形态收敛为「搜出外链,用户跳走」——
    返回 biligame wiki 攻略页 + 游民/3DM/知乎等攻略站的链接,
    由前端直接调起系统浏览器。
    """
    hits = await search_provider.search_guides(q, max_results=limit)
    return [GuideSearchHit(**hit.to_dict()) for hit in hits]


@router.get("/content", response_model=GuideContent)
async def guide_content(
    url: str = Query(min_length=1, max_length=500),
    max_chars: int = Query(default=8000, ge=500, le=20000),
) -> GuideContent:
    """按 URL 返回攻略页面正文(纯文本),App 内阅读用。

    只接受 http/https;SSRF 防护(内网地址黑名单、重定向逐跳校验)由
    page_fetcher 负责。抓取失败返回 502:这是「这篇页面暂时读不了」,
    不是服务端故障,客户端可以提示重试或跳转外部浏览器。
    """
    if not (url.startswith("http://") or url.startswith("https://")):
        raise HTTPException(status_code=422, detail="仅支持 http/https 链接")
    try:
        data = await get_guide_content(url, max_chars)
    except Exception as exc:  # noqa: BLE001 - FetchError 等统一翻译成 502
        raise HTTPException(status_code=502, detail="攻略页面暂时读不了,请稍后重试") from exc
    return GuideContent(**data)
