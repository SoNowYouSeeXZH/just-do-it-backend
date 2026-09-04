"""聊天与题库写入接口的集成测试。

聊天接口依赖大模型,测试里用 monkeypatch 替换掉 llm 调用——
这是「替换外部依赖」的另一个例子:不 mock 数据库,但必须 mock
真正的第三方网络服务(慢、要钱、结果不确定)。
"""

from collections.abc import AsyncIterator

import pytest
from fastapi.testclient import TestClient
from openai import OpenAIError
from sqlmodel import Session, select

from app.models.job import Job
from app.models.message import ChatMessage


def _chat_headers(client: TestClient, username: str = "chat-user") -> dict[str, str]:
    """给聊天接口准备一个真实 JWT,不绕过鉴权测试。"""
    registered = client.post(
        "/api/registry", json={"username": username, "password": "secret123"}
    )
    assert registered.status_code == 200
    login = client.post(
        "/api/login", json={"username": username, "password": "secret123"}
    )
    assert login.status_code == 200
    token = login.json()["data"]["access_token"]
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture(name="fake_llm")
def fake_llm_fixture(monkeypatch: pytest.MonkeyPatch) -> None:
    """把大模型调用替换成固定回复,并显式覆盖旧链路。"""
    monkeypatch.setattr("app.services.chat.settings.rag_enabled", False)

    async def fake_ask(message: str) -> str:
        return f"回答: {message}"

    async def fake_stream(message: str) -> AsyncIterator[str]:
        for chunk in ("回答", ": ", message):
            yield chunk

    monkeypatch.setattr("app.services.chat.ask_llm", fake_ask)
    monkeypatch.setattr("app.services.chat.ask_llm_stream", fake_stream)


def test_chat_returns_reply_and_persists(
    client: TestClient, session: Session, fake_llm: None
) -> None:
    """非流式对话:返回回复,并把 user/assistant 两条消息落库。"""
    response = client.post(
        "/api/chat", headers=_chat_headers(client), json={"message": "你好"}
    )

    assert response.status_code == 200
    assert response.json()["reply"] == "回答: 你好"

    messages = session.exec(select(ChatMessage).order_by(ChatMessage.id)).all()
    assert [m.role for m in messages] == ["user", "assistant"]
    assert messages[0].content == "你好"


def test_chat_stream_emits_sse_events(client: TestClient, fake_llm: None) -> None:
    """流式对话:返回 SSE 事件流,以 [DONE] 结束。"""
    response = client.post(
        "/api/chat",
        headers=_chat_headers(client),
        json={"message": "你好", "stream": True},
    )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert "data: [DONE]" in response.text


def test_chat_rag_returns_agent_reply(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """RAG 开关开启时,非流式请求走 Agent。"""
    monkeypatch.setattr("app.services.chat.settings.rag_enabled", True)

    async def fake_answer(message: str) -> str:
        return f"攻略回答: {message}\n\n---\n来源：\n[1] wiki"

    monkeypatch.setattr("app.services.chat.rag_agent.answer", fake_answer)
    response = client.post(
        "/api/chat", headers=_chat_headers(client), json={"message": "纳塔"}
    )

    assert response.status_code == 200
    assert response.json()["reply"].startswith("攻略回答: 纳塔")


def test_chat_rag_stream_adapts_agent_events(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """RAG 事件转换为兼容旧客户端的 stage/delta SSE。"""
    monkeypatch.setattr("app.services.chat.settings.rag_enabled", True)

    async def fake_stream(message: str) -> AsyncIterator[object]:
        from app.services.rag.agent import DeltaEvent, StageEvent

        yield StageEvent(stage="retrieving", detail="正在搜索：纳塔")
        yield StageEvent(stage="answering")
        yield DeltaEvent(text="攻略")
        yield DeltaEvent(text="内容")

    monkeypatch.setattr("app.services.chat.rag_agent.answer_stream", fake_stream)
    response = client.post(
        "/api/chat",
        headers=_chat_headers(client),
        json={"message": "纳塔", "stream": True},
    )

    assert response.status_code == 200
    assert '"stage": "retrieving"' in response.text
    assert '"detail": "正在搜索：纳塔"' in response.text
    assert '"stage": "answering"' in response.text
    assert '"delta": "攻略"' in response.text
    assert '"delta": "内容"' in response.text
    assert "data: [DONE]" in response.text


def test_chat_rag_upstream_failure_returns_502(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """RAG 非流式故障仍返回脱敏的 502。"""
    monkeypatch.setattr("app.services.chat.settings.rag_enabled", True)

    async def failing_answer(message: str) -> str:
        raise OpenAIError("Incorrect API key provided: sk-rag-secret")

    monkeypatch.setattr("app.services.chat.rag_agent.answer", failing_answer)
    response = client.post(
        "/api/chat", headers=_chat_headers(client), json={"message": "纳塔"}
    )

    assert response.status_code == 502
    assert response.json()["error"] == "UPSTREAM_ERROR"
    assert "sk-rag-secret" not in response.text


def test_chat_rag_stream_failure_does_not_persist_partial_reply(
    client: TestClient, session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """RAG 流式中途失败时发送错误和 DONE,但不保存半截 assistant。"""
    monkeypatch.setattr("app.services.chat.settings.rag_enabled", True)

    async def failing_stream(message: str) -> AsyncIterator[object]:
        from app.services.rag.agent import DeltaEvent, StageEvent

        yield StageEvent(stage="retrieving")
        yield DeltaEvent(text="半截回答")
        raise OpenAIError("upstream key=sk-rag-secret")

    monkeypatch.setattr("app.services.chat.rag_agent.answer_stream", failing_stream)
    response = client.post(
        "/api/chat",
        headers=_chat_headers(client),
        json={"message": "纳塔", "stream": True},
    )

    assert response.status_code == 200
    assert '"delta": "半截回答"' in response.text
    assert '"error": "智能问答服务暂时不可用,请稍后再试"' in response.text
    assert "data: [DONE]" in response.text
    messages = session.exec(select(ChatMessage).order_by(ChatMessage.id)).all()
    assert [item.role for item in messages] == ["user"]


def test_chat_rejects_empty_message(client: TestClient, fake_llm: None) -> None:
    """空消息:被 Field(min_length=1) 拦下,返回 422。"""
    response = client.post(
        "/api/chat", headers=_chat_headers(client), json={"message": ""}
    )

    assert response.status_code == 422


def test_chat_rejects_oversized_message(client: TestClient, fake_llm: None) -> None:
    """超长消息:重构时新加的约束,防止把大字符串转发给按 token 计费的接口。"""
    response = client.post(
        "/api/chat", headers=_chat_headers(client), json={"message": "x" * 3000}
    )

    assert response.status_code == 422


def test_chat_upstream_failure_returns_502(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """大模型故障:抛 UpstreamServiceError → 502,且不泄露上游原始错误。"""
    monkeypatch.setattr("app.services.chat.settings.rag_enabled", False)

    async def failing_ask(message: str) -> str:
        raise OpenAIError("Incorrect API key provided: sk-abc123")

    monkeypatch.setattr("app.services.chat.ask_llm", failing_ask)

    response = client.post(
        "/api/chat", headers=_chat_headers(client), json={"message": "你好"}
    )

    assert response.status_code == 502
    body = response.json()
    assert body["error"] == "UPSTREAM_ERROR"
    # 关键断言:API Key 片段绝不能出现在响应里
    assert "sk-abc123" not in response.text


# ===== 题库写入 =====

_QUESTION = {
    "qtype": "single",
    "prompt": "HTTP 状态码 404 表示什么?",
    "options": ["成功", "未找到", "服务器错误", "重定向"],
    "answer_indices": [1],
    "explanation": "404 Not Found 表示资源不存在",
}


def test_batch_without_configured_key_returns_503(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """没配 ADMIN_API_KEY 时抛 ConfigurationError → 503。

    fail closed:配置缺失时拒绝服务,而不是放行。

    注意必须显式把 key 置空,不能依赖「本地 .env 里没配」——
    实际上 .env 里是配了的,依赖环境的测试会随机失败。
    """
    monkeypatch.setattr("app.api.admin.settings.admin_api_key", "")

    response = client.post(
        "/api/admin/questions/batch",
        json={"job_id": "frontend", "questions": [_QUESTION]},
    )

    assert response.status_code == 503


def test_batch_missing_api_key_returns_401(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """配了 key 但请求没带:401。"""
    monkeypatch.setattr("app.api.admin.settings.admin_api_key", "right-key")

    response = client.post(
        "/api/admin/questions/batch",
        json={"job_id": "frontend", "questions": [_QUESTION]},
    )

    assert response.status_code == 401


def test_batch_rejects_wrong_api_key(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("app.api.admin.settings.admin_api_key", "right-key")

    response = client.post(
        "/api/admin/questions/batch",
        headers={"X-API-Key": "wrong-key"},
        json={"job_id": "frontend", "questions": [_QUESTION]},
    )

    assert response.status_code == 401


def test_batch_creates_and_dedups(
    client: TestClient, session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """首次写入 created,重复提交同一道题 skipped。"""
    monkeypatch.setattr("app.api.admin.settings.admin_api_key", "test-key")
    session.add(
        Job(id="frontend", title="前端", emoji="🎨", tagline="tl", accent="#000")
    )
    session.commit()

    payload = {"job_id": "frontend", "questions": [_QUESTION]}
    headers = {"X-API-Key": "test-key"}

    first = client.post("/api/admin/questions/batch", headers=headers, json=payload)
    assert first.status_code == 200
    assert first.json()["created"] == 1

    second = client.post("/api/admin/questions/batch", headers=headers, json=payload)
    assert second.status_code == 200
    assert second.json()["skipped"] == 1


def test_batch_unknown_job_returns_404(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("app.api.admin.settings.admin_api_key", "test-key")

    response = client.post(
        "/api/admin/questions/batch",
        headers={"X-API-Key": "test-key"},
        json={"job_id": "nope", "questions": [_QUESTION]},
    )

    assert response.status_code == 404


def test_batch_reports_per_question_failure(
    client: TestClient, session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """一道题不合格不影响其余题目:逐题返回结果。"""
    monkeypatch.setattr("app.api.admin.settings.admin_api_key", "test-key")
    session.add(
        Job(id="frontend", title="前端", emoji="🎨", tagline="tl", accent="#000")
    )
    session.commit()

    bad_question = {**_QUESTION, "answer_indices": [99]}  # 越界下标
    response = client.post(
        "/api/admin/questions/batch",
        headers={"X-API-Key": "test-key"},
        json={"job_id": "frontend", "questions": [_QUESTION, bad_question]},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["created"] == 1
    assert body["failed"] == 1
    assert body["results"][1]["reason"] == "answer_indices 包含越界下标"


def test_batch_rejects_unknown_field(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """extra="forbid":传了未定义字段直接 422,而不是静默忽略。"""
    monkeypatch.setattr("app.api.admin.settings.admin_api_key", "test-key")

    response = client.post(
        "/api/admin/questions/batch",
        headers={"X-API-Key": "test-key"},
        json={
            "job_id": "frontend",
            "questions": [{**_QUESTION, "content_hash": "forged"}],
        },
    )

    assert response.status_code == 422
