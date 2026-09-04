"""攻略语料分块表(pgvector)。

RAG 主线的第三种检索形态:预爬语料存本地,用向量相似度检索。
和现有"实时抓 wiki"的差别:
- 实时抓取受 WAF 限流影响,面试现场答不出是硬伤;语料入库后检索不依赖外网。
- 语料分块(chunk)而不是整页:嵌入模型对长文本会稀释语义,
  一个 8000 字的攻略页变成 20 个几百字的块,检索才能命中到"那一段"。

一页攻略拆成多行,它们共享 source_url/title,靠 chunk_index 保持顺序。
(source_url, chunk_index) 唯一:重爬同一页时走"删旧插新"实现幂等。
"""

from datetime import datetime

from pgvector.sqlalchemy import Vector
from sqlmodel import Field, SQLModel, UniqueConstraint

# 与 settings.embedding_dimensions 一致。列维度写死而不是跟着配置走:
# HNSW 索引要求列有固定维度,而且向量维度由模型决定,不是部署参数。
# 换嵌入模型(维度不同)时需要改这里 + 重新生成迁移 + 重爬语料。
EMBEDDING_DIMENSIONS = 1024


class GuideChunk(SQLModel, table=True):
    __tablename__ = "guide_chunks"  # pyright: ignore[reportAssignmentType, reportUnannotatedClassAttribute]
    __table_args__ = (
        UniqueConstraint("source_url", "chunk_index", name="uq_guide_chunks_url_index"),
    )

    id: int | None = Field(default=None, primary_key=True)
    # 语料按游戏分区,检索时可限定 game_slug 缩小候选集。
    game_slug: str = Field(max_length=32, index=True)
    # 来源页 URL,作为引用链接展示给用户。
    source_url: str = Field(max_length=500)
    title: str = Field(max_length=200)
    # 第几块。同一页的块按 0,1,2... 排列。
    chunk_index: int
    chunk_text: str
    # 向量。允许为 NULL:先把语料爬入库,再补算嵌入(两步可以分开跑)。
    embedding: list[float] | None = Field(
        default=None,
        sa_type=Vector(EMBEDDING_DIMENSIONS),
    )
    created_at: datetime = Field(default_factory=datetime.now)
