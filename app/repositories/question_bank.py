"""题库写入的数据访问封装。

从 services/question_bank.py 里抽出来的三个查询。
业务层保留的是「怎么算重复、怎么处理冲突」,这里只负责取数和写入。
"""

from sqlmodel import Session, select

from app.models.job import Question


def existing_hashes(session: Session, job_id: str) -> set[str]:
    """取某职业下已有的内容指纹集合。

    只查有 hash 的行。历史 seed 数据的 content_hash 是 NULL,
    由 legacy_questions 单独处理。
    """
    rows = session.exec(
        select(Question.content_hash).where(
            Question.job_id == job_id,
            Question.content_hash.is_not(None),  # pyright: ignore[reportAttributeAccessIssue]
        )
    ).all()
    return {row for row in rows if row}


def legacy_questions(session: Session, job_id: str) -> list[Question]:
    """取某职业下 content_hash 为 NULL 的历史题目。

    这些是早期 seed 灌入的数据,没有指纹。业务层会现场算一遍它们的 hash
    并纳入去重集合,避免重复导入。数据迁移完成后这个查询通常返回空列表。
    """
    return list(
        session.exec(
            select(Question).where(
                Question.job_id == job_id,
                Question.content_hash.is_(None),  # pyright: ignore[reportAttributeAccessIssue]
            )
        ).all()
    )
