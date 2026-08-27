"""chat_messages 表的数据访问封装。"""

from sqlmodel import Session, desc, select

from app.models.message import ChatMessage


def list_recent(session: Session, user_id: int, limit: int) -> list[ChatMessage]:
    """只取指定用户自己的最近 limit 条消息。

    user_id 过滤必须在 SQL 层完成,不能先查出全部数据再用 Python 过滤:
    后者既可能造成数据泄露,又会把不属于当前用户的数据先加载进内存。
    """
    statement = (
        select(ChatMessage)
        .where(ChatMessage.user_id == user_id)
        .order_by(desc(ChatMessage.created_at))
        .limit(limit)
    )
    return list(session.exec(statement).all())


def insert(
    session: Session, *, user_id: int, role: str, content: str
) -> ChatMessage:
    """插入一条属于指定用户的消息并提交。"""
    message = ChatMessage(user_id=user_id, role=role, content=content)
    session.add(message)
    session.commit()
    session.refresh(message)
    return message
