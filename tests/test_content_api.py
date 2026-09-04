"""只读内容接口的集成测试:行业、学习路径、职业、题目。

重点验证三件事:
1. 404 语义正确(业务层抛 ResourceNotFoundError → 全局处理器转 404)
2. DTO 白名单生效(内部字段不出现在响应里)
3. 题干接口不泄露答案
"""

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session

from app.models.career import CareerPath, CurrentJob
from app.models.industry import Industry
from app.models.job import Job, Question


@pytest.fixture(name="seeded")
def seeded_fixture(session: Session) -> None:
    """灌一份最小数据集,覆盖各表。"""
    session.add(
        Industry(
            id="web",
            name="前端 / Web 开发",
            emoji="🌐",
            accent="#1CB0F6",
            summary="做界面",
            overview="概述段落",
            key_points=["HTML", "CSS"],
            links=[{"label": "MDN", "url": "https://developer.mozilla.org"}],
        )
    )
    session.add(
        CareerPath(
            id="frontend",
            title="前端工程师",
            emoji="🎨",
            accent="#1CB0F6",
            summary="一句话定位",
            steps=[{"id": "s1", "title": "学 HTML", "desc": "基础"}],
        )
    )
    session.add(
        CurrentJob(
            id="tester",
            title="测试工程师",
            emoji="🔍",
            recommendations=[
                {"targetId": "frontend", "reason": "技能相近", "difficulty": "中"}
            ],
        )
    )
    session.add(
        Job(
            id="frontend",
            title="前端工程师",
            emoji="🎨",
            tagline="HTML / CSS / JS",
            accent="#1CB0F6",
        )
    )
    session.add(
        Question(
            job_id="frontend",
            qtype="single",
            prompt="CSS 盒模型包含哪些部分?",
            options=["A", "B", "C", "D"],
            answer_indices=[0],
            explanation="标准盒模型包含 content/padding/border/margin",
            source_url="https://example.com/q1",
            content_hash="hash-1",
        )
    )
    session.commit()


# ===== 行业 =====


def test_list_industries(client: TestClient, seeded: None) -> None:
    response = client.get("/api/industries")

    assert response.status_code == 200
    body = response.json()
    assert len(body) == 1
    assert body[0]["name"] == "前端 / Web 开发"
    assert body[0]["key_points"] == ["HTML", "CSS"]


def test_get_industry_not_found(client: TestClient, seeded: None) -> None:
    """不存在的行业:业务层抛 ResourceNotFoundError,统一转成 404。"""
    response = client.get("/api/industries/nope")

    assert response.status_code == 404
    assert response.json()["error"] == "NOT_FOUND"


# ===== 学习路径 / 当前职业 =====


def test_get_career_path(client: TestClient, seeded: None) -> None:
    response = client.get("/api/career-paths/frontend")

    assert response.status_code == 200
    assert response.json()["title"] == "前端工程师"


def test_get_career_path_not_found(client: TestClient, seeded: None) -> None:
    response = client.get("/api/career-paths/nope")

    assert response.status_code == 404
    assert response.json()["message"] == "学习路径不存在"


def test_get_current_job(client: TestClient, seeded: None) -> None:
    response = client.get("/api/current-jobs/tester")

    assert response.status_code == 200
    assert response.json()["recommendations"][0]["targetId"] == "frontend"


def test_get_current_job_not_found(client: TestClient, seeded: None) -> None:
    response = client.get("/api/current-jobs/nope")

    assert response.status_code == 404


# ===== 职业与题目 =====


def test_list_jobs_includes_question_count(client: TestClient, seeded: None) -> None:
    """列表接口带题目数,且不含题目内容。"""
    response = client.get("/api/jobs")

    assert response.status_code == 200
    body = response.json()
    assert len(body) == 1
    assert body[0]["question_count"] == 1
    assert "questions" not in body[0]


def test_get_job_not_found(client: TestClient, seeded: None) -> None:
    response = client.get("/api/jobs/nope")

    assert response.status_code == 404
    assert response.json()["message"] == "职业不存在"


def test_question_titles_hide_answers(client: TestClient, seeded: None) -> None:
    """题干接口只返回字符串列表,从类型上杜绝答案泄露。"""
    response = client.get("/api/jobs/frontend/question-titles")

    assert response.status_code == 200
    body = response.json()
    assert body == ["CSS 盒模型包含哪些部分?"]
    # 整个响应里不该出现解析文本
    assert "标准盒模型" not in response.text


def test_question_titles_unknown_job_returns_404(
    client: TestClient, seeded: None
) -> None:
    response = client.get("/api/jobs/nope/question-titles")

    assert response.status_code == 404


def test_sample_questions_returns_full_content(
    client: TestClient, seeded: None
) -> None:
    """抽题接口返回答案与解析(前端本地判分需要),但不含内部字段。

    这条用例能跑起来本身就是重构的成果:原来 repository 里硬编码
    func.rand()(MySQL 写法),SQLite 上报 "no such function: rand",
    正常路径无法测试。现在方言函数收在 repository 层,用 random()。
    """
    response = client.get("/api/jobs/frontend/questions?limit=5")

    assert response.status_code == 200
    body = response.json()
    assert len(body) == 1
    question = body[0]
    assert question["answer_indices"] == [0]
    assert question["explanation"].startswith("标准盒模型")
    # DTO 白名单:内部去重指纹和爬取来源不该下发给前端
    assert "content_hash" not in question
    assert "source_url" not in question


def test_sample_questions_unknown_job_returns_404(
    client: TestClient, seeded: None
) -> None:
    """重构时补上的校验:原来传错 job_id 会返回空数组,和「题库为空」无法区分。"""
    response = client.get("/api/jobs/nope/questions")

    assert response.status_code == 404


def test_sample_questions_rejects_limit_over_max(
    client: TestClient, seeded: None
) -> None:
    """limit 超出 1~50 范围:被 Query(ge=1, le=50) 拦下,返回 422。"""
    response = client.get("/api/jobs/frontend/questions?limit=999")

    assert response.status_code == 422
