"""
转型推荐 + 目标职业学习路径表模型。

对应前端 data/careers.ts 的两个数据集：
- CareerPath  → career_paths 表:目标职业的分步学习路径
- CurrentJob  → current_jobs 表:用户当前职业 + 转型建议

两表彼此独立、无外键关联:
- CareerPath.steps 里的 quizJobId 只是字符串,指向 jobs 表的业务 id
- CurrentJob.recommendations 里的 targetId 只是字符串,指向 career_paths 的业务 id
这两处引用都不建外键约束——和 jobs.py 里 Question.job_id 加外键的做法不同,
是因为这里的"引用对象"（学习步骤、转型建议）整条跟随父记录读写,数据库层面
不需要按引用关联做查询或级联,加外键只会徒增约束成本。

steps / recommendations 都是"永远和父记录一起读写、无独立查询需求"的嵌套结构,
和 Industry 的 key_points/links 一样，直接用 JSON 列存，不拆子表。
"""

from sqlalchemy import Column, JSON
from sqlmodel import Field, SQLModel


class CareerPath(SQLModel, table=True):
    """一条目标职业的完整学习路径(学习步骤页用)。"""

    __tablename__ = "career_paths"  # pyright: ignore[reportAssignmentType, reportUnannotatedClassAttribute]

    # 主键:业务 id(frontend/backend/...),和 jobs 表的 id 对齐,方便前端跳转答题。
    id: str = Field(primary_key=True, max_length=32)

    title: str = Field(max_length=64)      # 职业名,如「前端工程师」
    emoji: str = Field(max_length=16)      # 图标
    accent: str = Field(max_length=16)     # 主题色十六进制
    summary: str = Field(max_length=128)   # 一句话定位

    # 分步学习路径:[{id, title, desc, quizJobId?, links?}, ...]
    # 整条跟随 path 读写，不需要单独查询某一步，JSON 列存。
    steps: list = Field(sa_column=Column(JSON, nullable=False))


class CurrentJob(SQLModel, table=True):
    """用户可选择的「当前职业」，附带若干转型建议。"""

    __tablename__ = "current_jobs"  # pyright: ignore[reportAssignmentType, reportUnannotatedClassAttribute]

    # 主键:业务 id(tester/operation/...)。
    id: str = Field(primary_key=True, max_length=32)

    title: str = Field(max_length=64)  # 职业名,如「测试工程师」
    emoji: str = Field(max_length=16)  # 图标

    # 转型建议列表:[{targetId, reason, difficulty}, ...]
    # 整条跟随 current_job 读写，JSON 列存。
    recommendations: list = Field(sa_column=Column(JSON, nullable=False))
