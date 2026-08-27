"""聊天业务逻辑:对话编排 + 消息持久化。

这个模块承担了一个明确的业务决策,值得单独说明:

**存库失败不能阻塞用户对话。**

对话是核心功能,聊天记录是辅助功能。MySQL 挂了的时候,用户应该还能
正常和模型对话,只是这次的记录丢了。所以 _try_save 里吞掉异常只记日志。

这个决策原来写在 api/chat.py 里。搬到这一层的意义是:它是一条业务规则
(「哪个功能可以降级」),不是接口实现细节。
"""

import json
import logging
from collections.abc import AsyncIterator

from openai import OpenAIError
from sqlmodel import Session

from app.core.exceptions import UpstreamServiceError
from app.models.message import ChatMessage
from app.repositories import message as message_repo
from app.services.llm import ask_llm, ask_llm_stream

logger = logging.getLogger(__name__)


def list_recent_messages(
    session: Session, *, user_id: int, limit: int
) -> list[ChatMessage]:
    """取当前用户最近 limit 条聊天记录。"""
    return message_repo.list_recent(session, user_id, limit)


def _try_save(session: Session, user_id: int, role: str, content: str) -> None:
    """尽力保存消息,失败只记日志不抛异常。

    这里是全项目唯一一处刻意吞掉异常的地方,理由见模块文档字符串。
    注意用 warning 而不是 error:这是一个已知的、可接受的降级路径,
    不是需要立刻响应的故障。
    """
    try:
        message_repo.insert(session, user_id=user_id, role=role, content=content)
    except Exception as exc:  # noqa: BLE001 —— 存库失败不影响对话主流程
        session.rollback()
        logger.warning("保存消息到数据库失败: %s", exc)


async def reply(session: Session, *, user_id: int, message: str) -> str:
    """非流式对话:等模型全部生成完再返回,同时落库两条消息。"""
    _try_save(session, user_id, "user", message)

    try:
        reply_text = await ask_llm(message)
    except OpenAIError as exc:
        # 只记日志时才带上原始异常细节;对外抛出的 message 保持通用,
        # 避免把 API Key 片段、上游内部地址之类的信息泄露给调用方。
        logger.error("大模型调用失败: %s", exc, exc_info=True)
        raise UpstreamServiceError("智能问答服务暂时不可用,请稍后再试") from exc

    _try_save(session, user_id, "assistant", reply_text)
    return reply_text


async def stream_reply(
    session: Session, *, user_id: int, message: str
) -> AsyncIterator[str]:
    """流式对话:把模型输出的片段包装成 SSE 事件。

    SSE(Server-Sent Events)格式很朴素:每条消息写成 "data: 内容\\n\\n",
    连续两个换行表示一条结束。这里再用 JSON 包一层,是为了让回复内容里
    的换行不会破坏 SSE 的分帧。

    为什么流式模式的错误不抛异常?
    因为流一旦开始发送,HTTP 状态码早就定成 200 了,没法再改成 502。
    只能在流里塞一条错误事件,让前端自己识别。这是 SSE 的固有约束。
    """
    _try_save(session, user_id, "user", message)

    parts: list[str] = []
    try:
        async for delta in ask_llm_stream(message):
            parts.append(delta)
            yield f"data: {json.dumps({'delta': delta}, ensure_ascii=False)}\n\n"
    except OpenAIError as exc:
        logger.error("大模型流式调用失败: %s", exc, exc_info=True)
        # 同样不把上游原始错误透传给前端
        error_payload = {"error": "智能问答服务暂时不可用,请稍后再试"}
        yield f"data: {json.dumps(error_payload, ensure_ascii=False)}\n\n"
    else:
        # 只有模型正常跑完才落库,避免把半截回复存成完整回复
        full_reply = "".join(parts)
        if full_reply:
            _try_save(session, user_id, "assistant", full_reply)

    # 结束标记,和 OpenAI 官方接口的约定一致
    yield "data: [DONE]\n\n"
