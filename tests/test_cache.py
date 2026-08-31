"""缓存行为测试。

这些用例不测"有没有调用 Redis",而测**行为**:
- 第二次请求是否真的没打数据库
- 数据变更后是否能读到新值
- Redis 挂掉时接口是否还能用

所以用 fakeredis(真实的 Redis 语义实现)而不是 mock。判断"有没有查库"
的手段是给 Repository 打计数器——这比断言 "redis.get 被调用了" 更贴近
我们真正关心的事情。
"""

from __future__ import annotations

import json

import pytest
import redis
from fastapi.testclient import TestClient
from sqlmodel import Session

from app.core import cache, cache_keys
from app.models.industry import Industry
from app.models.job import Job, Question


@pytest.fixture(name="seeded")
def seeded_fixture(session: Session) -> None:
    """一个职业 + 两道题 + 一个行业。"""
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
            prompt="盒模型包含哪些部分?",
            options=["A", "B", "C", "D"],
            answer_indices=[0],
            explanation="解析一",
            content_hash="hash-1",
        )
    )
    session.add(
        Industry(
            id="web",
            name="前端 / Web 开发",
            emoji="🌐",
            accent="#1CB0F6",
            summary="做界面",
            overview="概述",
            key_points=["HTML"],
            links=[],
        )
    )
    session.commit()


# ===== 命中与未命中 =====


def test_second_request_hits_cache_and_skips_db(
    client: TestClient, seeded: None, cache_client, monkeypatch: pytest.MonkeyPatch
) -> None:
    """第二次请求应该完全不查数据库。

    这是缓存最核心的断言。用计数器包住 Repository 函数,
    比检查 "redis.get 调用过" 更能说明问题——我们关心的是省掉了查询,
    而不是调用了某个库。
    """
    from app.repositories import job as job_repo

    calls = {"n": 0}
    original = job_repo.list_jobs

    def counted(*args, **kwargs):
        calls["n"] += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(job_repo, "list_jobs", counted)

    first = client.get("/api/jobs")
    second = client.get("/api/jobs")

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json() == second.json()
    assert calls["n"] == 1, "第二次请求应该命中缓存,不再查数据库"


def test_cache_key_written_with_ttl(
    client: TestClient, seeded: None, cache_client
) -> None:
    """缓存必须带 TTL。

    没有 TTL 的缓存是"永久脏数据"的来源:一旦数据变了而失效逻辑有遗漏,
    这个 key 会一直错下去。TTL 是最后一道兜底。
    """
    client.get("/api/jobs")

    assert cache_client.exists(cache_keys.JOBS_LIST)
    ttl = cache_client.ttl(cache_keys.JOBS_LIST)
    # 基础 TTL 300 秒 ±10% 抖动,所以落在 270~330 之间
    assert 260 < ttl <= 330, f"TTL 应在抖动区间内,实际 {ttl}"


def test_ttl_jitter_spreads_expiry(cache_client) -> None:
    """TTL 抖动应该让过期时间分散,而不是全部相同。

    这是防雪崩的机制:同一批 key 若 TTL 完全一致,会在同一秒集体失效,
    那一瞬间所有请求穿透到数据库。
    """
    ttls = {cache._ttl_with_jitter(300) for _ in range(30)}

    assert len(ttls) > 1, "抖动没生效,所有 TTL 都一样"
    assert all(270 <= t <= 330 for t in ttls)


# ===== 防穿透:空结果也缓存 =====


def test_missing_resource_is_cached_to_prevent_penetration(
    client: TestClient, seeded: None, cache_client, monkeypatch: pytest.MonkeyPatch
) -> None:
    """查不存在的 id 两次,数据库只应被查一次。

    不缓存空结果的话,用随机 id 刷接口就能绕过缓存直击数据库——
    这是"缓存穿透"攻击。两次都返回 404,但第二次来自缓存。
    """
    from app.repositories import job as job_repo

    calls = {"n": 0}
    original = job_repo.get_job

    def counted(*args, **kwargs):
        calls["n"] += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(job_repo, "get_job", counted)

    first = client.get("/api/jobs/does-not-exist")
    second = client.get("/api/jobs/does-not-exist")

    assert first.status_code == 404
    assert second.status_code == 404, "缓存了空值也必须仍然返回 404"
    assert calls["n"] == 1, "空结果没被缓存,存在穿透风险"


def test_null_cache_uses_shorter_ttl(
    client: TestClient, seeded: None, cache_client
) -> None:
    """空结果的 TTL 必须明显短于正常数据。

    空值不是真数据,只是挡穿透的临时挡板。如果和正常数据一样存 5 分钟,
    那么新建一个资源后,之前访问过的人要等 5 分钟才能看到它。
    """
    client.get("/api/jobs/not-yet-created")
    client.get("/api/jobs/frontend")

    null_ttl = cache_client.ttl(cache_keys.job_detail("not-yet-created"))
    real_ttl = cache_client.ttl(cache_keys.job_detail("frontend"))

    assert null_ttl < real_ttl
    assert 25 <= null_ttl <= 35, f"空值 TTL 应在 30 秒左右,实际 {null_ttl}"


# ===== 失效 =====


def test_write_invalidates_job_cache(
    client: TestClient, seeded: None, cache_client, monkeypatch: pytest.MonkeyPatch
) -> None:
    """写入题库后,职业列表的题目数必须立刻反映新值,不能等 TTL。

    这是 Cache-Aside 里最容易出错的一环:只加缓存不写失效,
    表现是"数据库改了但接口一直返回旧值",而且要等 TTL 过期才自愈,
    排查时极易误判成"写入失败"。
    """
    monkeypatch.setattr("app.config.settings.admin_api_key", "test-admin-key")

    before = client.get("/api/jobs").json()
    assert before[0]["question_count"] == 1
    assert cache_client.exists(cache_keys.JOBS_LIST)

    response = client.post(
        "/api/admin/questions/batch",
        headers={"X-API-Key": "test-admin-key"},
        json={
            "job_id": "frontend",
            "questions": [
                {
                    "qtype": "single",
                    "prompt": "新增的一道题?",
                    "options": ["A", "B"],
                    "answer_indices": [0],
                    "explanation": "解析二",
                }
            ],
        },
    )
    assert response.status_code == 200
    assert response.json()["created"] == 1

    after = client.get("/api/jobs").json()
    assert after[0]["question_count"] == 2, "写入后缓存没失效,读到的还是旧题目数"


def test_no_write_keeps_cache(
    client: TestClient, seeded: None, cache_client, monkeypatch: pytest.MonkeyPatch
) -> None:
    """全部跳过(重复题)时不该清缓存——数据没变,清了是白付一次回源成本。"""
    monkeypatch.setattr("app.config.settings.admin_api_key", "test-admin-key")

    client.get("/api/jobs")
    assert cache_client.exists(cache_keys.JOBS_LIST)

    # 提交一道校验不通过的题:created=0
    response = client.post(
        "/api/admin/questions/batch",
        headers={"X-API-Key": "test-admin-key"},
        json={
            "job_id": "frontend",
            "questions": [
                {
                    "qtype": "single",
                    "prompt": "选项太少的题",
                    "options": ["只有一个"],
                    "answer_indices": [0],
                    "explanation": "解析",
                }
            ],
        },
    )
    assert response.status_code == 200
    assert response.json()["created"] == 0
    assert cache_client.exists(cache_keys.JOBS_LIST), "没有实际写入时不该清缓存"


def test_delete_prefix_removes_all_matching_keys(cache_client) -> None:
    """按前缀失效应该清掉该前缀下全部 key。

    用前缀而不是逐个删,是因为一次写入会影响多类派生缓存
    (列表题目数、详情题目数、题干列表),逐个删容易漏。
    """
    cache_client.set("jd:jobs:list", "1")
    cache_client.set("jd:jobs:detail:frontend", "2")
    cache_client.set("jd:jobs:titles:frontend", "3")
    cache_client.set("jd:industries:list", "4")

    removed = cache.delete_prefix(cache_keys.JOBS_PREFIX)

    assert removed == 3
    assert not cache_client.exists("jd:jobs:list")
    assert cache_client.exists("jd:industries:list"), "不该误删其他业务域的缓存"


# ===== 降级(fail open)=====


def test_接口在_redis_不可用时仍然可用(
    client: TestClient, seeded: None, cache_client, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Redis 抛异常时应该退化为直接查库,而不是 500。

    缓存是加速手段,不该变成新的单点故障。很多线上事故正是
    "Redis 抖动导致整站不可用"——因为代码把缓存当成了依赖。
    """

    def boom(*args, **kwargs):
        raise redis.ConnectionError("模拟 Redis 挂掉")

    monkeypatch.setattr(cache_client, "get", boom)
    monkeypatch.setattr(cache_client, "setex", boom)

    response = client.get("/api/jobs")

    assert response.status_code == 200
    assert response.json()[0]["question_count"] == 1


def test_缓存值损坏时当作未命中(
    client: TestClient, seeded: None, cache_client
) -> None:
    """缓存里是非法 JSON 时应该回源查库,而不是抛异常。

    脏值可能来自换了序列化格式、或别的程序误写同名 key。
    当作未命中能自愈:下一次写入会覆盖掉脏值。
    """
    cache_client.set(cache_keys.JOBS_LIST, "{这不是合法 JSON")

    response = client.get("/api/jobs")

    assert response.status_code == 200
    assert response.json()[0]["title"] == "前端工程师"


def test_缓存关闭时完全不使用_redis(
    client: TestClient, seeded: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CACHE_ENABLED=false 时不该写入任何 key。

    这个开关的价值在排查线上问题时体现:怀疑"是不是缓存导致的数据不一致"
    时,可以直接关掉验证,不需要改代码重新部署。
    """
    import fakeredis

    fake = fakeredis.FakeRedis(decode_responses=True)
    cache.reset_client_for_tests()
    monkeypatch.setattr(cache, "_client", fake)
    monkeypatch.setattr("app.config.settings.cache_enabled", False)

    client.get("/api/jobs")

    assert fake.dbsize() == 0, "缓存关闭时不该写入 Redis"
    cache.reset_client_for_tests()


# ===== 序列化 =====


def test_cached_payload_is_json_not_pickle(
    client: TestClient, seeded: None, cache_client
) -> None:
    """缓存内容必须是 JSON。

    不用 pickle 有两个原因:
    1. 安全——pickle 反序列化可以执行任意代码,缓存一旦被写脏就是 RCE
    2. 可读/跨语言——JSON 能直接用 redis-cli 看,也能被其他服务消费
    """
    client.get("/api/jobs")

    raw = cache_client.get(cache_keys.JOBS_LIST)
    parsed = json.loads(raw)  # 能解析说明是 JSON

    assert isinstance(parsed, list)
    assert parsed[0]["id"] == "frontend"


def test_industry_cache_works(
    client: TestClient, seeded: None, cache_client, monkeypatch: pytest.MonkeyPatch
) -> None:
    """行业接口同样应该走缓存。"""
    from app.repositories import industry as industry_repo

    calls = {"n": 0}
    original = industry_repo.list_all

    def counted(*args, **kwargs):
        calls["n"] += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(industry_repo, "list_all", counted)

    first = client.get("/api/industries")
    second = client.get("/api/industries")

    assert first.json() == second.json()
    assert calls["n"] == 1


def test_question_titles_cached_without_leaking_answers(
    client: TestClient, seeded: None, cache_client
) -> None:
    """题干缓存里不能含答案。

    加缓存不能削弱既有的安全约束——这个接口的设计意图就是"只给题干",
    如果缓存了完整题目对象,一旦有人能读到 Redis 就等于拿到全部答案。
    """
    response = client.get("/api/jobs/frontend/question-titles")
    assert response.status_code == 200

    raw = cache_client.get(cache_keys.job_question_titles("frontend"))
    assert "解析一" not in raw, "缓存里不应包含解析"
    assert "answer_indices" not in raw, "缓存里不应包含答案下标"
