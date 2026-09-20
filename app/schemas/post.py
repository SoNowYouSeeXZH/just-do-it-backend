"""社区帖子相关 DTO。"""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class PostCreate(BaseModel):
    game_slug: str = Field(min_length=1, max_length=32)
    title: str = Field(min_length=1, max_length=120)
    content: str = Field(min_length=1, max_length=5000)
    city: str | None = Field(default=None, max_length=32)
    # 发帖位置(可选):前端拿到定位就带上,坐标入库用于「附近」距离过滤,
    # district 是模糊化后的行政区名(展示用)。三者要么都有要么都无。
    lat: float | None = Field(default=None, ge=-90, le=90)
    lng: float | None = Field(default=None, ge=-180, le=180)
    district: str | None = Field(default=None, max_length=32)


class PostPublic(BaseModel):
    id: int
    game_slug: str
    city: str | None
    # 行政区名(模糊化位置)。精确坐标 lat/lng 不在公开 DTO 里——只到行政区级。
    district: str | None
    # 距离(公里)。仅「附近」查询(带 lat/lng)时由服务层计算回填,其他查询为 None。
    distance_km: float | None = None
    title: str
    content: str
    status: str
    like_count: int
    comment_count: int
    created_at: datetime
    # 作者名而不是 user_id:前端只需要展示,暴露内部主键没有收益。
    author_name: str


class CommentCreate(BaseModel):
    content: str = Field(min_length=1, max_length=1000)


class CommentPublic(BaseModel):
    id: int
    post_id: int
    content: str
    created_at: datetime
    author_name: str


class LikeResult(BaseModel):
    post_id: int
    liked: bool
    like_count: int


class PostReviewRequest(BaseModel):
    """审核决定。用 Literal 限死取值,非法状态在进业务层前就被 422 拦掉,
    业务层不用再写一遍「这个状态合不合法」的校验。
    """

    status: Literal["published", "rejected"]
