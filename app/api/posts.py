"""社区帖子接口。

浏览类接口对游客开放;发帖、评论、点赞需要登录。
这一层只做契约声明和转交,404 由业务层抛出、全局处理器翻译。
"""

from typing import Annotated

from fastapi import APIRouter, Depends, Query

from app.api.deps import get_current_user_id
from app.db import SessionDep
from app.schemas.post import (
    CommentCreate,
    CommentPublic,
    LikeResult,
    PostCreate,
    PostPublic,
)
from app.services import post as post_service

router = APIRouter(prefix="/api/posts", tags=["posts"])


@router.get("", response_model=list[PostPublic])
def list_posts(
    session: SessionDep,
    game_slug: Annotated[str | None, Query(max_length=32)] = None,
    city: Annotated[str | None, Query(max_length=32)] = None,
    limit: Annotated[int, Query(ge=1, le=50)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[PostPublic]:
    """列出已发布的帖子,游客可访问。offset/limit 分页。"""
    return post_service.list_posts(
        session, game_slug=game_slug, city=city, limit=limit, offset=offset
    )


@router.post("", response_model=PostPublic)
def create_post(
    payload: PostCreate,
    session: SessionDep,
    user_id: Annotated[int, Depends(get_current_user_id)],
) -> PostPublic:
    """发帖,需要登录。是否直接发布取决于审核配置。"""
    return post_service.create_post(
        session,
        user_id=user_id,
        game_slug=payload.game_slug,
        title=payload.title,
        content=payload.content,
        city=payload.city,
    )


@router.get("/mine", response_model=list[PostPublic])
def list_my_posts(
    session: SessionDep,
    user_id: Annotated[int, Depends(get_current_user_id)],
    limit: Annotated[int, Query(ge=1, le=50)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[PostPublic]:
    """我发过的帖子,含待审和被驳回的。需要登录。

    这个路由必须声明在 `/{post_id}` 之前:FastAPI 按声明顺序匹配,
    放在后面会先命中 `/{post_id}` 并试图把 "mine" 解析成 int,直接 422。
    """
    return post_service.list_my_posts(
        session, user_id=user_id, limit=limit, offset=offset
    )


@router.get("/{post_id}", response_model=PostPublic)
def get_post(post_id: int, session: SessionDep) -> PostPublic:
    return post_service.get_post(session, post_id)


@router.get("/{post_id}/comments", response_model=list[CommentPublic])
def list_comments(
    post_id: int,
    session: SessionDep,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[CommentPublic]:
    return post_service.list_comments(
        session, post_id=post_id, limit=limit, offset=offset
    )


@router.post("/{post_id}/comments", response_model=CommentPublic)
def create_comment(
    post_id: int,
    payload: CommentCreate,
    session: SessionDep,
    user_id: Annotated[int, Depends(get_current_user_id)],
) -> CommentPublic:
    return post_service.create_comment(
        session, post_id=post_id, user_id=user_id, content=payload.content
    )


@router.post("/{post_id}/like", response_model=LikeResult)
def toggle_like(
    post_id: int,
    session: SessionDep,
    user_id: Annotated[int, Depends(get_current_user_id)],
) -> LikeResult:
    """点赞或取消点赞,需要登录。"""
    return post_service.toggle_like(session, post_id=post_id, user_id=user_id)
