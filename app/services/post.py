"""社区帖子业务逻辑:发帖审核、列表过滤、评论与点赞。

审核策略(一期):
- 帖子命中敏感词 -> 强制进 pending 等人工复核,即使 community_auto_publish=True。
  不直接拒绝是因为词表是粗粒度的,误杀正常内容的代价比多一次人工复核大。
- 评论命中敏感词 -> 直接拒绝(ContentRejectedError)。评论表没有审核状态列,
  一期不为它单独建审核队列;评论短、改一句重发的成本很低。
- 没命中时,`community_auto_publish=True` 直接发布(本地/自用),
  False 则统一进 pending,由管理端接口改为 published/rejected。
"""

import logging

from sqlmodel import Session

from app.config import settings
from app.core.exceptions import ContentRejectedError, ResourceNotFoundError
from app.models.post import (
    POST_STATUS_PENDING,
    POST_STATUS_PUBLISHED,
)
from app.repositories import post as post_repo
from app.schemas.post import CommentPublic, LikeResult, PostPublic
from app.services import moderation

logger = logging.getLogger(__name__)


def _to_public(post, author_name: str) -> PostPublic:  # noqa: ANN001 - 入参是 ORM 模型
    # 显式列字段而不是 model_validate:Post 上有 user_id 这类不该外泄的列,
    # 白名单写法能保证以后给表加字段时不会顺带泄露出去。
    return PostPublic(
        id=post.id,
        game_slug=post.game_slug,
        city=post.city,
        title=post.title,
        content=post.content,
        status=post.status,
        like_count=post.like_count,
        comment_count=post.comment_count,
        created_at=post.created_at,
        author_name=author_name,
    )


def _comment_to_public(comment, author_name: str) -> CommentPublic:  # noqa: ANN001
    return CommentPublic(
        id=comment.id,
        post_id=comment.post_id,
        content=comment.content,
        created_at=comment.created_at,
        author_name=author_name,
    )


def list_posts(
    session: Session,
    *,
    game_slug: str | None = None,
    city: str | None = None,
    limit: int = 20,
    offset: int = 0,
) -> list[PostPublic]:
    """列出已发布的帖子。未审核内容不进入公开列表。"""
    rows = post_repo.list_published(
        session, game_slug=game_slug, city=city, limit=limit, offset=offset
    )
    return [_to_public(post, author_name) for post, author_name in rows]


def list_my_posts(
    session: Session, *, user_id: int, limit: int = 20, offset: int = 0
) -> list[PostPublic]:
    """「我的帖子」:作者本人能看到自己的待审和被驳回内容。

    没有这个接口的话,命中审核规则的帖子对作者来说就是「发完就消失」——
    公开接口对未发布内容返回 404,作者会以为发帖失败而反复重发。
    """
    rows = post_repo.list_by_user(session, user_id=user_id, limit=limit, offset=offset)
    return [_to_public(post, author_name) for post, author_name in rows]


def create_post(
    session: Session,
    *,
    user_id: int,
    game_slug: str,
    title: str,
    content: str,
    city: str | None,
) -> PostPublic:
    hit = moderation.find_banned_word(title, content)
    if hit:
        # 命中即降级为待审,不理会 auto_publish。日志只记命中词,不抄正文。
        status = POST_STATUS_PENDING
        logger.warning("发帖命中敏感词 user_id=%s word=%s", user_id, hit)
    else:
        status = (
            POST_STATUS_PUBLISHED
            if settings.community_auto_publish
            else POST_STATUS_PENDING
        )
    post = post_repo.insert(
        session,
        user_id=user_id,
        game_slug=game_slug,
        title=title,
        content=content,
        city=city,
        status=status,
    )
    logger.info("新帖创建 post_id=%s status=%s", post.id, post.status)
    return _to_public(post, post_repo.get_author_name(session, user_id))


def _require_published_post(session: Session, post_id: int):  # noqa: ANN202
    post = post_repo.get(session, post_id)
    if post is None or post.status != POST_STATUS_PUBLISHED:
        # 未发布的帖子对外表现为「不存在」,避免通过 404/403 差异探测待审内容。
        raise ResourceNotFoundError("帖子不存在")
    return post


def get_post(session: Session, post_id: int) -> PostPublic:
    post = _require_published_post(session, post_id)
    return _to_public(post, post_repo.get_author_name(session, post.user_id))


def list_comments(
    session: Session, *, post_id: int, limit: int = 50, offset: int = 0
) -> list[CommentPublic]:
    _require_published_post(session, post_id)
    rows = post_repo.list_comments(session, post_id, limit, offset)
    return [
        _comment_to_public(comment, author_name) for comment, author_name in rows
    ]


def create_comment(
    session: Session, *, post_id: int, user_id: int, content: str
) -> CommentPublic:
    _require_published_post(session, post_id)
    hit = moderation.find_banned_word(content)
    if hit:
        logger.warning("评论命中敏感词 user_id=%s post_id=%s word=%s", user_id, post_id, hit)
        raise ContentRejectedError("评论包含不允许发布的内容,请修改后重试")
    comment = post_repo.insert_comment(
        session, post_id=post_id, user_id=user_id, content=content
    )
    return _comment_to_public(comment, post_repo.get_author_name(session, user_id))


def toggle_like(session: Session, *, post_id: int, user_id: int) -> LikeResult:
    _require_published_post(session, post_id)
    liked, like_count = post_repo.toggle_like(
        session, post_id=post_id, user_id=user_id
    )
    return LikeResult(post_id=post_id, liked=liked, like_count=like_count)


# ===== 管理端(审核)=====
# 这些函数不做 published 过滤:审核的前提就是能看到未发布的内容。
# 调用方必须是已鉴权的管理接口,公开接口一律走上面的 list_posts。


def list_posts_for_review(
    session: Session, *, status: str | None = None, limit: int = 20, offset: int = 0
) -> list[PostPublic]:
    rows = post_repo.list_by_status(
        session, status=status, limit=limit, offset=offset
    )
    return [_to_public(post, author_name) for post, author_name in rows]


def review_post(session: Session, *, post_id: int, status: str) -> PostPublic:
    """把帖子改为 published 或 rejected。

    这里用真实的 404(而不是公开接口那种「未发布也当不存在」):
    管理端需要区分"这个 id 不存在"和"存在但没发布",否则无法排查。
    取值合法性由 PostReviewRequest 的 Literal 保证。
    """
    post = post_repo.get(session, post_id)
    if post is None:
        raise ResourceNotFoundError("帖子不存在")
    updated = post_repo.set_status(session, post, status)
    logger.info("帖子审核 post_id=%s status=%s", post_id, status)
    return _to_public(updated, post_repo.get_author_name(session, updated.user_id))
