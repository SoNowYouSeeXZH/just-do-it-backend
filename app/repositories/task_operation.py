"""task_operation_records 表的数据访问封装。

只负责 add / flush / select,绝不 commit。
操作记录通常和任务状态更新属于同一个业务事务,不能在这里提前提交。
"""

from sqlmodel import Session, select

from app.models.task_operation import TaskOperationRecord


def list_for_task(
    session: Session, *, task_id: int, user_id: int
) -> list[TaskOperationRecord]:
    """按任务和 owner 查询操作记录,防止通过 task_id 读取别人的审计记录。"""
    return list(
        session.exec(
            select(TaskOperationRecord)
            .where(
                TaskOperationRecord.task_id == task_id,
                TaskOperationRecord.user_id == user_id,
            )
            .order_by(TaskOperationRecord.created_at.desc())
        ).all()
    )


def find_by_idempotency_key(
    session: Session, *, user_id: int, idempotency_key: str
) -> TaskOperationRecord | None:
    """查当前用户已经处理过的幂等键。"""
    return session.exec(
        select(TaskOperationRecord).where(
            TaskOperationRecord.user_id == user_id,
            TaskOperationRecord.idempotency_key == idempotency_key,
        )
    ).first()


def insert(
    session: Session,
    *,
    task_id: int,
    user_id: int,
    action: str,
    from_status: str,
    to_status: str,
    idempotency_key: str | None,
) -> TaskOperationRecord:
    """添加一条操作记录,不提交事务。"""
    record = TaskOperationRecord(
        task_id=task_id,
        user_id=user_id,
        action=action,
        from_status=from_status,
        to_status=to_status,
        idempotency_key=idempotency_key,
    )
    session.add(record)
    session.flush()
    return record
