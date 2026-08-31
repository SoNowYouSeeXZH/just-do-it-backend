"""职业与面试题业务逻辑。

这一层承担的不只是「转发」,而是几个真实的业务决策:

1. 列表接口要带题目数 —— 需要把两次查询的结果拼起来
2. 抽题前必须确认职业存在 —— 否则空题库和「职业不存在」返回一样,
   前端无法区分「这个职业没题」和「你传错了 id」
3. 组装 DTO —— 决定哪些字段对外可见
4. **决定哪些查询该缓存** —— 见下

关于缓存边界:职业列表、职业详情、题干列表都是"读多写少 + 全用户相同"的
内容型数据,适合缓存。但随机抽题接口**故意不缓存**——它的语义就是每次
返回不同的题,缓存会让随机性失效。判断能不能缓存,看的是"同样的输入是否
应该得到同样的输出",而不是"这个接口是不是读接口"。

缓存放在 Service 而不是 Repository:缓存的粒度是"一次业务查询的结果"
(职业+题目数拼装后的 DTO),而不是"一次 SQL 的结果"。放 Repository 层
只能缓存半成品,每次还要重新拼装;而且 Repository 的职责是如实读写数据库,
加缓存会让它不再"如实"。
"""

from sqlmodel import Session

from app.core import cache, cache_keys
from app.core.exceptions import ResourceNotFoundError
from app.repositories import job as job_repo
from app.schemas.job import JobPublic, QuestionPublic


def list_jobs(session: Session) -> list[JobPublic]:
    """列出全部职业,附带各自的题目数。

    两条查询而不是 N+1:先一次聚合出所有职业的题目数,再遍历职业列表拼装。
    结果整体缓存——首页每次打开都要调它,是最值得缓存的接口。
    """

    def load() -> list[dict]:
        counts = job_repo.count_questions_by_job(session)
        jobs = job_repo.list_jobs(session)
        # 缓存里存 dict 而不是 Pydantic 对象:JSON 序列化需要普通数据结构。
        # 读回来时再 model_validate 成 DTO,类型安全不丢。
        return [
            JobPublic(**job.model_dump(), question_count=counts.get(job.id, 0)).model_dump()
            for job in jobs
        ]

    rows = cache.cache_aside(cache_keys.JOBS_LIST, load)
    return [JobPublic.model_validate(row) for row in rows]


def get_job(session: Session, job_id: str) -> JobPublic:
    """取单个职业(带题目数),不存在抛 404。

    这里是"缓存空结果防穿透"的落地点。如果不缓存"查不到",
    攻击者用随机 job_id 刷这个接口,每次都会打到数据库——
    缓存等于被绕过了。cache_aside 内部会把 None 存成一个短 TTL 的哨兵值,
    后续相同 id 的请求直接从缓存返回空,不再查库。
    """

    def load() -> dict | None:
        job = job_repo.get_job(session, job_id)
        if job is None:
            # 返回 None 而不是在这里抛异常:让 cache_aside 有机会
            # 把"不存在"这个事实也缓存下来。异常在缓存之外再抛。
            return None
        count = job_repo.count_questions_of_job(session, job_id)
        return JobPublic(**job.model_dump(), question_count=count).model_dump()

    row = cache.cache_aside(cache_keys.job_detail(job_id), load)
    if row is None:
        raise ResourceNotFoundError("职业不存在")
    return JobPublic.model_validate(row)


def sample_questions(session: Session, job_id: str, limit: int) -> list[QuestionPublic]:
    """按职业随机抽题。

    先校验职业存在:原来的实现直接抽题,职业 id 传错时返回空数组,
    和「这个职业确实还没录题」完全无法区分。补上这个校验后语义就清晰了——
    404 说明 id 错了,空数组说明题库是空的。

    抽题结果本身不缓存(见模块顶部说明);但这里的存在性校验走了
    get_job 的缓存,所以恶意刷不存在的 id 也不会每次打到数据库。
    """
    # 复用带缓存的 get_job:它在职业不存在时抛 404,语义正好一致
    get_job(session, job_id)

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
    get_job(session, job_id)

    def load() -> list[str]:
        questions = job_repo.list_questions_of_job(session, job_id)
        return [question.prompt for question in questions]

    return cache.cache_aside(cache_keys.job_question_titles(job_id), load)
