"""
聊天消息表模型。

一个 SQLModel 类同时承担两个角色:
1. ORM 表定义(table=True 时)—— 告诉数据库"这张表长什么样"
2. Pydantic 校验模型 —— 可以直接当作 API 返回体,不用再写一份 DTO

字段设计的取舍写在每个字段旁边的注释里。
"""

from datetime import datetime

from sqlmodel import Field, SQLModel


class ChatMessage(SQLModel, table=True):
    __tablename__ = "chat_messages" # pyright: ignore[reportAssignmentType]

    # 主键:BIGINT 自增。
    # Optional[int] + default=None 是 SQLModel 的惯用法——
    # 新建对象时 id 还没有,存进去后由数据库赋值。
    id: int | None = Field(default=None, primary_key=True)

    # 角色:'user' 或 'assistant'。
    # 用 VARCHAR(16) 而不是 ENUM,是因为 ENUM 加字段要改表结构,VARCHAR 灵活。
    # index=True 会为它建索引,方便"只查 user 说过的话"这类过滤。
    role: str = Field(max_length=16, index=True)

    # 内容:用 TEXT(SQLModel 里通过不给 max_length 默认为 TEXT/较长)。
    # 直接给 str 类型即可,长度上限交给 MySQL 的 TEXT(64KB)兜底。
    content: str

    # 创建时间:默认取当前时间。
    # 用 default_factory 而不是 default=datetime.utcnow(),
    # 是为了每次新建对象时才取一次"当前时间",而不是模块加载那一刻。
    # index=True 让"按时间倒序取最近 N 条"能走索引,不必全表扫描。
    created_at: datetime = Field(default_factory=datetime.now, index=True)
