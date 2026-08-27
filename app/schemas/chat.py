"""聊天相关的请求/响应 DTO。"""

from datetime import datetime

from pydantic import BaseModel, Field


class ChatRequest(BaseModel):
    """对话请求。

    message 加了长度约束:原来没有上限,意味着可以提交一个几 MB 的字符串,
    它会被直接转发给大模型——按 token 计费的接口上这是一个成本漏洞,
    也是一种廉价的 DoS 手段。
    """

    message: str = Field(min_length=1, max_length=2000)
    stream: bool = False


class ChatResponse(BaseModel):
    """非流式模式的对话响应。"""

    reply: str


class ChatMessagePublic(BaseModel):
    """一条历史消息的公开字段。"""

    id: int
    role: str
    content: str
    created_at: datetime
