"""
消息历史查询接口。

只提供一个 GET /api/messages?limit=20,按时间倒序拉最近 N 条。
用来给前端展示对话历史,或者供你自己在浏览器里 debug 数据是否落库成功。

这个接口能看到全部用户的聊天记录,所以必须登录才能访问——
挂上 get_current_user_id 依赖,没带有效 token 直接 401。
"""

from typing import Annotated, Sequence

from fastapi import APIRouter, Depends, Query
from sqlmodel import desc, select

from app.db import SessionDep
from app.models.message import ChatMessage
from app.services.auth import get_current_user_id

router = APIRouter(prefix="/api", tags=["messages"])


@router.get("/messages", response_model=list[ChatMessage])
def list_messages(
    session: SessionDep,
    _user_id: Annotated[int, Depends(get_current_user_id)],
    # Query(...) 用来给查询参数加上校验和文档说明:
    # - ge=1, le=200:范围限制,超出直接 422,不用自己写 if
    # - 默认值写在等号右边(= 20),不再塞进 Query 里
    limit: Annotated[int, Query(ge=1, le=200)] = 20,
) -> list[ChatMessage]:
    # SQLModel 的 select 语法:等价于 SELECT * FROM chat_messages ORDER BY created_at DESC LIMIT :limit
    # desc(ChatMessage.created_at) 表示按创建时间倒序排,最新的在前
    statement = (
        select(ChatMessage)
        .order_by(desc(ChatMessage.created_at))
        .limit(limit)
    )
    # session.exec 执行 SQL,.all() 把结果全部取出为 list
    results = session.exec(statement).all()
    return list[ChatMessage](results)
