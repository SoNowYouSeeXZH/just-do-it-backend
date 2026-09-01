"""任务表模型。

一个 SQLModel 类同时承担两个角色:
1. ORM 表定义(table=True 时)—— 告诉数据库"这张表长什么样"
2. Pydantic 校验模型 —— 可以直接当作 API 返回体,不用再写一份 DTO

字段设计的取舍写在每个字段旁边的注释里。
"""

from datetime import datetime

from sqlalchemy import Index
from sqlmodel import Field, SQLModel


class Task(SQLModel, table=True):
    __tablename__ = "tasks"  # pyright: ignore[reportAssignmentType, reportUnannotatedClassAttribute]

    # 复合索引:覆盖 list_active(见 repositories/task.py)的完整查询形状——
    # WHERE user_id = ? AND deleted_at IS NULL [AND status = ?] ORDER BY created_at DESC。
    # 四个字段各自的单列索引只能让查询命中其中一个,剩下的条件仍要逐行比对;
    # 列的顺序是关键:等值过滤字段(user_id/deleted_at/status)在前,
    # 排序字段(created_at)放最后,索引本身就按 created_at 排好序,
    # 配合 LIMIT 能省掉一次额外的 filesort。
    __table_args__ = (
        Index(
            "ix_tasks_user_deleted_status_created",
            "user_id",
            "deleted_at",
            "status",
            "created_at",
        ),
    )

    # 主键:BIGINT 自增。
    # Optional[int] + default=None 是 SQLModel 的惯用法——
    # 新建对象时 id 还没有,存进去后由数据库赋值。
    id: int | None = Field(default=None, primary_key=True)

    # 所有者:必须关联真实用户。和 chat_messages 一样,没有 owner 就只能
    # 认证"有人登录了",无法授权"这个用户能不能改这条任务"。
    # 外键保正确(拒绝指向不存在的用户),index 保性能(永远按 owner 过滤)。
    user_id: int = Field(foreign_key="users.id", index=True)

    # 标题:VARCHAR(255),给一个明确上限,挡住超长输入在 schema 层就 422,
    # 而不是一路走到 INSERT 才被数据库拒绝报 500。
    title: str = Field(max_length=255)

    # 描述:正文,不限长(SQLModel 里 str 不给 max_length 默认走 TEXT)。
    # 用 default="" 而不是 None,让"没有描述"和"有描述"在类型上都是 str,
    # 上层不用到处写 if description is None。
    description: str = Field(default="")

    # 状态:状态机的当前节点。取值见 services/task.py 的 _TRANSITIONS。
    # 用 VARCHAR(16) 而不是 ENUM:加状态不用改表结构,只改业务层那张映射表。
    # index=True 是因为列表查询常按状态筛选。
    status: str = Field(default="pending", max_length=16, index=True)

    # 截止时间:可空——不是所有任务都有 deadline。
    # 时区问题留给上层:存什么时区、要不要带 tzinfo,是业务决策,不在表结构里。
    due_at: datetime | None = Field(default=None)

    # 创建时间:default_factory 每次新建对象时才取一次"当前时间",
    # 而不是模块加载那一刻。index 让"按时间倒序分页"能走索引。
    created_at: datetime = Field(default_factory=datetime.now, index=True)

    # 更新时间:记录最近一次修改。SQLModel 不会自动维护它,
    # 所以业务层每次改任务时要手动赋值(见 services/task.py)。
    updated_at: datetime = Field(default_factory=datetime.now)

    # 软删除时间:非空表示这条记录已被"删除"。
    # 软删除的意义见 docs/notes/08-task-state-machine-and-soft-delete.md:
    # 物理删了就找不回来,而任务数据有审计/统计价值,用 deleted_at 标记"已删除",
    # 查询时 WHERE deleted_at IS NULL 过滤即可。
    # index=True 是因为几乎所有查询都要带上"未删除"这个条件。
    deleted_at: datetime | None = Field(default=None, index=True)
