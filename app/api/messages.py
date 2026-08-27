"""消息历史查询接口。

GET /api/messages?limit=20 —— 只按当前用户取最近 N 条。

认证(Authentication)只回答「你是谁」；授权(Authorization)还要回答
「你能看哪些数据」。这里通过 user_id 所有权过滤完成资源授权。
"""

from typing import Annotated

from fastapi import APIRouter, Depends, Query

from app.api.deps import get_current_user_id
from app.db import SessionDep
from app.schemas.chat import ChatMessagePublic
from app.services import chat as chat_service

router = APIRouter(prefix="/api", tags=["messages"])


@router.get("/messages", response_model=list[ChatMessagePublic])
def list_messages(
    session: SessionDep,
    _user_id: Annotated[int, Depends(get_current_user_id)],
    limit: Annotated[int, Query(ge=1, le=200)] = 20,
) -> list[ChatMessagePublic]:
    """取最近 limit 条聊天记录,需要登录。"""
    messages = chat_service.list_recent_messages(
        session, user_id=_user_id, limit=limit
    )
    return [
        ChatMessagePublic.model_validate(message, from_attributes=True)
        for message in messages
    ]
