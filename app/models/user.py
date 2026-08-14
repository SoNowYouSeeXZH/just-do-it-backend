"""
用户表模型。

一个 SQLModel 类同时承担两个角色:
1. ORM 表定义(table=True 时)—— 告诉数据库"这张表长什么样"
2. Pydantic 校验模型 —— 可以直接当作 API 返回体,不用再写一份 DTO

字段设计的取舍写在每个字段旁边的注释里。
"""

from datetime import datetime

from sqlmodel import Field, SQLModel


class Users(SQLModel, table=True):
    __tablename__ = "users"  # pyright: ignore[reportAssignmentType, reportUnannotatedClassAttribute]

    # 主键:BIGINT 自增。
    # Optional[int] + default=None 是 SQLModel 的惯用法——
    # 新建对象时 id 还没有,存进去后由数据库赋值。
    id: int | None = Field(default=None, primary_key=True)

    # 用户名:登录/展示用的唯一标识。
    # VARCHAR(64);unique=True 保证不重复,index=True 让"按用户名查用户/登录校验"走索引。
    username: str = Field(max_length=64, index=True, unique=True)

    # 密码哈希:只存哈希,绝不存明文。
    # 长度按所用算法留足余量(bcrypt 60、argon2 可能上百),这里给 255。
    # 不建索引——不会按密码查询。
    password_hash: str = Field(max_length=255)

    # 创建时间:默认取当前时间。
    # 用 default_factory 而不是 default=datetime.utcnow(),
    # 是为了每次新建对象时才取一次"当前时间",而不是模块加载那一刻。
    # index=True 让"按时间倒序取最近 N 条"能走索引,不必全表扫描。
    created_at: datetime = Field(default_factory=datetime.now, index=True)
