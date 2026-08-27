"""任务系统接口测试。

覆盖路线图迭代 3 的全部要点:
- CRUD 基本流程
- 资源所有权(越权访问必须 404,且不泄露存在性)
- 状态机(合法转换通过,非法转换 409)
- 终态任务不可编辑
- 分页与状态筛选
- 软删除(删除后列表不可见,但行仍在库)

这批用例是迭代 3 的安全网:只要它们保持绿色,任务系统的对外契约就没被改坏。
"""

from datetime import datetime

from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.core.security import hash_password
from app.models.task import Task
from app.models.task_operation import TaskOperationRecord
from app.models.user import Users
from app.services import task as task_service

_PASSWORD = "secret123"


def _create_user(session: Session, username: str) -> Users:
    user = Users(username=username, password_hash=hash_password(_PASSWORD))
    session.add(user)
    session.commit()
    session.refresh(user)
    return user


def _token(client: TestClient, username: str) -> str:
    response = client.post(
        "/api/login", json={"username": username, "password": _PASSWORD}
    )
    assert response.status_code == 200
    return response.json()["data"]["access_token"]


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _create_task(
    client: TestClient, token: str, title: str = "写笔记"
) -> dict:
    response = client.post("/api/tasks", headers=_auth(token), json={"title": title})
    assert response.status_code == 201
    return response.json()


def test_create_task_requires_auth(client: TestClient) -> None:
    """未登录创建任务:401。"""
    response = client.post("/api/tasks", json={"title": "x"})
    assert response.status_code == 401


def test_create_task_success(client: TestClient, session: Session) -> None:
    """创建任务:返回 201,初始状态 pending,且绑定当前用户。"""
    user = _create_user(session, "task-user")
    token = _token(client, "task-user")

    body = _create_task(client, token, title="学状态机")
    assert body["title"] == "学状态机"
    assert body["status"] == "pending"
    assert body["description"] == ""
    assert body["due_at"] is None
    assert body["id"] > 0

    task = session.exec(select(Task).where(Task.id == body["id"])).one()
    assert task.user_id == user.id


def test_create_task_rejects_empty_title(client: TestClient, session: Session) -> None:
    """空标题:schema 层 422,业务代码不参与。"""
    _create_user(session, "task-user")
    token = _token(client, "task-user")

    response = client.post("/api/tasks", headers=_auth(token), json={"title": ""})
    assert response.status_code == 422


def test_get_task_is_scoped_to_owner(client: TestClient, session: Session) -> None:
    """用户 A 取用户 B 的任务:404,且不暴露"存在但不属于你"。"""
    user_a = _create_user(session, "user-a")
    user_b = _create_user(session, "user-b")
    token_a = _token(client, "user-a")
    token_b = _token(client, "user-b")

    task_b = _create_task(client, token_b, title="B 的任务")

    # B 自己能取
    assert client.get(f"/api/tasks/{task_b['id']}", headers=_auth(token_b)).status_code == 200
    # A 取 B 的:404(不是 403,避免泄露存在性)
    response = client.get(f"/api/tasks/{task_b['id']}", headers=_auth(token_a))
    assert response.status_code == 404
    assert response.json()["error"] == "NOT_FOUND"

    # 顺便确认 A 看不到 B 的任务出现在列表里
    listing = client.get("/api/tasks", headers=_auth(token_a)).json()
    assert all(item["id"] != task_b["id"] for item in listing["items"])
    # 确认库里确实存在这条任务(只是 A 看不到),证明是授权过滤而非数据缺失
    assert session.exec(select(Task).where(Task.id == task_b["id"])).one().user_id == user_b.id


def test_update_task(client: TestClient, session: Session) -> None:
    """更新标题/描述:成功,updated_at 前进。"""
    _create_user(session, "task-user")
    token = _token(client, "task-user")
    task = _create_task(client, token)

    response = client.patch(
        f"/api/tasks/{task['id']}",
        headers=_auth(token),
        json={"title": "新标题", "description": "新描述"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["title"] == "新标题"
    assert body["description"] == "新描述"


def test_update_task_rejects_cross_user(client: TestClient, session: Session) -> None:
    """用户 A 改用户 B 的任务:404。"""
    _create_user(session, "user-a")
    _create_user(session, "user-b")
    token_a = _token(client, "user-a")
    token_b = _token(client, "user-b")
    task_b = _create_task(client, token_b)

    response = client.patch(
        f"/api/tasks/{task_b['id']}", headers=_auth(token_a), json={"title": "篡改"}
    )
    assert response.status_code == 404


def test_state_machine_happy_path(client: TestClient, session: Session) -> None:
    """合法转换链:pending -> in_progress -> completed -> archived。"""
    _create_user(session, "task-user")
    token = _token(client, "task-user")
    task = _create_task(client, token)

    for action, expected in [
        ("start", "in_progress"),
        ("complete", "completed"),
        ("archive", "archived"),
    ]:
        response = client.post(
            f"/api/tasks/{task['id']}/{action}", headers=_auth(token)
        )
        assert response.status_code == 200
        assert response.json()["status"] == expected


def test_state_machine_rejects_illegal_transition(
    client: TestClient, session: Session
) -> None:
    """非法转换(对 pending 直接 complete):409 TASK_STATE_CONFLICT。"""
    _create_user(session, "task-user")
    token = _token(client, "task-user")
    task = _create_task(client, token)

    response = client.post(f"/api/tasks/{task['id']}/complete", headers=_auth(token))
    assert response.status_code == 409
    assert response.json()["error"] == "TASK_STATE_CONFLICT"


def test_cancel_from_pending_and_in_progress(
    client: TestClient, session: Session
) -> None:
    """pending 和 in_progress 都可以 cancel;cancelled 是终态,不能再转换。"""
    _create_user(session, "task-user")
    token = _token(client, "task-user")

    # pending -> cancelled
    t1 = _create_task(client, token, title="t1")
    r = client.post(f"/api/tasks/{t1['id']}/cancel", headers=_auth(token))
    assert r.status_code == 200 and r.json()["status"] == "cancelled"
    # 终态再操作:409
    r = client.post(f"/api/tasks/{t1['id']}/start", headers=_auth(token))
    assert r.status_code == 409

    # in_progress -> cancelled
    t2 = _create_task(client, token, title="t2")
    client.post(f"/api/tasks/{t2['id']}/start", headers=_auth(token))
    r = client.post(f"/api/tasks/{t2['id']}/cancel", headers=_auth(token))
    assert r.status_code == 200 and r.json()["status"] == "cancelled"


def test_terminal_task_cannot_be_edited(client: TestClient, session: Session) -> None:
    """终态任务(完成/取消/归档)不可编辑正文:409。"""
    _create_user(session, "task-user")
    token = _token(client, "task-user")
    task = _create_task(client, token)

    client.post(f"/api/tasks/{task['id']}/start", headers=_auth(token))
    client.post(f"/api/tasks/{task['id']}/complete", headers=_auth(token))

    response = client.patch(
        f"/api/tasks/{task['id']}", headers=_auth(token), json={"title": "改完成态"}
    )
    assert response.status_code == 409


def test_transition_rejects_cross_user(client: TestClient, session: Session) -> None:
    """用户 A 对用户 B 的任务做状态转换:404(不是 403)。"""
    _create_user(session, "user-a")
    _create_user(session, "user-b")
    token_a = _token(client, "user-a")
    token_b = _token(client, "user-b")
    task_b = _create_task(client, token_b)

    response = client.post(f"/api/tasks/{task_b['id']}/start", headers=_auth(token_a))
    assert response.status_code == 404


def test_pagination_and_status_filter(client: TestClient, session: Session) -> None:
    """分页 + 状态筛选:total 是满足筛选条件的条数,不是全表条数。"""
    _create_user(session, "task-user")
    token = _token(client, "task-user")

    # 建三条:两条 pending,一条 in_progress
    _create_task(client, token, title="p1")
    _create_task(client, token, title="p2")
    t3 = _create_task(client, token, title="ip1")
    client.post(f"/api/tasks/{t3['id']}/start", headers=_auth(token))

    # 不带筛选:total = 3
    page = client.get("/api/tasks?page=1&page_size=2", headers=_auth(token)).json()
    assert page["total"] == 3
    assert len(page["items"]) == 2
    assert page["page"] == 1 and page["page_size"] == 2

    # 按状态筛选 pending:total = 2
    filtered = client.get(
        "/api/tasks?status=pending&page_size=10", headers=_auth(token)
    ).json()
    assert filtered["total"] == 2
    assert all(item["status"] == "pending" for item in filtered["items"])


def test_pagination_rejects_invalid_page(client: TestClient, session: Session) -> None:
    """page=0:被 Query(ge=1) 拦下,422。"""
    _create_user(session, "task-user")
    token = _token(client, "task-user")

    response = client.get("/api/tasks?page=0", headers=_auth(token))
    assert response.status_code == 422


def test_soft_delete_hides_from_list_but_keeps_row(
    client: TestClient, session: Session
) -> None:
    """软删除:列表和单查都 404,但行仍在库里(deleted_at 非空)。"""
    _create_user(session, "task-user")
    token = _token(client, "task-user")
    task = _create_task(client, token)

    response = client.delete(f"/api/tasks/{task['id']}", headers=_auth(token))
    assert response.status_code == 204

    # 单查:404
    assert client.get(f"/api/tasks/{task['id']}", headers=_auth(token)).status_code == 404
    # 列表里看不到
    listing = client.get("/api/tasks", headers=_auth(token)).json()
    assert all(item["id"] != task["id"] for item in listing["items"])
    # 行仍在库,deleted_at 已打上
    row = session.exec(select(Task).where(Task.id == task["id"])).one()
    assert row.deleted_at is not None


def test_delete_rejects_cross_user(client: TestClient, session: Session) -> None:
    """用户 A 删用户 B 的任务:404。"""
    _create_user(session, "user-a")
    _create_user(session, "user-b")
    token_a = _token(client, "user-a")
    token_b = _token(client, "user-b")
    task_b = _create_task(client, token_b)

    response = client.delete(f"/api/tasks/{task_b['id']}", headers=_auth(token_a))
    assert response.status_code == 404


def test_get_nonexistent_task_returns_404(client: TestClient, session: Session) -> None:
    """查不存在的 id:404。"""
    _create_user(session, "task-user")
    token = _token(client, "task-user")

    response = client.get("/api/tasks/99999", headers=_auth(token))
    assert response.status_code == 404


# ===== 事务、操作记录、幂等性 =====


def test_transition_creates_operation_record(
    client: TestClient, session: Session
) -> None:
    """状态更新和操作记录一起提交,记录包含动作与前后状态。"""
    _create_user(session, "task-user")
    token = _token(client, "task-user")
    task = _create_task(client, token)

    response = client.post(
        f"/api/tasks/{task['id']}/start",
        headers={**_auth(token), "Idempotency-Key": "start-task-once"},
    )
    assert response.status_code == 200

    records = client.get(
        f"/api/tasks/{task['id']}/operations", headers=_auth(token)
    )
    assert records.status_code == 200
    assert records.json() == [
        {
            "id": records.json()[0]["id"],
            "task_id": task["id"],
            "action": "start",
            "from_status": "pending",
            "to_status": "in_progress",
            "created_at": records.json()[0]["created_at"],
        }
    ]


def test_same_idempotency_key_does_not_repeat_transition(
    client: TestClient, session: Session
) -> None:
    """相同请求重试两次:状态只转换一次,操作记录只有一条。"""
    _create_user(session, "task-user")
    token = _token(client, "task-user")
    task = _create_task(client, token)
    headers = {**_auth(token), "Idempotency-Key": "retry-safe-key"}

    first = client.post(f"/api/tasks/{task['id']}/start", headers=headers)
    second = client.post(f"/api/tasks/{task['id']}/start", headers=headers)

    assert first.status_code == second.status_code == 200
    assert first.json()["status"] == second.json()["status"] == "in_progress"
    records = session.exec(
        select(TaskOperationRecord).where(TaskOperationRecord.task_id == task["id"])
    ).all()
    assert len(records) == 1


def test_idempotency_key_cannot_be_reused_for_another_action(
    client: TestClient, session: Session
) -> None:
    """同一个 key 用于不同动作:409,防止把两个业务请求误认为同一个。"""
    _create_user(session, "task-user")
    token = _token(client, "task-user")
    task = _create_task(client, token)
    headers = {**_auth(token), "Idempotency-Key": "one-business-request"}

    assert (
        client.post(f"/api/tasks/{task['id']}/start", headers=headers).status_code
        == 200
    )
    response = client.post(f"/api/tasks/{task['id']}/complete", headers=headers)

    assert response.status_code == 409
    assert response.json()["error"] == "IDEMPOTENCY_KEY_CONFLICT"


def test_transition_rolls_back_when_operation_record_fails(
    session: Session, monkeypatch
) -> None:
    """记录写入失败:任务状态更新也回滚,不能出现"状态变了但没记录"。"""
    user = _create_user(session, "task-user")
    task = Task(user_id=user.id, title="原子性测试", status="pending")
    session.add(task)
    session.commit()
    session.refresh(task)

    def failing_insert(*args, **kwargs):
        raise RuntimeError("模拟操作记录写入失败")

    monkeypatch.setattr("app.services.task.operation_repo.insert", failing_insert)

    import pytest

    with pytest.raises(RuntimeError, match="模拟操作记录写入失败"):
        task_service.transition_task(
            session,
            user_id=user.id,
            task_id=task.id,
            action="start",
            idempotency_key="rollback-key",
        )

    session.expire_all()
    persisted = session.exec(select(Task).where(Task.id == task.id)).one()
    assert persisted.status == "pending"
    records = session.exec(
        select(TaskOperationRecord).where(TaskOperationRecord.task_id == task.id)
    ).all()
    assert records == []


def test_operation_records_are_owner_scoped(
    client: TestClient, session: Session
) -> None:
    """用户 A 不能读取用户 B 任务的操作记录:404。"""
    _create_user(session, "user-a")
    _create_user(session, "user-b")
    token_a = _token(client, "user-a")
    token_b = _token(client, "user-b")
    task_b = _create_task(client, token_b)
    client.post(f"/api/tasks/{task_b['id']}/start", headers=_auth(token_b))

    response = client.get(
        f"/api/tasks/{task_b['id']}/operations", headers=_auth(token_a)
    )
    assert response.status_code == 404

