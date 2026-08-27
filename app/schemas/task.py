"""任务相关接口的请求/响应结构(DTO)。

为什么要独立出 schemas 层,不直接拿 ORM 模型当返回值:

1. 安全。Task 模型里有 user_id、deleted_at 这种内部字段。直接返回 ORM
   对象,一旦哪天忘了排除,内部字段就泄露了。用独立 DTO 是"白名单"思路——
   只有写进来的字段才会出去,默认安全。
2. 解耦。数据库表结构和 API 契约是两件事。表里加一个内部字段(比如来源渠道),
   不该自动出现在 API 响应里。
3. 可读。看一眼 schemas 就知道接口收什么、返什么,不用去翻表定义。

注意:本文件不 import app.models,这是架构测试(test_schemas_do_not_import_models)
守的硬性边界——DTO 层必须独立于 ORM 模型。
"""

from datetime import datetime

from pydantic import BaseModel, Field


class TaskCreate(BaseModel):
    """创建任务的请求体。

    Field 约束就是"输入校验"这一层的职责,和数据库列宽对齐:
    超长标题在 schema 层就 422,而不是一路走到 INSERT 才被数据库拒绝报 500。
    description 给上限是为了挡住"提交几 MB 正文"这种廉价 DoS。
    """

    title: str = Field(min_length=1, max_length=255)
    description: str = Field(default="", max_length=20000)
    due_at: datetime | None = Field(default=None)


class TaskUpdate(BaseModel):
    """更新任务的请求体。

    所有字段都可选(PATCH 语义):只传想改的字段,没传的保持不变。
    title 用 min_length=1 防止"传空字符串把标题清空"这种误操作。
    """

    title: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = Field(default=None, max_length=20000)
    due_at: datetime | None = Field(default=None)


class TaskPublic(BaseModel):
    """可以对外暴露的任务信息。注意:没有 user_id、没有 deleted_at。"""

    id: int
    title: str
    description: str
    status: str
    due_at: datetime | None
    created_at: datetime
    updated_at: datetime


class TaskPage(BaseModel):
    """分页响应外壳。

    统一分页格式,前端不用每个接口猜一次结构:
        { items: [...], total: 100, page: 1, page_size: 20 }
    total 是"满足筛选条件的总条数",不是全表条数——这样前端能正确算总页数。
    """

    items: list[TaskPublic]
    total: int
    page: int
    page_size: int


class TaskOperationPublic(BaseModel):
    """任务状态变更记录的公开字段。"""

    id: int
    task_id: int
    action: str
    from_status: str
    to_status: str
    created_at: datetime
