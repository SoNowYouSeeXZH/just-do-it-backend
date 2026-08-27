"""jobs / questions 两张表的数据访问封装。

这个文件是本轮重构里最有价值的一处——原来 app/api/jobs.py 的路由里
混着聚合查询、随机抽样、数据库方言函数(func.rand())。
方言问题尤其值得隔离:rand() 是 MySQL 的写法,SQLite/PostgreSQL 叫 random()。
把它关在这一层,以后换库只改这个文件,路由和业务层完全不受影响。
"""

from sqlalchemy import func
from sqlmodel import Session, select

from app.models.job import Job, Question


def list_jobs(session: Session) -> list[Job]:
    """按 id 升序取全部职业。"""
    return list(session.exec(select(Job).order_by(Job.id)).all())


def get_job(session: Session, job_id: str) -> Job | None:
    """按主键取单个职业,不存在返回 None。"""
    return session.get(Job, job_id)


def count_questions_by_job(session: Session) -> dict[str, int]:
    """一次查出每个职业的题目数,返回 {job_id: count}。

    这是避免 N+1 查询的关键。朴素写法是遍历职业列表,对每个职业
    单独 count 一次——10 个职业就是 1 + 10 = 11 条 SQL。
    改成一条 GROUP BY 聚合后只需 2 条(职业列表 + 计数)。

    等价 SQL:
        SELECT job_id, COUNT(id) FROM questions GROUP BY job_id
    """
    rows = session.exec(
        select(Question.job_id, func.count(Question.id)).group_by(Question.job_id)
    ).all()
    return dict(rows)


def count_questions_of_job(session: Session, job_id: str) -> int:
    """数单个职业的题目数。

    等价 SQL:
        SELECT COUNT(*) FROM questions WHERE job_id = :job_id
    """
    return session.exec(
        select(func.count()).select_from(Question).where(Question.job_id == job_id)
    ).one()


def _random_func(session: Session):
    """返回当前数据库方言对应的随机函数。

    MySQL 是 RAND(),SQLite / PostgreSQL 是 RANDOM()。
    原来代码里硬编码了 func.rand(),导致两个后果:
    1. 换数据库要改业务代码
    2. 测试用 SQLite 时直接报 "no such function: rand",
       抽题接口的正常路径根本没法测

    「测试写不出来」通常是耦合的信号——这里就是一个具体例子。
    把方言判断收在本层之后,上层完全不需要知道底下是什么数据库。
    """
    dialect = session.get_bind().dialect.name
    return func.rand() if dialect == "mysql" else func.random()


def sample_questions(session: Session, job_id: str, limit: int) -> list[Question]:
    """按职业随机抽 limit 道题。

    等价 SQL:
        SELECT * FROM questions WHERE job_id = :job_id ORDER BY RAND() LIMIT :limit

    ORDER BY RAND() 的名声不好,因为它要给全表每行算一个随机值再排序。
    但这里 WHERE job_id 先把范围缩到单个职业的题库(百级),
    开销可以忽略。题量到十万级再换策略(比如先随机取主键区间)。
    """
    statement = (
        select(Question)
        .where(Question.job_id == job_id)
        .order_by(_random_func(session))
        .limit(limit)
    )
    return list(session.exec(statement).all())


def list_questions_of_job(session: Session, job_id: str) -> list[Question]:
    """按职业取全部题目,按 id 升序。"""
    return list(
        session.exec(
            select(Question).where(Question.job_id == job_id).order_by(Question.id)
        ).all()
    )
