"""题库批量写入服务：校验、规范化、去重和事务处理。"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from app.models.job import Job, Question

_WHITESPACE_RE = re.compile(r"\s+")


class JobNotFoundError(Exception):
    """批量导入目标职业不存在。"""


class QuestionInput(BaseModel):
    """AI 提交的单道题目，不暴露数据库自增 id 和 content_hash。"""

    model_config = ConfigDict(extra="forbid")

    qtype: Literal["single", "multi"]
    prompt: str
    options: list[str]
    answer_indices: list[int]
    explanation: str
    source_url: str | None = None


class BatchQuestionsRequest(BaseModel):
    """批量接口请求体。"""

    model_config = ConfigDict(extra="forbid")

    job_id: str = Field(min_length=1, max_length=32)
    questions: list[QuestionInput] = Field(min_length=1, max_length=100)


class QuestionResult(BaseModel):
    index: int
    status: Literal["created", "skipped", "failed"]
    question_id: int | None = None
    reason: str | None = None


class BatchQuestionsResponse(BaseModel):
    total: int
    created: int
    skipped: int
    failed: int
    results: list[QuestionResult]


def normalize_text(value: str) -> str:
    """生成用于展示和去重的稳定文本。"""

    normalized = unicodedata.normalize("NFC", value).strip()
    return _WHITESPACE_RE.sub(" ", normalized)


def question_hash(job_id: str, question: QuestionInput) -> str:
    """按固定 JSON 规则生成题目内容指纹。"""

    canonical = {
        "job_id": normalize_text(job_id),
        "qtype": normalize_text(question.qtype),
        "prompt": normalize_text(question.prompt),
        "options": [normalize_text(option) for option in question.options],
    }
    payload = json.dumps(
        canonical,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def validate_question(question: QuestionInput) -> str | None:
    """执行无法仅靠 Pydantic 表达的单题业务校验。"""

    if not (2 <= len(question.options) <= 6):
        return "options 数量必须在 2 到 6 个之间"
    if any(not normalize_text(option) for option in question.options):
        return "options 不能包含空字符串"
    if not normalize_text(question.prompt):
        return "prompt 不能为空"
    if len(normalize_text(question.prompt)) > 512:
        return "prompt 不能超过 512 个字符"
    if not normalize_text(question.explanation):
        return "explanation 不能为空"
    if len(normalize_text(question.explanation)) > 1024:
        return "explanation 不能超过 1024 个字符"
    if question.source_url is not None and len(question.source_url) > 512:
        return "source_url 不能超过 512 个字符"
    if len(set(question.answer_indices)) != len(question.answer_indices):
        return "answer_indices 不能包含重复下标"
    if question.qtype == "single" and len(question.answer_indices) != 1:
        return "single 题必须且只能有一个正确答案"
    if question.qtype == "multi" and len(question.answer_indices) < 1:
        return "multi 题至少需要一个正确答案"
    if any(index < 0 or index >= len(question.options) for index in question.answer_indices):
        return "answer_indices 包含越界下标"
    return None


def _existing_hashes(session: Session, job_id: str) -> set[str]:
    return {
        question.content_hash
        for question in session.exec(
            select(Question).where(
                Question.job_id == job_id,
                Question.content_hash.is_not(None),
            )
        ).all()
        if question.content_hash
    }


def _legacy_question_hashes(session: Session, job_id: str) -> set[str]:
    """兼容历史 seed 数据的 NULL hash，迁移完成后通常为空。"""

    hashes: set[str] = set()
    legacy_questions = session.exec(
        select(Question).where(
            Question.job_id == job_id,
            Question.content_hash.is_(None),
        )
    ).all()
    for question in legacy_questions:
        canonical = QuestionInput(
            qtype=question.qtype,
            prompt=question.prompt,
            options=question.options,
            answer_indices=question.answer_indices,
            explanation=question.explanation,
            source_url=question.source_url,
        )
        hashes.add(question_hash(job_id, canonical))
    return hashes


def _is_content_hash_conflict(exc: IntegrityError) -> bool:
    """只把 content_hash 唯一键冲突识别为重复题。"""

    message = str(exc.orig).lower()
    return (
        "content_hash" in message
        or "questions.content_hash" in message
        or ("duplicate entry" in message and "questions" in message)
    )


def batch_create_questions(
    session: Session,
    request: BatchQuestionsRequest,
) -> BatchQuestionsResponse:
    """批量写入题目：业务错误逐题返回，数据库异常整批回滚。"""

    if session.get(Job, request.job_id) is None:
        raise JobNotFoundError("职业不存在")

    results: list[QuestionResult] = []
    known_hashes = _existing_hashes(session, request.job_id)
    known_hashes.update(_legacy_question_hashes(session, request.job_id))
    created = skipped = failed = 0

    try:
        for index, item in enumerate(request.questions):
            reason = validate_question(item)
            if reason:
                results.append(QuestionResult(index=index, status="failed", reason=reason))
                failed += 1
                continue

            digest = question_hash(request.job_id, item)
            if digest in known_hashes:
                results.append(
                    QuestionResult(index=index, status="skipped", reason="题目已存在")
                )
                skipped += 1
                continue

            normalized_item = item.model_copy(
                update={
                    "prompt": normalize_text(item.prompt),
                    "options": [normalize_text(option) for option in item.options],
                    "explanation": normalize_text(item.explanation),
                    "answer_indices": sorted(item.answer_indices),
                }
            )
            question = Question(
                job_id=request.job_id,
                qtype=normalized_item.qtype,
                prompt=normalized_item.prompt,
                options=normalized_item.options,
                answer_indices=normalized_item.answer_indices,
                explanation=normalized_item.explanation,
                source_url=normalized_item.source_url,
                content_hash=digest,
            )

            try:
                with session.begin_nested():
                    session.add(question)
                    session.flush()
            except IntegrityError as exc:
                if not _is_content_hash_conflict(exc):
                    raise
                results.append(
                    QuestionResult(index=index, status="skipped", reason="并发下题目已存在")
                )
                skipped += 1
                continue

            known_hashes.add(digest)
            results.append(
                QuestionResult(index=index, status="created", question_id=question.id)
            )
            created += 1

        session.commit()
    except Exception:
        session.rollback()
        raise

    return BatchQuestionsResponse(
        total=len(request.questions),
        created=created,
        skipped=skipped,
        failed=failed,
        results=results,
    )
