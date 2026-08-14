"""
转型推荐 + 学习路径接口。

四个接口:
- GET /api/career-paths              列出所有目标职业学习路径(转型页用来补全 targetId 展示信息)
- GET /api/career-paths/{path_id}    取单条学习路径详情(学习步骤页用)
- GET /api/current-jobs              列出所有「当前职业」选项(供用户选择)
- GET /api/current-jobs/{job_id}     取单个当前职业的转型建议

内容纯只读、数据量小,不需要聚合或随机抽样，直接整表/按主键查询即可，
接口风格和 industries.py 一致。
"""

from fastapi import APIRouter, HTTPException
from sqlmodel import select

from app.db import SessionDep
from app.models.career import CareerPath, CurrentJob

router = APIRouter(prefix="/api", tags=["careers"])


@router.get("/career-paths", response_model=list[CareerPath])
def list_career_paths(session: SessionDep) -> list[CareerPath]:
    """列出全部目标职业学习路径。按 id 排序,保证前端展示顺序稳定。"""
    return list(session.exec(select(CareerPath).order_by(CareerPath.id)).all())


@router.get("/career-paths/{path_id}", response_model=CareerPath)
def get_career_path(path_id: str, session: SessionDep) -> CareerPath:
    """取单条学习路径详情。业务 id 是字符串主键,查不到返回 404。"""
    path = session.get(CareerPath, path_id)
    if path is None:
        raise HTTPException(status_code=404, detail="学习路径不存在")
    return path


@router.get("/current-jobs", response_model=list[CurrentJob])
def list_current_jobs(session: SessionDep) -> list[CurrentJob]:
    """列出全部「当前职业」选项。按 id 排序,保证前端展示顺序稳定。"""
    return list(session.exec(select(CurrentJob).order_by(CurrentJob.id)).all())


@router.get("/current-jobs/{job_id}", response_model=CurrentJob)
def get_current_job(job_id: str, session: SessionDep) -> CurrentJob:
    """取单个当前职业的转型建议。业务 id 是字符串主键,查不到返回 404。"""
    job = session.get(CurrentJob, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="职业不存在")
    return job
