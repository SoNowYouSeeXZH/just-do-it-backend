"""社区帖子相关 DTO。"""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class PostCreate(BaseModel):
    game_slug: str = Field(min_length=1, max_length=32)
    title: str = Field(min_length=1, max_length=120)
    content: str = Field(min_length=1, max_length=5000)
    city: str | None = Field(default=None, max_length=32)


class PostPublic(BaseModel):
    id: int
    game_slug: str
    city: str | None
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
