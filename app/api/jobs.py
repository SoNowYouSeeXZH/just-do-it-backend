"""职业与面试题接口。

四个接口:
- GET /api/jobs                            列出所有职业(元信息 + 题目数)
- GET /api/jobs/{job_id}                   取单个职业元信息(带题目数)
- GET /api/jobs/{job_id}/questions         按职业随机抽题,默认 10 道
- GET /api/jobs/{job_id}/question-titles   只返回题干,不含答案

重构后这一层没有 SQL、没有聚合逻辑、没有数据库方言函数——
那些都在 repositories/job.py 里。
"""

from typing import Annotated

from fastapi import APIRouter, Query

from app.db import SessionDep
from app.schemas.job import JobPublic, QuestionPublic
from app.services import job as job_service

router = APIRouter(prefix="/api", tags=["jobs"])


@router.get("/jobs", response_model=list[JobPublic])
def list_jobs(session: SessionDep) -> list[JobPublic]:
    """列出全部职业(带题目数)。"""
    return job_service.list_jobs(session)


@router.get("/jobs/{job_id}", response_model=JobPublic)
def get_job(job_id: str, session: SessionDep) -> JobPublic:
    """取单个职业(带题目数),不存在返回 404。"""
    return job_service.get_job(session, job_id)


@router.get("/jobs/{job_id}/questions", response_model=list[QuestionPublic])
def get_random_questions(
    job_id: str,
    session: SessionDep,
    # 抽题数量,默认 10,范围 1~50,超出直接 422
    limit: Annotated[int, Query(ge=1, le=50)] = 10,
) -> list[QuestionPublic]:
    """按职业随机抽 limit 道题(含答案与解析,前端本地判分用)。"""
    return job_service.sample_questions(session, job_id, limit)


@router.get("/jobs/{job_id}/question-titles", response_model=list[str])
def list_question_titles(job_id: str, session: SessionDep) -> list[str]:
    """按职业返回全部题干,不含选项、答案或解析。"""
    return job_service.list_question_titles(session, job_id)
