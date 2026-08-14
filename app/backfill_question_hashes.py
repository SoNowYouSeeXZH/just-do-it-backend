"""为历史题目补齐 content_hash。

运行:
    python -m app.backfill_question_hashes

发现内容冲突时会终止且不提交，避免自动删除或合并题目。
"""

from __future__ import annotations

from sqlmodel import Session, select

from app.db import engine
from app.models.job import Question
from app.services.question_bank import QuestionInput, question_hash


def main() -> None:
    with Session(engine) as session:
        questions = session.exec(
            select(Question).where(Question.content_hash.is_(None))
        ).all()
        existing_hashes = {
            question.content_hash
            for question in session.exec(
                select(Question).where(Question.content_hash.is_not(None))
            ).all()
            if question.content_hash
        }
        hashes: dict[str, int] = {}
        for question in questions:
            item = QuestionInput(
                qtype=question.qtype,
                prompt=question.prompt,
                options=question.options,
                answer_indices=question.answer_indices,
                explanation=question.explanation,
                source_url=question.source_url,
            )
            digest = question_hash(question.job_id, item)
            if digest in existing_hashes:
                raise RuntimeError(
                    f"发现与已有题目重复: question_id={question.id}, content_hash={digest}"
                )
            if digest in hashes:
                raise RuntimeError(
                    f"发现重复内容: question_id={hashes[digest]} 与 question_id={question.id}"
                )
            hashes[digest] = question.id or 0
            question.content_hash = digest
            session.add(question)

        session.commit()
        print(f"已为 {len(questions)} 道历史题补齐 content_hash")


if __name__ == "__main__":
    main()
