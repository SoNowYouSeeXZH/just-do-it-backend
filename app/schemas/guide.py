from pydantic import BaseModel, Field


class GuideSearchHit(BaseModel):
    """攻略搜索结果的公开契约。"""

    title: str
    url: str
    snippet: str


class GuideSearchQuery(BaseModel):
    """攻略搜索参数。"""

    q: str = Field(min_length=1, max_length=100)
    limit: int = Field(default=5, ge=1, le=10)
