"""未被资源专项测试覆盖的 API 冒烟用例。"""

import logging
import time

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session

from app.models.career import CareerPath, CurrentJob
from app.models.industry import Industry


def test_health_check(client: TestClient) -> None:
    """健康检查探测数据库并返回 request id。"""
    response = client.get("/api/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "database": "ok"}
    assert response.headers["x-request-id"]


def test_list_career_paths(client: TestClient, session: Session) -> None:
    session.add(
        CareerPath(
            id="path",
            title="路径",
            emoji="x",
            accent="#000",
            summary="摘要",
            steps=[],
        )
    )
    session.commit()

    response = client.get("/api/career-paths")

    assert response.status_code == 200
    assert response.json()[0]["id"] == "path"


def test_list_current_jobs(client: TestClient, session: Session) -> None:
    session.add(
        CurrentJob(id="current", title="当前职业", emoji="x", recommendations=[])
    )
    session.commit()

    response = client.get("/api/current-jobs")

    assert response.status_code == 200
    assert response.json()[0]["id"] == "current"


def test_get_industry_success(client: TestClient, session: Session) -> None:
    session.add(
        Industry(
            id="industry",
            name="行业",
            emoji="x",
            accent="#000",
            summary="摘要",
            overview="概述",
            key_points=[],
            links=[],
        )
    )
    session.commit()

    response = client.get("/api/industries/industry")

    assert response.status_code == 200
    assert response.json()["id"] == "industry"


def test_request_id_is_preserved(client: TestClient) -> None:
    """合法的上游 request id 会贯穿响应,便于跨服务追踪。"""
    response = client.get("/api/health", headers={"X-Request-ID": "gateway-123"})

    assert response.status_code == 200
    assert response.headers["x-request-id"] == "gateway-123"


def test_invalid_request_id_is_replaced(client: TestClient) -> None:
    """不安全的 request id 不进入日志/响应,中间件重新生成。"""
    response = client.get("/api/health", headers={"X-Request-ID": "bad\\nline"})

    assert response.status_code == 200
    assert response.headers["x-request-id"] != "bad\\nline"
    assert len(response.headers["x-request-id"]) == 32


def test_slow_request_is_logged_as_warning(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, caplog
) -> None:
    """超过阈值的请求打 WARNING,日志中包含 request_id 和耗时字段。"""

    def slow_check(session):
        time.sleep(0.55)
        return {"status": "ok", "database": "ok"}

    monkeypatch.setattr("app.main.health_service.check", slow_check)

    with caplog.at_level(logging.WARNING, logger="app.request"):
        response = client.get(
            "/api/health", headers={"X-Request-ID": "slow-request"}
        )

    assert response.status_code == 200
    assert "request_id=slow-request" in caplog.text
    assert "duration_ms=" in caplog.text
