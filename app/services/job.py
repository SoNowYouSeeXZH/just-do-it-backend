"""职业与面试题业务逻辑。

这一层承担的不只是「转发」,而是几个真实的业务决策:

1. 列表接口要带题目数 —— 需要把两次查询的结果拼起来
2. 抽题前必须确认职业存在 —— 否则空题库和「职业不存在」返回一样,
   前端无法区分「这个职业没题」和「你传错了 id」
3. 组装 DTO —— 决定哪些字段对外可见
"""

from sqlmodel import Session

from app.core.exceptions import ResourceNotFoundError
from app.repositories import job as job_repo
from app.schemas.job import JobPublic, QuestionPublic


def list_jobs(session: Session) -> list[JobPublic]:
    """列出全部职业,附带各自的题目数。

    两条查询而不是 N+1:先一次聚合出所有职业的题目数,再遍历职业列表拼装。
    """
    counts = job_repo.count_questions_by_job(session)
    jobs = job_repo.list_jobs(session)
    return [
        JobPublic(**job.model_dump(), question_count=counts.get(job.id, 0))
        for job in jobs
    ]


def get_job(session: Session, job_id: str) -> JobPublic:
    """取单个职业(带题目数),不存在抛 404。"""
    job = job_repo.get_job(session, job_id)
    if job is None:
        raise ResourceNotFoundError("职业不存在")

    count = job_repo.count_questions_of_job(session, job_id)
    return JobPublic(**job.model_dump(), question_count=count)


def sample_questions(session: Session, job_id: str, limit: int) -> list[QuestionPublic]:
    """按职业随机抽题。

    先校验职业存在:原来的实现直接抽题,职业 id 传错时返回空数组,
    和「这个职业确实还没录题」完全无法区分。补上这个校验后语义就清晰了——
    404 说明 id 错了,空数组说明题库是空的。
    """
    if job_repo.get_job(session, job_id) is None:
        raise ResourceNotFoundError("职业不存在")

    questions = job_repo.sample_questions(session, job_id, limit)
    return [
        QuestionPublic.model_validate(question, from_attributes=True)
        for question in questions
    ]


def list_question_titles(session: Session, job_id: str) -> list[str]:
    """按职业返回全部题干,不含选项、答案和解析。

    这个接口的存在意义就是「只给题干」——所以返回类型是 list[str],
    从类型上就杜绝了答案泄露的可能。
    """
    if job_repo.get_job(session, job_id) is None:
        raise ResourceNotFoundError("职业不存在")

    questions = job_repo.list_questions_of_job(session, job_id)
    return [question.prompt for question in questions]
