"""posts / post_comments / post_likes 的数据访问封装。

所有权和审核状态过滤都必须在 SQL 层完成:先查全部再用 Python 过滤
既可能泄露未审核内容,也会把不该加载的数据读进内存。

列表查询用 JOIN 一次带出作者名:如果只返回 Post 再逐条查用户,
20 条帖子就是 21 次查询(N+1),这是最典型的一类性能坑。
"""

from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, desc, select

from app.models.post import (
    POST_STATUS_PUBLISHED,
    Post,
    PostComment,
    PostLike,
)
from app.models.user import Users


def list_published(
    session: Session,
    *,
    game_slug: str | None,
    city: str | None,
    limit: int,
    offset: int = 0,
) -> list[tuple[Post, str]]:
    """按发布时间倒序分页取帖子,返回 (帖子, 作者名) 列表。"""
    statement = (
        select(Post, Users.username)
        .join(Users, Users.id == Post.user_id)
        .where(Post.status == POST_STATUS_PUBLISHED)
    )
    if game_slug:
        statement = statement.where(Post.game_slug == game_slug)
    if city:
        statement = statement.where(Post.city == city)
    # 第二排序键是主键:只按 created_at 排序时,同一秒内写入的帖子顺序不稳定,
    # 翻页会出现同一条重复出现或被跳过。
    statement = (
        statement.order_by(desc(Post.created_at), desc(Post.id))
        .offset(offset)
        .limit(limit)
    )
    return [(post, username) for post, username in session.exec(statement).all()]


def get(session: Session, post_id: int) -> Post | None:
    return session.get(Post, post_id)


def list_by_status(
    session: Session, *, status: str | None, limit: int, offset: int = 0
) -> list[tuple[Post, str]]:
    """管理端用:按状态取帖子(status=None 表示不限状态)。

    和 list_published 分开而不是加个参数:公开列表「只能看到 published」
    是一条安全约束,不应该由调用方传参决定——传错一次就是数据泄露。
    """
    statement = select(Post, Users.username).join(Users, Users.id == Post.user_id)
    if status:
        statement = statement.where(Post.status == status)
    statement = (
        statement.order_by(desc(Post.created_at), desc(Post.id))
        .offset(offset)
        .limit(limit)
    )
    return [(post, username) for post, username in session.exec(statement).all()]


def set_status(session: Session, post: Post, status: str) -> Post:
    post.status = status
    session.add(post)
    session.commit()
    session.refresh(post)
    return post


def get_author_name(session: Session, user_id: int) -> str:
    """取作者名。用户被删除时给一个占位名,而不是让整个响应失败。"""
    user = session.get(Users, user_id)
    return user.username if user is not None else "已注销用户"


def list_by_user(
    session: Session, *, user_id: int, limit: int, offset: int = 0
) -> list[tuple[Post, str]]:
    """「我的帖子」:作者本人可以看到自己所有状态的帖子(含待审、被驳回)。

    user_id 过滤同样写在 SQL 里。先查全部再在 Python 里筛作者,
    既把别人的数据读进了内存,也很容易在某个分支上漏掉过滤。
    """
    statement = (
        select(Post, Users.username)
        .join(Users, Users.id == Post.user_id)
        .where(Post.user_id == user_id)
        .order_by(desc(Post.created_at), desc(Post.id))
        .offset(offset)
        .limit(limit)
    )
    return [(post, username) for post, username in session.exec(statement).all()]


def insert(
    session: Session,
    *,
    user_id: int,
    game_slug: str,
    title: str,
    content: str,
    city: str | None,
    status: str,
) -> Post:
    post = Post(
        user_id=user_id,
        game_slug=game_slug,
        title=title,
        content=content,
        city=city,
        status=status,
    )
    session.add(post)
    session.commit()
    session.refresh(post)
    return post


def list_comments(
    session: Session, post_id: int, limit: int, offset: int = 0
) -> list[tuple[PostComment, str]]:
    """取某帖的评论,返回 (评论, 作者名) 列表。"""
    statement = (
        select(PostComment, Users.username)
        .join(Users, Users.id == PostComment.user_id)
        .where(PostComment.post_id == post_id)
        .order_by(desc(PostComment.created_at), desc(PostComment.id))
        .offset(offset)
        .limit(limit)
    )
    return [(comment, username) for comment, username in session.exec(statement).all()]


def insert_comment(
    session: Session, *, post_id: int, user_id: int, content: str
) -> PostComment:
    comment = PostComment(post_id=post_id, user_id=user_id, content=content)
    post = session.get(Post, post_id)
    if post is not None:
        post.comment_count += 1
        session.add(post)
    session.add(comment)
    session.commit()
    session.refresh(comment)
    return comment


def toggle_like(session: Session, *, post_id: int, user_id: int) -> tuple[bool, int]:
    """点赞/取消点赞。返回 (是否已点赞, 最新点赞数)。

    插入冲突走唯一约束而不是「先查再插」:并发双击时前者才能真正兜住。
    """
    existing = session.exec(
        select(PostLike).where(
            PostLike.post_id == post_id, PostLike.user_id == user_id
        )
    ).first()
    post = session.get(Post, post_id)

    if existing is not None:
        session.delete(existing)
        if post is not None and post.like_count > 0:
            post.like_count -= 1
            session.add(post)
        session.commit()
        return False, post.like_count if post else 0

    session.add(PostLike(post_id=post_id, user_id=user_id))
    if post is not None:
        post.like_count += 1
        session.add(post)
    try:
        session.commit()
    except IntegrityError:
        # 并发下另一个请求已经插入了同一条点赞,回滚后按「已点赞」返回。
        session.rollback()
        current = session.get(Post, post_id)
        return True, current.like_count if current else 0
    return True, post.like_count if post else 0
