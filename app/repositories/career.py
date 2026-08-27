"""career_paths / current_jobs 两张表的数据访问封装。

两张表放同一个文件,是因为它们属于同一个业务概念(职业转型),
总是被一起改动。分文件的标准是「是否一起变化」,不是「表的数量」。
"""

from sqlmodel import Session, select

from app.models.career import CareerPath, CurrentJob


def list_career_paths(session: Session) -> list[CareerPath]:
    """按 id 升序取全部学习路径。"""
    return list(session.exec(select(CareerPath).order_by(CareerPath.id)).all())


def get_career_path(session: Session, path_id: str) -> CareerPath | None:
    """按主键取单条学习路径,不存在返回 None。"""
    return session.get(CareerPath, path_id)


def list_current_jobs(session: Session) -> list[CurrentJob]:
    """按 id 升序取全部「当前职业」选项。"""
    return list(session.exec(select(CurrentJob).order_by(CurrentJob.id)).all())


def get_current_job(session: Session, job_id: str) -> CurrentJob | None:
    """按主键取单个「当前职业」,不存在返回 None。"""
    return session.get(CurrentJob, job_id)
