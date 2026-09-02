"""题库批量写入服务:校验、规范化、去重和事务处理。

本模块保留的是纯业务规则:
- 什么样的题目算合法(validate_question)
- 怎么算「同一道题」(question_hash)
- 冲突时是跳过还是失败

DTO 已移到 schemas/question_bank.py,数据查询已移到 repositories/question_bank.py。
异常改用 core.exceptions.ResourceNotFoundError,不再自定义 JobNotFoundError——
「资源不存在」是通用概念,没必要每个模块造一个。

写入成功后会主动失效职业相关缓存(见 batch_create_questions 末尾),
否则新增的题目要等缓存 TTL 过期才会体现在 /api/jobs 的题目数里。
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata

from sqlalchemy.exc import IntegrityError
from sqlmodel import Session

from app.core import cache, cache_keys
from app.core.exceptions import ResourceNotFoundError
from app.models.job import Question
from app.repositories import job as job_repo
from app.repositories import question_bank as bank_repo
from app.schemas.question_bank import (
    BatchQuestionsRequest,
    BatchQuestionsResponse,
    QuestionInput,
    QuestionResult,
)

_WHITESPACE_RE = re.compile(r"\s+")


def normalize_text(value: str) -> str:
    """生成用于展示和去重的稳定文本。

    两步归一化:
    1. NFC —— 把 Unicode 组合字符统一成合成形式,否则视觉相同的字
       可能有不同字节表示,算出的 hash 就不同
    2. 空白折叠 —— 连续空格/换行压成一个空格

    做这些的目的是让「肉眼看着一样的题」算出同一个指纹。
    """
    normalized = unicodedata.normalize("NFC", value).strip()
    return _WHITESPACE_RE.sub(" ", normalized)


def question_hash(job_id: str, question: QuestionInput) -> str:
    """按固定 JSON 规则生成题目内容指纹。

    只用 job_id + qtype + prompt + options 参与计算,不含 explanation——
    因为「同一道题配了不同解析」应该算重复题,而不是两道题。

    sort_keys=True 和固定 separators 是为了保证同样的内容永远
    序列化成同样的字节串,否则 dict 顺序变化会导致 hash 不稳定。
    """
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
    """执行无法仅靠 Pydantic 表达的单题业务校验。

    返回 None 表示通过,否则返回失败原因。
    为什么不抛异常?因为批量导入时要逐题报告结果,
    一道题不合格不该中断其余 99 道。
    """
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
    if any(
        index < 0 or index >= len(question.options)
        for index in question.answer_indices
    ):
        return "answer_indices 包含越界下标"
    return None


def _legacy_hashes(session: Session, job_id: str) -> set[str]:
    """给历史 NULL hash 数据现场补算指纹,纳入去重集合。"""
    hashes: set[str] = set()
    for question in bank_repo.legacy_questions(session, job_id):
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


def _is_unique_violation(exc: IntegrityError, constraint: str) -> bool:
    """判断 IntegrityError 是否为「指定唯一约束」的冲突。

    为什么要这么仔细地判断?因为 IntegrityError 也可能是外键失败、
    字段超长等真正的错误。一律当成「重复跳过」会掩盖 bug。

    数据库切换到 PostgreSQL 后重写为跨驱动判定,按信号的结构化程度排序:
    1. sqlstate == "23505"(SQL 标准的 unique_violation,psycopg3 提供),
       再看约束名/表名里是否含目标约束——最可靠,完全不依赖错误文案
    2. MySQL 错误码 1062(pymysql 放在 args[0],迁移脚本会连 MySQL),约束名出现在文案里
    3. sqlite3(测试环境)没有结构化信息,只能匹配文案 "UNIQUE constraint failed: ..."
    """
    orig = getattr(exc, "orig", None)
    if getattr(orig, "sqlstate", None) == "23505":
        names = f"{getattr(orig, 'constraint', '')} {getattr(orig, 'table_name', '')}"
        return constraint in names
    args = getattr(orig, "args", ())
    if args and args[0] == 1062:
        return constraint in str(orig).lower()
    message = str(orig).lower()
    return "unique constraint failed" in message and constraint in message


def batch_create_questions(
    session: Session,
    request: BatchQuestionsRequest,
) -> BatchQuestionsResponse:
    """批量写入题目:业务错误逐题返回,数据库异常整批回滚。"""
    if job_repo.get_job(session, request.job_id) is None:
        raise ResourceNotFoundError("职业不存在")

    results: list[QuestionResult] = []
    known_hashes = bank_repo.existing_hashes(session, request.job_id)
    known_hashes.update(_legacy_hashes(session, request.job_id))
    created = skipped = failed = 0

    try:
        for index, item in enumerate(request.questions):
            reason = validate_question(item)
            if reason:
                results.append(
                    QuestionResult(index=index, status="failed", reason=reason)
                )
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
                # begin_nested 开一个 SAVEPOINT:单题冲突只回滚这一题,
                # 不影响已经成功的其他题。这是「部分成功」语义的实现关键。
                with session.begin_nested():
                    session.add(question)
                    session.flush()
            except IntegrityError as exc:
                if not _is_unique_violation(exc, "content_hash"):
                    raise
                # 内存里的 known_hashes 没拦住,说明是并发写入——
                # 数据库唯一索引兜住了,这正是「约束该由数据库保证」的例证
                results.append(
                    QuestionResult(
                        index=index, status="skipped", reason="并发下题目已存在"
                    )
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

    # 写成功后主动失效职业相关缓存。
    #
    # 顺序很关键:先 commit 数据库,再删缓存(Cache-Aside 的标准做法)。
    # 反过来先删缓存再写库会有一个危险窗口:删完缓存、库还没写完时,
    # 另一个请求来读,会把「旧数据」重新加载进缓存,而这个旧值要等
    # 整个 TTL 才过期——数据库已经是新的,缓存却一直是旧的。
    #
    # 为什么是删除而不是更新缓存:更新需要知道新值的完整形状,
    # 而这里影响的是「职业列表的题目数」和「单个职业的题目数」两处派生数据,
    # 重算一遍不如直接删掉、让下一次读请求自然回填。
    # 删除是幂等的,更新则要考虑并发写的先后顺序。
    #
    # 只在真正写入了数据时才失效:全部跳过/失败时数据没变,没必要清缓存。
    if created:
        # 按前缀清:一次写入会同时影响列表和详情两类 key,
        # 逐个删容易漏(比如忘了题干列表也缓存着)。
        cache.delete_prefix(cache_keys.JOBS_PREFIX)

    return BatchQuestionsResponse(
        total=len(request.questions),
        created=created,
        skipped=skipped,
        failed=failed,
        results=results,
    )
