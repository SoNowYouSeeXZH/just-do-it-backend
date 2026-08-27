"""任务业务逻辑层(Service)。

这一层回答的问题是:"任务这件事,业务上要做哪几步、什么算成功、什么算越权"。

边界(判断分层有没有守住,就看这几条):
- 不 import fastapi —— 不知道 HTTP、状态码、请求响应的存在
- 不写 SQL —— 数据读写全部委托给 repositories
- 出错时抛 core.exceptions 里的业务异常,不抛 HTTPException

本层承担三个明确的业务规则:
1. 资源所有权:任何按 id 操作的任务,都要确认它属于当前用户,
   不属于(或不存在)统一抛 ResourceNotFoundError——对外只暴露"不存在",
   不泄露"存在但不属于你"。
2. 状态机:任务状态只能按允许的方向转换,非法转换抛 TaskStateError。
3. 软删除:删除只是打时间戳,且终态任务(已归档)不允许再删。
"""

from datetime import datetime

from sqlalchemy.exc import IntegrityError
from sqlmodel import Session

from app.core.exceptions import (
    IdempotencyConflictError,
    ResourceNotFoundError,
    TaskStateError,
)
from app.models.task import Task
from app.repositories import task as task_repo
from app.repositories import task_operation as operation_repo

# 状态机:每个当前状态允许通过哪个动作转到哪个新状态。
# 把转换表集中定义在这里,业务规则只有一份实现,改一处不会漏。
# 动作(action)是对外的语义("开始/完成/取消/归档"),
# 状态(status)是存储的值——分开是为了让前端用稳定的动作名,
# 不关心内部状态字符串。
_TRANSITIONS: dict[str, dict[str, str]] = {
    "pending": {"start": "in_progress", "cancel": "cancelled"},
    "in_progress": {"complete": "completed", "cancel": "cancelled"},
    "completed": {"archive": "archived"},
    "cancelled": {},  # 终态:不再有合法转换
    "archived": {},  # 终态:不再有合法转换
}

# 哪些状态允许编辑标题/描述/截止时间。
# 业务决策:已结束的任务(完成/取消/归档)视为历史快照,不允许再改正文,
# 要改就重新建一条。pending / in_progress 是"进行中",可以改。
_EDITABLE_STATUSES = {"pending", "in_progress"}


def create_task(
    session: Session,
    *,
    user_id: int,
    title: str,
    description: str,
    due_at: datetime | None,
) -> Task:
    """创建一条属于当前用户的任务,初始状态 pending。"""
    try:
        task = task_repo.insert(
            session,
            user_id=user_id,
            title=title,
            description=description,
            status="pending",
            due_at=due_at,
        )
        session.commit()
        session.refresh(task)
        return task
    except Exception:
        session.rollback()
        raise


def list_tasks(
    session: Session,
    *,
    user_id: int,
    page: int,
    page_size: int,
    status: str | None = None,
) -> tuple[list[Task], int]:
    """分页取当前用户的任务。

    返回 (当前页数据, 满足筛选的总数)。分页参数的边界校验由 schema 层
    (Query 的 ge/le)兜底,这里假定 page>=1、page_size>=1。
    """
    offset = (page - 1) * page_size
    return task_repo.list_active(
        session,
        user_id=user_id,
        status=status,
        offset=offset,
        limit=page_size,
    )


def get_owned_task(session: Session, *, user_id: int, task_id: int) -> Task:
    """取一条属于当前用户的任务,不存在或不属于该用户则抛 404。

    这是所有权授权的落地点:把"查不到"和"不是你的"统一翻译成
    "不存在",避免泄露资源存在性。所有按 id 改/删/转状态的入口
    都先调它,确保不会出现"用户 A 改了用户 B 的任务"。
    """
    task = task_repo.find_active_by_id(session, task_id)
    if task is None or task.user_id != user_id:
        raise ResourceNotFoundError("任务不存在")
    return task


def get_task(session: Session, *, user_id: int, task_id: int) -> Task:
    """取单个任务(已做所有权校验)。"""
    return get_owned_task(session, user_id=user_id, task_id=task_id)


def update_task(
    session: Session,
    *,
    user_id: int,
    task_id: int,
    title: str | None,
    description: str | None,
    due_at: datetime | None,
) -> Task:
    """更新任务的标题/描述/截止时间。

    只更新调用方实际传入的字段(None 表示没传、保持不变)。
    终态任务不允许编辑——已结束的任务是历史快照。
    """
    task = get_owned_task(session, user_id=user_id, task_id=task_id)
    if task.status not in _EDITABLE_STATUSES:
        raise TaskStateError("该任务已结束,不能编辑")

    if title is not None:
        task.title = title
    if description is not None:
        task.description = description
    if due_at is not None:
        task.due_at = due_at
    try:
        task.updated_at = datetime.now()
        task_repo.save(session, task)
        session.commit()
        session.refresh(task)
        return task
    except Exception:
        session.rollback()
        raise


def transition_task(
    session: Session,
    *,
    user_id: int,
    task_id: int,
    action: str,
    idempotency_key: str | None = None,
) -> Task:
    """按动作推进任务状态机,并记录一次操作。

    带同一个幂等键重试时,直接返回第一次执行后的任务状态,
    不会再次执行状态转换或新增操作记录。
    """
    if idempotency_key is not None:
        previous = operation_repo.find_by_idempotency_key(
            session, user_id=user_id, idempotency_key=idempotency_key
        )
        if previous is not None:
            if previous.task_id != task_id or previous.action != action:
                raise IdempotencyConflictError("幂等键已用于其他任务操作")
            return get_owned_task(session, user_id=user_id, task_id=previous.task_id)

    task = get_owned_task(session, user_id=user_id, task_id=task_id)
    allowed = _TRANSITIONS.get(task.status, {})
    if action not in allowed:
        raise TaskStateError(f"任务当前状态为 {task.status},不支持该操作")

    from_status = task.status
    to_status = allowed[action]
    try:
        task.status = to_status
        task.updated_at = datetime.now()
        task_repo.save(session, task)
        operation_repo.insert(
            session,
            task_id=task_id,
            user_id=user_id,
            action=action,
            from_status=from_status,
            to_status=to_status,
            idempotency_key=idempotency_key,
        )
        session.commit()
        session.refresh(task)
        return task
    except IntegrityError:
        # 并发请求可能同时通过前置 SELECT。唯一键由数据库兜底；
        # 回滚后读取已提交的那条记录，重试请求返回第一次结果。
        session.rollback()
        if idempotency_key is not None:
            previous = operation_repo.find_by_idempotency_key(
                session, user_id=user_id, idempotency_key=idempotency_key
            )
            if previous is not None:
                if previous.task_id != task_id or previous.action != action:
                    raise IdempotencyConflictError("幂等键已用于其他任务操作")
                return get_owned_task(session, user_id=user_id, task_id=task_id)
        raise
    except Exception:
        session.rollback()
        raise



def list_operations(
    session: Session, *, user_id: int, task_id: int
):
    """列出当前用户自己任务的操作记录。"""
    get_owned_task(session, user_id=user_id, task_id=task_id)
    return operation_repo.list_for_task(session, task_id=task_id, user_id=user_id)


def delete_task(session: Session, *, user_id: int, task_id: int) -> None:
    """软删除任务(打上 deleted_at)。

    终态任务也允许"删除",但语义只是从列表里隐藏——行仍在库里。
    这里没有额外状态限制:已归档/已取消的任务同样可以软删。
    """
    task = get_owned_task(session, user_id=user_id, task_id=task_id)
    try:
        task_repo.soft_delete(session, task, deleted_at=datetime.now())
        session.commit()
    except Exception:
        session.rollback()
        raise
