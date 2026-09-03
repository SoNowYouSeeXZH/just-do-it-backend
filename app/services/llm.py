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
from app.core.exceptions import ConfigurationError

# 全局复用一个客户端:AsyncOpenAI 内部维护 HTTP 连接池,
# 每次请求都 new 一个会白白重建连接。
#
# 但**不在 import 期就创建**。原来是模块级直接 `AsyncOpenAI(api_key=...)`,
# 有两个问题:
# 1. 测试里想换成假客户端,只能改模块私有变量,顺序还得赶在 import 之前;
# 2. 没配 key 时不会报错——AsyncOpenAI 的构造函数不校验 key,
#    于是问题被推迟到真正发请求时才以 401 的形式冒出来,像是上游故障。
# 改成惰性工厂后,缺配置在第一次调用时就以 ConfigurationError(503) 显式失败,
# 运维看到 503 就知道去查部署配置,而不是翻业务代码找 bug。
_client: AsyncOpenAI | None = None


def get_client() -> AsyncOpenAI:
    """返回全局共享的客户端,首次调用时才真正创建。"""
    global _client
    if _client is None:
        if not settings.llm_api_key:
            raise ConfigurationError("未配置大模型 API Key，AI 问答功能暂不可用")
        _client = AsyncOpenAI(
            api_key=settings.llm_api_key,
            base_url=settings.llm_base_url,
        )
    return _client


def reset_client_for_tests() -> None:
    """丢弃已缓存的客户端。

    命名和 app/core/cache.py 的同名函数保持一致:两处都是"模块级单例 +
    惰性创建"的结构,测试里换掉配置后必须清一次缓存才能生效。
    """
    global _client
    _client = None


# 系统提示词:决定模型的"人设"和回答风格,相当于给它的常驻指令。
SYSTEM_PROMPT = "你是一个乐于助人的中文 AI 助手,回答简洁准确。"


async def ask_llm(message: str) -> str:
    """把用户的一句话发给大模型,返回模型回复的纯文本。"""
    # messages 是一个数组,每项有 role(角色)和 content(内容):
    # - system:系统指令   - user:用户说的话   - assistant:模型之前的回复
    # 目前只传当前这一句,所以模型"没有记忆";加上历史消息就能多轮对话(后续阶段做)。
    completion = await get_client().chat.completions.create(
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
    stream = await get_client().chat.completions.create(
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
