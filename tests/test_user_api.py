"""注册与登录接口的集成测试。

这些用例同时验证两件事:
1. 功能对不对(能注册、能登录、密码错了会拒绝)
2. 重构后的分层是否真的生效(业务异常有没有被正确翻译成 HTTP 状态码)

为什么这批测试值得先写:它们是后续继续重构的安全网。
只要这些用例保持绿色,就说明对外契约没被改坏。
"""

from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.models.user import Users

_USERNAME = "alice"
_PASSWORD = "secret123"


def _register(client: TestClient, username: str = _USERNAME, password: str = _PASSWORD):
    return client.post("/api/registry", json={"username": username, "password": password})


def _login(client: TestClient, username: str = _USERNAME, password: str = _PASSWORD):
    return client.post("/api/login", json={"username": username, "password": password})


def test_register_success(client: TestClient, session: Session) -> None:
    """注册成功:返回 200,响应里带 id,且不含 password_hash。"""
    response = _register(client)

    assert response.status_code == 200
    body = response.json()
    assert body["code"] == 200
    assert body["data"]["username"] == _USERNAME
    assert body["data"]["id"] > 0
    # DTO 白名单的实际效果:哈希字段根本不在响应里
    assert "password_hash" not in body["data"]


def test_register_stores_hashed_password(client: TestClient, session: Session) -> None:
    """库里存的必须是哈希,不能是明文。"""
    _register(client)

    user = session.exec(select(Users).where(Users.username == _USERNAME)).first()
    assert user is not None
    assert user.password_hash != _PASSWORD
    # bcrypt 哈希固定以 $2 开头
    assert user.password_hash.startswith("$2")


def test_register_duplicate_username_returns_409(client: TestClient) -> None:
    """重名注册:业务层抛 UsernameTakenError,全局处理器翻译成 409。"""
    _register(client)
    response = _register(client)

    assert response.status_code == 409
    assert response.json()["error"] == "USERNAME_TAKEN"


def test_register_rejects_short_password(client: TestClient) -> None:
    """密码太短:被 schema 的 Field(min_length=6) 拦下,返回 422。

    业务代码里没有任何长度判断——校验属于输入层的职责。
    """
    response = _register(client, password="123")

    assert response.status_code == 422


def test_login_success_returns_token(client: TestClient) -> None:
    """登录成功:返回可用的 access_token。"""
    _register(client)
    response = _login(client)

    assert response.status_code == 200
    body = response.json()
    assert body["code"] == 200
    assert body["data"]["token_type"] == "bearer"
    assert len(body["data"]["access_token"]) > 0


def test_login_wrong_password_returns_401(client: TestClient) -> None:
    """密码错误:401,且提示文案不透露"用户存在"这一信息。"""
    _register(client)
    response = _login(client, password="wrong-password")

    assert response.status_code == 401
    body = response.json()
    assert body["error"] == "INVALID_CREDENTIALS"
    assert body["message"] == "用户名或密码错误"


def test_login_unknown_user_returns_same_error(client: TestClient) -> None:
    """用户不存在:响应必须和"密码错误"完全一致,防止用户名被枚举。"""
    _register(client)
    wrong_password = _login(client, password="wrong-password").json()
    unknown_user = _login(client, username="nobody").json()

    assert wrong_password["error"] == unknown_user["error"]
    assert wrong_password["message"] == unknown_user["message"]


def test_protected_route_rejects_missing_token(client: TestClient) -> None:
    """未带 token 访问受保护接口:401 + WWW-Authenticate 响应头。

    这条用例守的是 app/api/deps.py:HTTP 协议细节留在 API 层,
    而 token 解码逻辑在 services/auth.py。
    """
    response = client.get("/api/messages")

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"


def test_protected_route_accepts_login_token(client: TestClient) -> None:
    """带上登录拿到的 token:能正常访问受保护接口。

    这条用例把"签发"和"校验"两端连起来验证,
    确认拆分 services/auth.py 与 api/deps.py 之后链路仍然通。
    """
    _register(client)
    token = _login(client).json()["data"]["access_token"]

    response = client.get(
        "/api/messages", headers={"Authorization": f"Bearer {token}"}
    )

    assert response.status_code == 200
    assert response.json() == []


def test_protected_route_rejects_forged_token(client: TestClient) -> None:
    """伪造的 token:签名校验失败,同样 401。"""
    response = client.get(
        "/api/messages", headers={"Authorization": "Bearer not-a-real-token"}
    )

    assert response.status_code == 401
