"""转型推荐 + 学习路径接口。

四个接口:
- GET /api/career-paths            列出所有目标职业学习路径
- GET /api/career-paths/{path_id}  取单条学习路径详情
- GET /api/current-jobs            列出所有「当前职业」选项
- GET /api/current-jobs/{job_id}   取单个当前职业的转型建议
"""

from fastapi import APIRouter

from app.db import SessionDep
from app.schemas.career import CareerPathPublic, CurrentJobPublic
from app.services import career as career_service

router = APIRouter(prefix="/api", tags=["careers"])


@router.get("/career-paths", response_model=list[CareerPathPublic])
def list_career_paths(session: SessionDep) -> list[CareerPathPublic]:
    """列出全部目标职业学习路径。"""
    paths = career_service.list_career_paths(session)
    return [
        CareerPathPublic.model_validate(path, from_attributes=True) for path in paths
    ]


@router.get("/career-paths/{path_id}", response_model=CareerPathPublic)
def get_career_path(path_id: str, session: SessionDep) -> CareerPathPublic:
    """取单条学习路径详情,不存在返回 404。"""
    path = career_service.get_career_path(session, path_id)
    return CareerPathPublic.model_validate(path, from_attributes=True)


@router.get("/current-jobs", response_model=list[CurrentJobPublic])
def list_current_jobs(session: SessionDep) -> list[CurrentJobPublic]:
    """列出全部「当前职业」选项。"""
    jobs = career_service.list_current_jobs(session)
    return [
        CurrentJobPublic.model_validate(job, from_attributes=True) for job in jobs
    ]


@router.get("/current-jobs/{job_id}", response_model=CurrentJobPublic)
def get_current_job(job_id: str, session: SessionDep) -> CurrentJobPublic:
    """取单个当前职业的转型建议,不存在返回 404。"""
    job = career_service.get_current_job(session, job_id)
    return CurrentJobPublic.model_validate(job, from_attributes=True)
