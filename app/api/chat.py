"""聊天接口模块。

两种返回方式:
- 普通模式:等模型全部生成完,一次性返回 JSON
- 流式模式(stream=true):用 SSE 边生成边推送,前端可做打字机效果

聊天接口需要登录,因为每条消息必须绑定当前用户。
重构后这一层不再包含:消息落库逻辑、SSE 事件拼装、大模型异常翻译。
它们分别在 repositories/message.py 和 services/chat.py 里。
"""

from typing import Annotated

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse

from app.api.deps import get_current_user_id

from app.db import SessionDep
from app.schemas.chat import ChatRequest, ChatResponse
from app.services import chat as chat_service

router = APIRouter(prefix="/api", tags=["chat"])


@router.post("/chat", response_model=ChatResponse)
async def chat(
    req: ChatRequest,
    session: SessionDep,
    user_id: Annotated[int, Depends(get_current_user_id)],
):
    """对话接口。stream=true 时返回 SSE 流,否则返回完整 JSON。"""
    if req.stream:
        # 返回 Response 对象时 FastAPI 会跳过 response_model 校验直接透传
        return StreamingResponse(
            chat_service.stream_reply(session, user_id=user_id, message=req.message),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                # 关掉 nginx 一类反向代理的缓冲,否则流会被攒着一起发
                "X-Accel-Buffering": "no",
            },
        )

    reply_text = await chat_service.reply(session, user_id=user_id, message=req.message)
    return ChatResponse(reply=reply_text)
