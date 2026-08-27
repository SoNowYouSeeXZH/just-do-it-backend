"""任务接口模块:CRUD + 状态流转。

这一层只做三件事,一行业务逻辑都没有:
1. 声明 HTTP 契约(路径、方法、请求体、响应模型、查询参数校验)
2. 把请求参数转交给 service
3. 把 service 的返回值装进响应结构

业务异常(NotFound / TaskStateError)由 main.py 注册的全局处理器统一翻译,
所以这里看不到一个 try/except。

资源命名沿用项目既有约定(/api/tasks),版本前缀(/api/v1)留作后续迭代。
"""

from typing import Annotated

from fastapi import APIRouter, Depends, Header, Query

from app.api.deps import get_current_user_id
from app.db import SessionDep
from app.schemas.task import (
    TaskCreate,
    TaskOperationPublic,
    TaskPage,
    TaskPublic,
    TaskUpdate,
)
from app.services import task as task_service

router = APIRouter(prefix="/api", tags=["tasks"])


@router.post("/tasks", response_model=TaskPublic, status_code=201)
def create_task(
    session: SessionDep,
    user_id: Annotated[int, Depends(get_current_user_id)],
    req: TaskCreate,
) -> TaskPublic:
    """创建任务,需要登录。"""
    task = task_service.create_task(
        session,
        user_id=user_id,
        title=req.title,
        description=req.description,
        due_at=req.due_at,
    )
    return TaskPublic.model_validate(task, from_attributes=True)


@router.get("/tasks", response_model=TaskPage)
def list_tasks(
    session: SessionDep,
    user_id: Annotated[int, Depends(get_current_user_id)],
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
    status: Annotated[str | None, Query(max_length=16)] = None,
) -> TaskPage:
    """分页取当前用户的任务,可按状态筛选。"""
    items, total = task_service.list_tasks(
        session, user_id=user_id, page=page, page_size=page_size, status=status
    )
    return TaskPage(
        items=[
            TaskPublic.model_validate(task, from_attributes=True) for task in items
        ],
        total=total,
        page=page,
        page_size=page_size,
    )


@router.get("/tasks/{task_id}", response_model=TaskPublic)
def get_task(
    session: SessionDep,
    user_id: Annotated[int, Depends(get_current_user_id)],
    task_id: int,
) -> TaskPublic:
    """取单个任务,不存在或不属于当前用户返回 404。"""
    task = task_service.get_task(session, user_id=user_id, task_id=task_id)
    return TaskPublic.model_validate(task, from_attributes=True)


@router.patch("/tasks/{task_id}", response_model=TaskPublic)
def update_task(
    session: SessionDep,
    user_id: Annotated[int, Depends(get_current_user_id)],
    task_id: int,
    req: TaskUpdate,
) -> TaskPublic:
    """更新任务标题/描述/截止时间。终态任务(完成/取消/归档)不可编辑,返回 409。"""
    task = task_service.update_task(
        session,
        user_id=user_id,
        task_id=task_id,
        title=req.title,
        description=req.description,
        due_at=req.due_at,
    )
    return TaskPublic.model_validate(task, from_attributes=True)


@router.get("/tasks/{task_id}/operations", response_model=list[TaskOperationPublic])
def list_operations(
    session: SessionDep,
    user_id: Annotated[int, Depends(get_current_user_id)],
    task_id: int,
) -> list[TaskOperationPublic]:
    """查看当前用户任务的状态变更记录。"""
    records = task_service.list_operations(
        session, user_id=user_id, task_id=task_id
    )
    return [
        TaskOperationPublic.model_validate(record, from_attributes=True)
        for record in records
    ]


@router.post("/tasks/{task_id}/{action}", response_model=TaskPublic)
def transition_task(
    session: SessionDep,
    user_id: Annotated[int, Depends(get_current_user_id)],
    task_id: int,
    action: str,
    idempotency_key: Annotated[
        str | None, Header(alias="Idempotency-Key", max_length=128)
    ] = None,
) -> TaskPublic:
    """推进任务状态。action 取值:start / complete / cancel / archive。

    用 POST + 路径里的动作名,而不是 PATCH 改 status 字段:
    状态转换是"动作"不是"字段赋值",直接让客户端传任意 status 会绕过状态机。
    合法动作与允许的转换见 services/task.py 的 _TRANSITIONS。
    """
    task = task_service.transition_task(
        session, user_id=user_id, task_id=task_id, action=action,
        idempotency_key=idempotency_key,
    )
    return TaskPublic.model_validate(task, from_attributes=True)


@router.delete("/tasks/{task_id}", status_code=204)
def delete_task(
    session: SessionDep,
    user_id: Annotated[int, Depends(get_current_user_id)],
    task_id: int,
) -> None:
    """软删除任务。已删除后不再出现在列表里,但行仍在库中。"""
    task_service.delete_task(session, user_id=user_id, task_id=task_id)
