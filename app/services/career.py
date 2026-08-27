"""职业转型与学习路径业务逻辑。"""

from sqlmodel import Session

from app.core.exceptions import ResourceNotFoundError
from app.models.career import CareerPath, CurrentJob
from app.repositories import career as career_repo


def list_career_paths(session: Session) -> list[CareerPath]:
    """列出全部目标职业学习路径。"""
    return career_repo.list_career_paths(session)


def get_career_path(session: Session, path_id: str) -> CareerPath:
    """取单条学习路径,不存在抛 404。"""
    path = career_repo.get_career_path(session, path_id)
    if path is None:
        raise ResourceNotFoundError("学习路径不存在")
    return path


def list_current_jobs(session: Session) -> list[CurrentJob]:
    """列出全部「当前职业」选项。"""
    return career_repo.list_current_jobs(session)


def get_current_job(session: Session, job_id: str) -> CurrentJob:
    """取单个「当前职业」的转型建议,不存在抛 404。"""
    job = career_repo.get_current_job(session, job_id)
    if job is None:
        raise ResourceNotFoundError("职业不存在")
    return job
