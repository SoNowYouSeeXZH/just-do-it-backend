"""guide_chunks 的数据访问封装。

写入侧(爬虫):按 source_url 整页覆盖——删旧插新。
不用 ON CONFLICT DO UPDATE:那是 PG 方言,SQLite 测试库跑不了;
而且页面重新抓取后块数会变(内容增删),"对位更新"本来就不成立,
整页覆盖语义更直白,几十行数据也不心疼。

检索侧:pgvector 的余弦距离算子 `<=>` 是 PG 独有的,
SQLite 上没有对应实现,所以 search_similar 只能在 PG 环境跑
(测试套件用 SQLite,这个函数的测试见 docs/notes/21 的真机验证,
或后续用 testcontainers 起真 PG 再补)。
"""

from sqlalchemy import text
from sqlmodel import Session, select

from app.models.guide_chunk import GuideChunk


def replace_page_chunks(session: Session, chunks: list[GuideChunk]) -> None:
    """整页覆盖:删掉该 source_url 的所有旧块,插入新块。调用方控制事务提交。"""
    if not chunks:
        return
    source_url = chunks[0].source_url
    for chunk in chunks:
        if chunk.source_url != source_url:
            raise ValueError("一次只能写入同一页的块")
    existing = session.exec(
        select(GuideChunk).where(GuideChunk.source_url == source_url)
    ).all()
    for old in existing:
        session.delete(old)
    # 必须先把删除刷进数据库再插入:SQLAlchemy 的 flush 顺序是先 INSERT 后 DELETE,
    # 不 flush 的话,新旧块会带着相同的 (source_url, chunk_index) 同时在场,
    # 撞在唯一约束上。这一步也保证了"删旧插新"在同一个事务里原子完成。
    session.flush()
    session.add_all(chunks)


def count_chunks(session: Session, *, embedded_only: bool = False) -> int:
    """语料规模统计,给爬虫/管理端用。"""
    statement = select(GuideChunk)
    if embedded_only:
        statement = statement.where(GuideChunk.embedding.is_not(None))
    return len(list(session.exec(statement).all()))


def search_similar(
    session: Session,
    *,
    query_embedding: list[float],
    game_slug: str | None = None,
    top_k: int = 5,
) -> list[GuideChunk]:
    """按余弦相似度取最相近的语料块。

    `<=>` 是 pgvector 的余弦距离算符(值越小越相似)。
    game_slug 过滤在 SQL 里做:先取全量再筛,等于把向量检索变成全表扫描。

    拼两条 SQL 而不是 `(:slug IS NULL OR game_slug = :slug)`:
    psycopg 对只出现在 `IS NULL` 判断里的参数推不出类型,会报
    AmbiguousParameter(真 PG 冒烟实测);而且带 `OR NULL` 的写法
    对查询计划也不友好。
    """
    vec_literal = "[" + ",".join(str(value) for value in query_embedding) + "]"
    if game_slug:
        statement = text(
            """
            SELECT id FROM guide_chunks
            WHERE embedding IS NOT NULL AND game_slug = :game_slug
            ORDER BY embedding <=> CAST(:vec AS vector)
            LIMIT :top_k
            """
        ).bindparams(game_slug=game_slug, vec=vec_literal, top_k=top_k)
    else:
        statement = text(
            """
            SELECT id FROM guide_chunks
            WHERE embedding IS NOT NULL
            ORDER BY embedding <=> CAST(:vec AS vector)
            LIMIT :top_k
            """
        ).bindparams(vec=vec_literal, top_k=top_k)
    ids = [row[0] for row in session.exec(statement).all()]  # type: ignore[index]
    if not ids:
        return []
    # 再按主键取完整 ORM 对象;上一步 SQL 只取 id,避免把大文本列拉进排序阶段。
    return list(session.exec(select(GuideChunk).where(GuideChunk.id.in_(ids))).all())
