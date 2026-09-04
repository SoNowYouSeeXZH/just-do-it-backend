"""游戏攻略搜索接口。"""

from fastapi import APIRouter, Query

from app.schemas.guide import GuideSearchHit
from app.services.rag import search_provider

router = APIRouter(prefix="/api/guides", tags=["guides"])


@router.get("/search", response_model=list[GuideSearchHit])
async def search_guides(
    q: str = Query(min_length=1, max_length=100),
    limit: int = Query(default=5, ge=1, le=10),
) -> list[GuideSearchHit]:
    """公开搜索攻略候选,不触发大模型调用。"""
    hits = await search_provider.search_guides(q, max_results=limit)
    return [GuideSearchHit(**hit.to_dict()) for hit in hits]
