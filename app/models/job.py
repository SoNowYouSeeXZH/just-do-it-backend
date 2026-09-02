"""
职业与面试题表模型。

两张表的关系:一个 Job(职业)对应多道 Question(面试题),一对多。
之所以把题目从 Job 里拆出来单独建表,而不是塞进一个 JSON 字段,是因为
后续要支持"每次进入随机抽 10 道新题",需要按 job_id 在题库里随机采样——
题目独立成行才能用 SQL 高效抽样,JSON 列没法做这件事。

题型同时覆盖「单选」和「多选」:
- qtype='single':只有一个正确选项,answer_indices 长度为 1
- qtype='multi' :有多个正确选项,answer_indices 长度 >= 1
前端按 qtype 决定渲染单选按钮还是多选框,判分时比较"选中集合 == answer_indices 集合"。
"""

from sqlalchemy import Column, JSON
from sqlmodel import Field, SQLModel


class Job(SQLModel, table=True):
    """职业元信息(不含题目)。

    和前端 data/jobs.ts 里 Job 的顶层字段一一对应;questions 已搬到 Question 表,
    通过 job_id 关联。接口返回时按需 join 或单独查 questions。
    """

    __tablename__ = "jobs"  # pyright: ignore[reportAssignmentType, reportUnannotatedClassAttribute]

    # 主键:用业务 id(frontend/backend/...)做主键,而不是自增 int。
    # 因为职业是有限且稳定的枚举,业务 id 天然唯一,前端路由也直接用它,
    # 用字符串主键省去一层"业务 id ↔ 自增 id"的转换。
    id: str = Field(primary_key=True, max_length=32)

    title: str = Field(max_length=64)      # 职业名,如「前端工程师」
    emoji: str = Field(max_length=16)      # 图标(用 emoji 避免美术依赖)
    tagline: str = Field(max_length=128)   # 一句话定位,如「HTML / CSS / JS / 框架」
    accent: str = Field(max_length=16)     # 主题色十六进制,如 '#1CB0F6'


class Question(SQLModel, table=True):
    """一道面试题(单选或多选)。

    每题一行,job_id 加索引——这是"随机抽 10 题"能高效执行的前提:
    WHERE job_id = ? 先把范围缩到该职业的题库(~百级),再 ORDER BY rand() LIMIT 10,
    开销可忽略。题量到十万级再考虑换抽样策略,现在不必过早优化。
    """

    __tablename__ = "questions"  # pyright: ignore[reportAssignmentType, reportUnannotatedClassAttribute]

    # 主键:BIGINT 自增。题目是会持续累积的(爬一批、再爬一批),用自增 int 合适。
    id: int | None = Field(default=None, primary_key=True)

    # 外键:指向 jobs.id。index=True 让"按职业筛题"走索引。
    job_id: str = Field(foreign_key="jobs.id", index=True, max_length=32)

    # 题型:'single' 单选 / 'multi' 多选。
    # 用 VARCHAR(8) 而不是 ENUM,避免以后加新题型要改表结构。
    qtype: str = Field(max_length=8)

    # 题干。给 512 字符余量,足够中文面试题。
    prompt: str = Field(max_length=512)

    # 选项列表:不定长(常见 4 个,但允许 3~6)。
    # 用 JSON 列存,而不是拆一张 options 子表——选项永远和题目一起读、一起写,
    # 没有独立查询需求,拆表只会徒增 join 成本。
    options: list = Field(sa_column=Column(JSON, nullable=False))

    # 正确答案的下标列表:
    # - 单选:[0]   (长度 1)
    # - 多选:[0,2] (长度 >= 1)
    # 前端 mock 里是 answerIndex(单个 int),升级为数组以同时表达单选/多选。
    answer_indices: list = Field(sa_column=Column(JSON, nullable=False))

    # 解析:答题后展示。PG 的 TEXT 没有长度上限,这里给 1024 上限是业务约束。
    explanation: str = Field(max_length=1024)

    # 溯源:这道题是从哪个页面爬来的,方便核对内容与版权归属。
    # 可空——手动录入或 LLM 生成的题没有来源。
    source_url: str | None = Field(default=None, max_length=512)

    # 内容指纹:对 prompt(必要时含 job_id)做 hash,用于爬取去重。
    # unique=True 防止同一道题被重复入库;PG/SQLite 的唯一索引同样允许多个 NULL,
    # 所以手动题留空不冲突。
    content_hash: str | None = Field(default=None, max_length=64, unique=True, index=True)
