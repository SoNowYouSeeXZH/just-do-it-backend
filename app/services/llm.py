"""
大模型调用模块(阶段 3)。

为什么单独抽一个文件:
接口层(api/chat.py)只负责"收请求、返响应",具体怎么调模型属于业务逻辑。
分开后,以后接入 RAG、换模型、加对话历史,都只改这里,接口层不用动。
类比前端:api/chat.py 是组件,这个文件是 service/请求层。
"""

from collections.abc import AsyncIterator

from openai import AsyncOpenAI

from app.config import settings

# 创建一个全局复用的客户端。
# AsyncOpenAI 是异步版本(对应 async/await),内部维护 HTTP 连接池,
# 所以只创建一次、全局复用,不要每次请求都 new 一个——那样会白白重建连接。
_client: AsyncOpenAI = AsyncOpenAI(
    api_key=settings.llm_api_key,
    base_url=settings.llm_base_url,
)

# 系统提示词:决定模型的"人设"和回答风格,相当于给它的常驻指令。
SYSTEM_PROMPT = "你是一个乐于助人的中文 AI 助手,回答简洁准确。"

async def ask_llm(message: str) -> str:
    """把用户的一句话发给大模型,返回模型回复的纯文本。"""
    # messages 是一个数组,每项有 role(角色)和 content(内容):
    # - system:系统指令   - user:用户说的话   - assistant:模型之前的回复
    # 目前只传当前这一句,所以模型"没有记忆";加上历史消息就能多轮对话(后续阶段做)。
    completion = await _client.chat.completions.create(
        model=settings.llm_model,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": message},
        ],
    )
    
    # 返回结构里 choices 是候选回复列表(默认只有一条),取第一条的内容。
    # content 在极少数情况下可能为 None,所以用 or "" 兜底,保证返回值一定是 str。
    return completion.choices[0].message.content or ""


async def ask_llm_stream(message: str) -> AsyncIterator[str]:
    """流式版本:模型边生成边返回,每次 yield 一小段新增文本。

    函数里同时出现 async 和 yield,这叫"异步生成器"。
    调用方用 `async for delta in ask_llm_stream(...)` 逐段消费,
    类比前端 ReadableStream 的 reader.read() 循环。
    """
    stream = await _client.chat.completions.create(
        model=settings.llm_model,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": message},
        ],
        stream=True,  # 就是这个参数决定服务端要不要流式返回
    )

    async for chunk in stream:
        # 关键区别:流式模式下每个 chunk 装的是 delta(本次新增的一小段),
        # 而不是 message(完整回复)。把所有 delta 依次拼起来才等于完整回复。
        # 流的首尾可能出现 choices 为空的包,不跳过会报 IndexError。
        if not chunk.choices:
            continue
        delta: str | None = chunk.choices[0].delta.content
        # delta 可能是 None(比如某一帧只带了角色信息或思考内容),空值不往外发。
        if delta:
            yield delta
