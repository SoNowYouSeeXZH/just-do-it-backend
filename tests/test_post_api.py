"""社区帖子接口的集成测试。

浏览类接口对游客开放,写操作必须登录;未发布的帖子不进入公开列表。
"""

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.models.post import POST_STATUS_PENDING, Post


def _auth_headers(client: TestClient, username: str = "post-user") -> dict[str, str]:
    assert (
        client.post(
            "/api/registry", json={"username": username, "password": "secret123"}
        ).status_code
        == 200
    )
    login = client.post(
        "/api/login", json={"username": username, "password": "secret123"}
    )
    assert login.status_code == 200
    return {"Authorization": f"Bearer {login.json()['data']['access_token']}"}


_POST = {
    "game_slug": "ys",
    "title": "纳塔探索路线",
    "content": "先做主线再解锁传送点。",
    "city": "上海",
}


def test_create_post_requires_login(client: TestClient) -> None:
    assert client.post("/api/posts", json=_POST).status_code == 401


def test_guest_can_browse_published_posts(client: TestClient) -> None:
    created = client.post("/api/posts", headers=_auth_headers(client), json=_POST)
    assert created.status_code == 200
    assert created.json()["status"] == "published"

    listed = client.get("/api/posts")
    assert listed.status_code == 200
    assert [item["title"] for item in listed.json()] == ["纳塔探索路线"]


def test_post_list_filters_by_game_and_city(client: TestClient) -> None:
    headers = _auth_headers(client)
    client.post("/api/posts", headers=headers, json=_POST)
    client.post(
        "/api/posts",
        headers=headers,
        json={**_POST, "game_slug": "zzz", "title": "绝区零配队", "city": "北京"},
    )

    assert len(client.get("/api/posts", params={"game_slug": "ys"}).json()) == 1
    assert len(client.get("/api/posts", params={"city": "北京"}).json()) == 1
    assert len(client.get("/api/posts", params={"game_slug": "sr"}).json()) == 0


def test_pending_post_is_hidden_from_guests(
    client: TestClient, session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("app.services.post.settings.community_auto_publish", False)
    created = client.post("/api/posts", headers=_auth_headers(client), json=_POST)

    assert created.json()["status"] == POST_STATUS_PENDING
    assert client.get("/api/posts").json() == []
    assert client.get(f"/api/posts/{created.json()['id']}").status_code == 404
    assert session.exec(select(Post)).one().status == POST_STATUS_PENDING


def test_comment_requires_login_and_updates_count(client: TestClient) -> None:
    headers = _auth_headers(client)
    post_id = client.post("/api/posts", headers=headers, json=_POST).json()["id"]

    assert (
        client.post(f"/api/posts/{post_id}/comments", json={"content": "有用"}).status_code
        == 401
    )
    created = client.post(
        f"/api/posts/{post_id}/comments", headers=headers, json={"content": "有用"}
    )
    assert created.status_code == 200

    assert [item["content"] for item in client.get(f"/api/posts/{post_id}/comments").json()] == [
        "有用"
    ]
    assert client.get(f"/api/posts/{post_id}").json()["comment_count"] == 1


def test_like_toggles_and_is_idempotent_per_user(client: TestClient) -> None:
    headers = _auth_headers(client)
    post_id = client.post("/api/posts", headers=headers, json=_POST).json()["id"]

    assert client.post(f"/api/posts/{post_id}/like").status_code == 401

    liked = client.post(f"/api/posts/{post_id}/like", headers=headers).json()
    assert liked == {"post_id": post_id, "liked": True, "like_count": 1}

    unliked = client.post(f"/api/posts/{post_id}/like", headers=headers).json()
    assert unliked == {"post_id": post_id, "liked": False, "like_count": 0}


def test_comment_on_missing_post_returns_404(client: TestClient) -> None:
    response = client.post(
        "/api/posts/999/comments", headers=_auth_headers(client), json={"content": "x"}
    )
    assert response.status_code == 404


def test_post_rejects_invalid_payload(client: TestClient) -> None:
    response = client.post(
        "/api/posts", headers=_auth_headers(client), json={**_POST, "title": ""}
    )
    assert response.status_code == 422


def test_post_and_comment_expose_author_name_not_user_id(client: TestClient) -> None:
    headers = _auth_headers(client, "kaia")
    created = client.post("/api/posts", headers=headers, json=_POST).json()

    assert created["author_name"] == "kaia"
    # user_id 属于内部主键,不应出现在公开响应里
    assert "user_id" not in created

    client.post(
        f"/api/posts/{created['id']}/comments", headers=headers, json={"content": "有用"}
    )
    listed = client.get("/api/posts").json()
    comments = client.get(f"/api/posts/{created['id']}/comments").json()

    assert listed[0]["author_name"] == "kaia"
    assert comments[0]["author_name"] == "kaia"
    assert "user_id" not in comments[0]


def test_post_list_paginates_by_offset(client: TestClient) -> None:
    headers = _auth_headers(client)
    for index in range(3):
        client.post("/api/posts", headers=headers, json={**_POST, "title": f"帖子{index}"})

    first_page = client.get("/api/posts", params={"limit": 2}).json()
    second_page = client.get("/api/posts", params={"limit": 2, "offset": 2}).json()

    assert len(first_page) == 2
    assert len(second_page) == 1
    # 两页不重叠:分页有第二排序键兜底,不会漏帖或重复
    ids = [item["id"] for item in first_page + second_page]
    assert len(set(ids)) == 3


def test_comment_list_paginates_by_offset(client: TestClient) -> None:
    headers = _auth_headers(client)
    post_id = client.post("/api/posts", headers=headers, json=_POST).json()["id"]
    for index in range(3):
        client.post(
            f"/api/posts/{post_id}/comments",
            headers=headers,
            json={"content": f"评论{index}"},
        )

    first_page = client.get(
        f"/api/posts/{post_id}/comments", params={"limit": 2}
    ).json()
    second_page = client.get(
        f"/api/posts/{post_id}/comments", params={"limit": 2, "offset": 2}
    ).json()

    assert len(first_page) == 2
    assert len(second_page) == 1
    assert len({item["id"] for item in first_page + second_page}) == 3


def test_pagination_rejects_negative_offset(client: TestClient) -> None:
    assert client.get("/api/posts", params={"offset": -1}).status_code == 422
