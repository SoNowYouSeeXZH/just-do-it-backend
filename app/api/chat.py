"""
聊天接口模块。

阶段 3 已接入真实大模型,支持两种返回方式:
- 普通模式:等模型全部生成完,一次性返回 JSON
- 流式模式(stream=true):用 SSE 边生成边推送,前端可做打字机效果

阶段 4 接入 MySQL:
- 每次对话把 user 输入和 assistant 回复各存一行到 chat_messages 表
- 存库失败不影响接口本身返回(数据库故障不该阻塞用户对话)
"""

import json
import logging

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from openai import OpenAIError
from pydantic import BaseModel
from sqlmodel import Session

from app.db import SessionDep
from app.models.message import ChatMessage
from app.services.llm import ask_llm, ask_llm_stream

# APIRouter 相当于"子路由",把聊天相关的接口聚在一起,
# 最后在 main.py 里统一挂载。类比前端框架里的路由分模块。
router = APIRouter(prefix="/api", tags=["chat"])

# 用 logger 记录存库失败,而不是直接 print——生产上能被日志系统采集
logger = logging.getLogger(__name__)


# ===== 定义请求体的数据结构 =====
# 继承 BaseModel 后,FastAPI 会自动校验前端传来的 JSON:
# 少字段、类型不对都会自动返回 422 错误,不用自己写校验代码。
class ChatRequest(BaseModel):
    message: str  # 用户发来的这句话
    stream: bool = False  # 是否流式返回;有默认值,所以前端不传也不会 422


# ===== 定义响应体的数据结构 =====
class ChatResponse(BaseModel):
    reply: str  # 后端回复的内容


def _save_message(session: Session, role: str, content: str) -> None:
    """把一条消息写进数据库。

    单独抽出来是为了:
    1. 复用(user 和 assistant 都要存)
    2. 用 try/except 隔离存库异常,不影响主流程
       —— 就算 MySQL 挂了,对话本身还是能正常给用户返回
    """
    try:
        session.add(ChatMessage(role=role, content=content))
        session.commit()
    except Exception as exc:  # noqa: BLE001 —— 存库失败原因不重要,记日志继续
        session.rollback()
        logger.warning("保存消息到数据库失败: %s", exc)


async def _sse_events(message: str, session: Session):
    """把模型输出的文本片段包装成 SSE 事件流。

    SSE(Server-Sent Events)是浏览器原生支持的单向推送协议,格式很朴素:
    每条消息写成 "data: 内容\\n\\n",连续两个换行表示一条结束。
    这里再用 JSON 包一层,是为了让回复内容里的换行不会破坏 SSE 的分帧。

    流式模式下,我们把所有 delta 拼成完整回复,等流结束后再一次性存库,
    避免每个 token 都写一次数据库。
    """
    full_reply_parts: list[str] = []
    try:
        async for delta in ask_llm_stream(message):
            full_reply_parts.append(delta)
            yield f"data: {json.dumps({'delta': delta}, ensure_ascii=False)}\n\n"
    except OpenAIError as exc:
        # 注意:流一旦开始发送,HTTP 状态码早就定成 200 了,没法再改成 502。
        # 所以只能在流里塞一条错误事件,让前端自己识别处理。
        yield f"data: {json.dumps({'error': str(exc)}, ensure_ascii=False)}\n\n"
    else:
        # 只有模型正常跑完才落库(带 else 分支,发生异常时跳过)
        full_reply = "".join(full_reply_parts)
        if full_reply:
            _save_message(session, "assistant", full_reply)
    # 约定一个结束标记,前端收到就可以收尾(和 OpenAI 官方接口的约定一致)。
    yield "data: [DONE]\n\n"


# @router.post 表示这是一个 POST 接口,路径为 /api/chat
# response_model=ChatResponse 只约束非流式那条分支的返回结构
#
# session: SessionDep
#   ↑ SessionDep = Annotated[Session, Depends(get_session)],定义在 app/db.py。
#   FastAPI 的依赖注入,每次请求进来时会调用 get_session 拿一个数据库会话。
#   类比前端 hooks:接口拿到"当前请求的 db 句柄",用完自动释放。
@router.post("/chat", response_model=ChatResponse)
async def chat(req: ChatRequest, session: SessionDep):
    # 先落库用户这条(流式和非流式都要落,所以放在分支之前)
    _save_message(session, "user", req.message)

    # 流式模式:返回 StreamingResponse。
    # 返回值是 Response 对象时,FastAPI 会跳过 response_model 校验直接透传。
    if req.stream:
        return StreamingResponse(
            _sse_events(req.message, session),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                # 关掉 nginx 一类反向代理的缓冲,否则流会被攒着一起发,失去实时性
                "X-Accel-Buffering": "no",
            },
        )

    # async def:异步函数,和前端 JS 的 async/await 用法几乎一样。
    # await 期间(等模型返回,通常几秒)服务器不会被阻塞,可以继续处理别人的请求。
    try:
        reply_text: str = await ask_llm(req.message)
    except OpenAIError as exc:
        # 模型服务出错(Key 无效、余额不足、网络超时等)属于"上游服务故障",
        # 用 502 返回,并把原因放进 detail 方便前端调试。
        # HTTPException 是 FastAPI 提供的"主动返回错误响应"的方式。
        raise HTTPException(status_code=502, detail=f"大模型调用失败: {exc}") from exc

    # 模型正常返回后再落库 assistant 的回复
    _save_message(session, "assistant", reply_text)

    return ChatResponse(reply=reply_text)
