"""任务状态变更操作记录表。

一条 TaskOperationRecord 表示一次业务动作,而不是一次数据库 UPDATE:
谁(user_id)对哪条任务(task_id)执行了什么动作(action),
任务从什么状态(from_status)变成了什么状态(to_status)。

idempotency_key 是客户端为一次业务请求生成的唯一键。它的唯一约束
是数据库层的最后防线:即使两个重试请求同时到达,也不能产生两条操作记录。
"""

from datetime import datetime

from sqlalchemy import UniqueConstraint
from sqlmodel import Field, SQLModel


class TaskOperationRecord(SQLModel, table=True):
    __tablename__ = "task_operation_records"  # pyright: ignore[reportAssignmentType, reportUnannotatedClassAttribute]
    __table_args__ = (
        UniqueConstraint(
            "user_id", "idempotency_key", name="uq_task_operation_user_key"
        ),
    )

    id: int | None = Field(default=None, primary_key=True)
    task_id: int = Field(foreign_key="tasks.id", index=True)
    user_id: int = Field(foreign_key="users.id", index=True)
    action: str = Field(max_length=16)
    from_status: str = Field(max_length=16)
    to_status: str = Field(max_length=16)

    # 可选是为了兼容没有接入幂等键的旧客户端；非空时必须唯一。
    # SQL 的 UNIQUE 对 NULL 通常允许多行，这正好符合“没带 key 就不做幂等去重”。
    idempotency_key: str | None = Field(
        default=None, max_length=128, index=True
    )
    created_at: datetime = Field(default_factory=datetime.now, index=True)
