"""嵌入(embedding)调用模块。

和 llm.py 同样的结构:惰性全局客户端 + 配置缺失时显式 503。
为什么不复用 llm.py 的客户端:base_url 和 key 都可能不同
(聊天走内部网关,嵌入走智谱),复用就得给"换一个"加一堆特判。

嵌入是批量接口:一次请求带多条文本,比逐条调用便宜也快得多。
智谱 embedding-3 单批上限 64 条,这里按 64 分批。
"""

from openai import AsyncOpenAI, OpenAIError

from app.config import settings
from app.core.exceptions import ConfigurationError, UpstreamServiceError

_client: AsyncOpenAI | None = None

_BATCH_SIZE = 64


def get_client() -> AsyncOpenAI:
    """返回全局共享的嵌入客户端,首次调用时才创建。"""
    global _client
    if _client is None:
        if not settings.embedding_api_key:
            raise ConfigurationError("未配置嵌入模型 API Key，语料向量检索暂不可用")
        _client = AsyncOpenAI(
            api_key=settings.embedding_api_key,
            base_url=settings.embedding_base_url,
        )
    return _client


def reset_client_for_tests() -> None:
    """丢弃已缓存的客户端(与 llm.py 同名函数保持一致)。"""
    global _client
    _client = None


async def embed_texts(texts: list[str]) -> list[list[float]]:
    """把一批文本转成向量。返回顺序与输入一致。

    给爬虫/回填脚本用,失败直接抛 UpstreamServiceError(502):
    部分成功的批次没有任何用处——语料要么完整嵌入,要么重跑,
    存一半向量只会让检索结果时有时无。
    """
    if not texts:
        return []
    try:
        vectors: list[list[float]] = []
        for start in range(0, len(texts), _BATCH_SIZE):
            batch = texts[start : start + _BATCH_SIZE]
            response = await get_client().embeddings.create(
                model=settings.embedding_model,
                input=batch,
                dimensions=settings.embedding_dimensions,
            )
            # OpenAI 兼容接口按输入顺序返回 data,但保险起见按 index 排一次:
            # 顺序错了不会报错,只会在检索时悄悄给出"错误的段落",极难排查。
            ordered = sorted(response.data, key=lambda item: item.index)
            vectors.extend(item.embedding for item in ordered)
    except OpenAIError as exc:
        # 不回传上游原始错误,可能带 key 片段或内部地址。
        raise UpstreamServiceError("嵌入服务调用失败，请稍后重试") from exc

    if len(vectors) != len(texts):
        raise UpstreamServiceError("嵌入服务返回数量与输入不一致")
    if any(len(vec) != settings.embedding_dimensions for vec in vectors):
        # 维度对不上通常是配错了模型/维度。入库不校验的话,
        # pgvector 会在插入时才报错,而且错误信息远不如这句直观。
        raise UpstreamServiceError("嵌入结果维度与配置不一致，请检查 EMBEDDING_MODEL")
    return vectors
