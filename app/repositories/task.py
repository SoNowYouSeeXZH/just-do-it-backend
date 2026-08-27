"""tasks 表的数据访问封装。

本层只负责"如实取数/写入",不做业务判断、不抛业务异常
(这是架构测试 test_repositories_stay_free_of_web_and_business 守的边界)。
"查不到算不算错误"是业务层的决策,这里一律返回 None / 空集合。

软删除的查询约束统一收敛在这里:所有读取都带上
`deleted_at IS NULL`,上层不用每次记得加这个条件。
"""

from sqlalchemy import func
from sqlmodel import Session, select

from app.models.task import Task


def insert(
    session: Session,
    *,
    user_id: int,
    title: str,
    description: str,
    status: str,
    due_at,
) -> Task:
    """插入一条属于指定用户的任务,但不提交事务。

    Repository 只负责 session.add + flush,事务边界由 Service 控制。
    flush 会把 INSERT 发给数据库并拿到自增 id,但事务仍可 rollback。
    这样 Service 才能把“建任务 + 操作记录”组合成一个原子业务用例。
    """
    task = Task(
        user_id=user_id,
        title=title,
        description=description,
        status=status,
        due_at=due_at,
    )
    session.add(task)
    session.flush()
    return task


def find_active_by_id(session: Session, task_id: int) -> Task | None:
    """按主键取一条"未删除"的任务,不存在或已删除返回 None。

    等价 SQL:
        SELECT * FROM tasks WHERE id = :id AND deleted_at IS NULL
    """
    statement = select(Task).where(Task.id == task_id, Task.deleted_at.is_(None))
    return session.exec(statement).first()


def list_active(
    session: Session,
    *,
    user_id: int,
    status: str | None,
    offset: int,
    limit: int,
) -> tuple[list[Task], int]:
    """分页取指定用户的未删除任务,返回 (当前页数据, 满足条件的总数)。

    owner 过滤、软删除过滤都在 SQL 层完成,不能先全查出来再用 Python 过滤:
    既可能泄露别人的数据,也白白占用内存。

    等价 SQL:
        SELECT * FROM tasks
        WHERE user_id = :uid AND deleted_at IS NULL
          [AND status = :status]
        ORDER BY created_at DESC
        LIMIT :limit OFFSET :offset
        -- 配合一条
        SELECT COUNT(*) FROM tasks WHERE user_id = :uid AND deleted_at IS NULL [AND status = :status]
    """
    filters = [Task.user_id == user_id, Task.deleted_at.is_(None)]
    if status is not None:
        filters.append(Task.status == status)

    items = list(
        session.exec(
            select(Task)
            .where(*filters)
            .order_by(Task.created_at.desc())
            .offset(offset)
            .limit(limit)
        ).all()
    )
    total = session.exec(
        select(func.count()).select_from(Task).where(*filters)
    ).one()
    return items, total


def save(session: Session, task: Task) -> Task:
    """把已修改的 ORM 对象 flush,不提交事务。

    Service 需要在同一个事务里继续写操作记录，因此 Repository 不能提前 commit。
    """
    session.add(task)
    session.flush()
    return task


def soft_delete(session: Session, task: Task, deleted_at) -> None:
    """软删除:只打上 deleted_at 时间戳,不真正 DELETE 行。

    物理删除不可恢复,而任务数据有审计/统计价值。软删除让"删除"变成
    一次普通更新,查询层用 deleted_at IS NULL 把它过滤掉即可。
    """
    task.deleted_at = deleted_at
    session.add(task)
    session.flush()
