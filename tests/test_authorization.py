"""资源所有权授权测试。"""

from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.core.security import hash_password
from app.models.message import ChatMessage
from app.models.user import Users


def _create_user(session: Session, username: str) -> Users:
    user = Users(username=username, password_hash=hash_password("secret123"))
    session.add(user)
    session.commit()
    session.refresh(user)
    return user


def _token(client: TestClient, username: str) -> str:
    response = client.post(
        "/api/login", json={"username": username, "password": "secret123"}
    )
    assert response.status_code == 200
    return response.json()["data"]["access_token"]


def test_messages_are_limited_to_current_user(
    client: TestClient, session: Session
) -> None:
    """用户 A 登录时只能看到 A 的消息,看不到 B 的消息。"""
    user_a = _create_user(session, "user-a")
    user_b = _create_user(session, "user-b")
    session.add_all(
        [
            ChatMessage(user_id=user_a.id, role="user", content="A 的私密消息"),
            ChatMessage(user_id=user_b.id, role="user", content="B 的私密消息"),
        ]
    )
    session.commit()

    response = client.get(
        "/api/messages", headers={"Authorization": f"Bearer {_token(client, 'user-a')}"}
    )

    assert response.status_code == 200
    assert [item["content"] for item in response.json()] == ["A 的私密消息"]
    assert "B 的私密消息" not in response.text


def test_chat_messages_are_owned_by_authenticated_user(
    client: TestClient, session: Session, monkeypatch
) -> None:
    """新聊天写入的 user/assistant 消息都绑定当前登录用户。"""
    user = _create_user(session, "chat-owner")

    async def fake_ask(message: str) -> str:
        return "回复"

    monkeypatch.setattr("app.services.chat.ask_llm", fake_ask)
    response = client.post(
        "/api/chat",
        headers={"Authorization": f"Bearer {_token(client, 'chat-owner')}"},
        json={"message": "问题"},
    )

    assert response.status_code == 200
    messages = session.exec(select(ChatMessage).order_by(ChatMessage.id)).all()
    assert len(messages) == 2
    assert {message.user_id for message in messages} == {user.id}
