"""
行业知识库表模型。

对应前端 data/industries.ts 的 Industry 结构。这张表内容是纯只读的知识条目
(名称、简介、要点、外链)，没有子表关联——不像 Job/Question 需要按 job_id
随机抽题，Industry 的 keyPoints/links 永远和整条记录一起读、一起写，
所以直接用 JSON 列存，不必拆子表。
"""

from sqlalchemy import Column, JSON
from sqlmodel import Field, SQLModel


class Industry(SQLModel, table=True):
    """一个行业方向(前端知识库列表 + 详情页用同一份数据)。"""

    __tablename__ = "industries"  # pyright: ignore[reportAssignmentType, reportUnannotatedClassAttribute]

    # 主键:业务 id(web-dev/backend/...),和 Job 一样用字符串主键——
    # 行业是有限且稳定的枚举,前端路由也直接用它。
    id: str = Field(primary_key=True, max_length=32)

    name: str = Field(max_length=64)       # 行业名,如「前端 / Web 开发」
    emoji: str = Field(max_length=16)      # 图标
    accent: str = Field(max_length=16)     # 主题色十六进制
    summary: str = Field(max_length=128)   # 列表卡片用的一句话简介
    overview: str = Field(max_length=1024)  # 详情页概述段落

    # 关键知识点列表:字符串数组,永远整条读写,用 JSON 列存。
    key_points: list[str] = Field(sa_column=Column(JSON, nullable=False))

    # 外部学习资料:[{label, url}, ...]，同样整条读写，JSON 列存。
    links: list = Field(sa_column=Column(JSON, nullable=False))
