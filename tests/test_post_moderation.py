"""社区内容审核的集成测试。

覆盖三件事:
1. 敏感词过滤把帖子降级为待审、把评论直接拒绝
2. 绕过手段(大小写/插入空格)同样被识别
3. 管理端审核队列与状态流转必须鉴权,且能把 pending 改成 published
"""

import pytest
from fastapi.testclient import TestClient

from app.models.post import POST_STATUS_PENDING, POST_STATUS_PUBLISHED

_ADMIN_HEADERS = {"X-API-Key": "test-key"}


def _auth_headers(client: TestClient, username: str = "mod-user") -> dict[str, str]:
    client.post("/api/registry", json={"username": username, "password": "secret123"})
    login = client.post(
        "/api/login", json={"username": username, "password": "secret123"}
    )
    return {"Authorization": f"Bearer {login.json()['data']['access_token']}"}


_CLEAN_POST = {
    "game_slug": "ys",
    "title": "纳塔探索路线",
    "content": "先做主线再解锁传送点。",
    "city": None,
}


def test_banned_word_forces_post_into_review(client: TestClient) -> None:
    created = client.post(
        "/api/posts",
        headers=_auth_headers(client),
        json={**_CLEAN_POST, "content": "承接代练，价格便宜"},
    )

    # 即使 community_auto_publish=True,命中敏感词也只能进待审
    assert created.status_code == 200
    assert created.json()["status"] == POST_STATUS_PENDING
    # 待审内容不进公开列表
    assert client.get("/api/posts").json() == []


def test_banned_word_detection_ignores_case_and_spacing(client: TestClient) -> None:
    created = client.post(
        "/api/posts",
        headers=_auth_headers(client),
        json={**_CLEAN_POST, "title": "外 挂 分享"},
    )

    assert created.json()["status"] == POST_STATUS_PENDING


def test_clean_post_is_published(client: TestClient) -> None:
    created = client.post("/api/posts", headers=_auth_headers(client), json=_CLEAN_POST)

    assert created.json()["status"] == POST_STATUS_PUBLISHED


def test_banned_word_rejects_comment(client: TestClient) -> None:
    headers = _auth_headers(client)
    post_id = client.post("/api/posts", headers=headers, json=_CLEAN_POST).json()["id"]

    rejected = client.post(
        f"/api/posts/{post_id}/comments", headers=headers, json={"content": "私服群号多少"}
    )

    assert rejected.status_code == 400
    # 错误响应里 code 是 HTTP 状态码,error 才是机器可读的业务标识
    assert rejected.json()["error"] == "CONTENT_REJECTED"
    # 不回显命中的敏感词,避免词表被逐次探测出来
    assert "私服" not in rejected.json()["message"]
    # 被拒绝的评论不入库,计数也不能涨
    assert client.get(f"/api/posts/{post_id}/comments").json() == []
    assert client.get(f"/api/posts/{post_id}").json()["comment_count"] == 0


def test_extra_banned_words_from_settings(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "app.services.moderation.settings.moderation_extra_banned_words", ["内测码"]
    )
    created = client.post(
        "/api/posts",
        headers=_auth_headers(client),
        json={**_CLEAN_POST, "content": "求一个内测码"},
    )

    assert created.json()["status"] == POST_STATUS_PENDING


def test_review_queue_requires_api_key(client: TestClient) -> None:
    assert client.get("/api/admin/posts").status_code in (401, 503)


def test_review_publishes_pending_post(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("app.api.admin.settings.admin_api_key", "test-key")
    created = client.post(
        "/api/posts",
        headers=_auth_headers(client),
        json={**_CLEAN_POST, "content": "承接代练"},
    ).json()

    queue = client.get(
        "/api/admin/posts", params={"status": POST_STATUS_PENDING}, headers=_ADMIN_HEADERS
    )
    assert [item["id"] for item in queue.json()] == [created["id"]]

    approved = client.patch(
        f"/api/admin/posts/{created['id']}/status",
        headers=_ADMIN_HEADERS,
        json={"status": POST_STATUS_PUBLISHED},
    )
    assert approved.json()["status"] == POST_STATUS_PUBLISHED
    # 审核通过后立即出现在公开列表
    assert [item["id"] for item in client.get("/api/posts").json()] == [created["id"]]


def test_review_rejects_invalid_status(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("app.api.admin.settings.admin_api_key", "test-key")
    post_id = client.post(
        "/api/posts", headers=_auth_headers(client), json=_CLEAN_POST
    ).json()["id"]

    response = client.patch(
        f"/api/admin/posts/{post_id}/status",
        headers=_ADMIN_HEADERS,
        json={"status": "deleted"},
    )
    assert response.status_code == 422


def test_review_missing_post_returns_404(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("app.api.admin.settings.admin_api_key", "test-key")
    response = client.patch(
        "/api/admin/posts/999/status",
        headers=_ADMIN_HEADERS,
        json={"status": POST_STATUS_PUBLISHED},
    )
    assert response.status_code == 404


def test_my_posts_include_pending_and_require_login(client: TestClient) -> None:
    headers = _auth_headers(client, "author-a")
    client.post(
        "/api/posts", headers=headers, json={**_CLEAN_POST, "content": "承接代练"}
    )
    client.post("/api/posts", headers=headers, json=_CLEAN_POST)

    assert client.get("/api/posts/mine").status_code == 401

    mine = client.get("/api/posts/mine", headers=headers).json()
    # 公开列表只有 1 条(已发布),但作者能看到自己的 2 条
    assert len(client.get("/api/posts").json()) == 1
    assert sorted(item["status"] for item in mine) == [
        POST_STATUS_PENDING,
        POST_STATUS_PUBLISHED,
    ]


def test_my_posts_excludes_other_users_posts(client: TestClient) -> None:
    other = _auth_headers(client, "author-b")
    client.post("/api/posts", headers=other, json=_CLEAN_POST)

    mine = client.get("/api/posts/mine", headers=_auth_headers(client, "author-c"))

    assert mine.json() == []
