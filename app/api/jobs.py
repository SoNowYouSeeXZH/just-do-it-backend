"""
职业与面试题接口。

三个接口:
- GET /api/jobs                        列出所有职业(元信息 + 各职业题目数,不含题目)
- GET /api/jobs/{job_id}               取单个职业的元信息(带题目数),404 处理
- GET /api/jobs/{job_id}/questions     按职业随机抽题,默认 10 道,覆盖单选/多选

随机抽题用 MySQL 的 ORDER BY rand()。该写法的开销名声不好,但仅在题库
非常大的情况下(十万级)才是问题;这里 WHERE job_id 先缩小到单职业题库(~百级),
再排序,开销可忽略。题量真上来了再换策略即可。

为什么列表接口带 question_count 而不带 questions:
前端首页课程卡要展示「N 道题 / 最佳 X/N」,但不需要题目内容——题目在进入
具体职业时才按需拉取。返回题目数而不是题目本身,既满足展示又避免一次拉一大坨。
"""

from typing import Annotated

from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import func
from sqlmodel import SQLModel, select

from app.db import SessionDep
from app.models.job import Job, Question

router = APIRouter(prefix="/api", tags=["jobs"])


class JobListItem(SQLModel):
    """职业列表项:元信息 + 该职业题目数(纯返回结构,不落表)。"""

    id: str
    title: str
    emoji: str
    tagline: str
    accent: str
    question_count: int


@router.get("/jobs", response_model=list[JobListItem])
def list_jobs(session: SessionDep) -> list[JobListItem]:
    """列出全部职业(带题目数)。

    题目数用一条 GROUP BY 聚合查出来再拼进返回结构,避免对每个职业单独
    数一次(N+1 查询)。不带 questions——题目在进入具体职业时再按需请求。
    """
    # 一次查出每个职业的题目数:{job_id: count}
    counts = dict(
        session.exec(
            select(Question.job_id, func.count(Question.id)).group_by(Question.job_id)
        ).all()
    )
    # 按 id 排序只是为了让前端展示顺序稳定(不依赖数据库内部存储顺序)。
    jobs = session.exec(select(Job).order_by(Job.id)).all()
    return [
        JobListItem(**job.model_dump(), question_count=counts.get(job.id, 0))
        for job in jobs
    ]


@router.get("/jobs/{job_id}", response_model=JobListItem)
def get_job(job_id: str, session: SessionDep) -> JobListItem:
    """取单个职业(带题目数)。

    答题页进入时用它拿 title/accent 等展示信息;题目另外走 questions 接口。
    业务 id 是字符串主键,直接按主键取,查不到返回 404。
    """
    job = session.get(Job, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="职业不存在")

    count = session.exec(
        select(func.count()).select_from(Question).where(Question.job_id == job_id)
    ).one()
    return JobListItem(**job.model_dump(), question_count=count)


@router.get("/jobs/{job_id}/questions", response_model=list[Question])
def get_random_questions(
    job_id: str,
    session: SessionDep,
    # 抽题数量,默认 10,范围限定 1~50,超出直接 422。
    limit: Annotated[int, Query(ge=1, le=50)] = 10,
) -> list[Question]:
    """按职业随机抽 limit 道题。

    等价 SQL:
        SELECT * FROM questions
        WHERE job_id = :job_id
        ORDER BY RAND()
        LIMIT :limit
    """
    statement = (
        select(Question)
        .where(Question.job_id == job_id)
        # func.rand() 生成 MySQL 的 RAND();每行一个随机值,排序即"打乱"。
        # 注意:PostgreSQL 对应的是 RANDOM(),跨库时这里要改。
        .order_by(func.rand())
        .limit(limit)
    )
    results = session.exec(statement).all()
    return list[Question](results)


@router.get("/jobs/{job_id}/question-titles", response_model=list[str])
def list_question_titles(
    job_id: str,
    session: SessionDep,
) -> list[str]:
    """按职业返回全部题干，不返回选项、答案或解析。"""

    if session.get(Job, job_id) is None:
        raise HTTPException(status_code=404, detail="职业不存在")

    questions = session.exec(
        select(Question)
        .where(Question.job_id == job_id)
        .order_by(Question.id)
    ).all()
    return [question.prompt for question in questions]
