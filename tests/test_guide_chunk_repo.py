"""guide_chunks 仓储的写入侧测试(SQLite 内存库)。

检索侧(search_similar)依赖 pgvector 的 `<=>` 算符,SQLite 没有,
只能对着真 PG 验证——那部分在爬虫跑通后用真机数据冒烟,不在这补假测试。
"""

from sqlmodel import Session, select

from app.models.guide_chunk import GuideChunk
from app.repositories import guide_chunk as chunk_repo


def _chunks(url: str, count: int) -> list[GuideChunk]:
    return [
        GuideChunk(
            game_slug="ys",
            source_url=url,
            title="纳塔探索",
            chunk_index=i,
            chunk_text=f"第 {i} 段内容",
        )
        for i in range(count)
    ]


def test_replace_page_chunks_inserts(session: Session) -> None:
    chunks = _chunks("https://wiki.example/nata", 3)

    chunk_repo.replace_page_chunks(session, chunks)
    session.commit()

    stored = session.exec(select(GuideChunk)).all()
    assert len(stored) == 3
    assert {item.chunk_index for item in stored} == {0, 1, 2}


def test_replace_page_chunks_is_idempotent_on_recrawl(session: Session) -> None:
    # 第一次爬:3 块
    chunk_repo.replace_page_chunks(session, _chunks("https://wiki.example/zzz", 3))
    session.commit()
    # 重爬同一页:页面变长了,5 块
    chunk_repo.replace_page_chunks(session, _chunks("https://wiki.example/zzz", 5))
    session.commit()

    stored = session.exec(select(GuideChunk)).all()
    # 旧的 3 块被整页覆盖,只剩新的 5 块——不会累积重复
    assert len(stored) == 5
    assert all(item.source_url == "https://wiki.example/zzz" for item in stored)


def test_replace_page_chunks_rejects_mixed_pages(session: Session) -> None:
    mixed = [
        GuideChunk(
            game_slug="ys", source_url="https://a", title="t", chunk_index=0, chunk_text="x"
        ),
        GuideChunk(
            game_slug="sr",
            source_url="https://b",
            title="t",
            chunk_index=0,
            chunk_text="y",
        ),
    ]

    try:
        chunk_repo.replace_page_chunks(session, mixed)
    except ValueError:
        pass  # noqa: PT017 - 期望抛出,不许整批写入来源混杂的块
    else:
        raise AssertionError("混页写入应抛 ValueError")


def test_count_chunks(session: Session) -> None:
    embedded = GuideChunk(
        game_slug="ys",
        source_url="https://wiki.example/e",
        title="t",
        chunk_index=0,
        chunk_text="x",
        embedding=[0.1] * 1024,
    )
    plain = _chunks("https://wiki.example/p", 2)

    session.add(embedded)
    chunk_repo.replace_page_chunks(session, plain)
    session.commit()

    assert chunk_repo.count_chunks(session) == 3
    assert chunk_repo.count_chunks(session, embedded_only=True) == 1
