"""pytest 公共配置(fixture)。

这里解决两个测试基础问题:

1. 不连真实 MySQL。测试用 SQLite 内存库,每个测试函数一套全新的表,
   跑完即弃。这样测试快、不污染开发数据、也不需要先启动 docker。
   注意:这是"接口 + 业务"的集成测试,不 mock 数据库读写,
   只是把数据库换成一个更轻的实现。

2. 依赖覆盖(dependency_overrides)。生产代码里路由通过
   Depends(get_session) 拿数据库会话,测试时把这个依赖替换成
   返回 SQLite 会话的版本 —— 这就是依赖注入的实际价值:
   不改一行业务代码,就能替换掉它的外部依赖。
"""

import os
from collections.abc import Iterator

# 必须在 import app 之前设置:app.config 的 settings 是模块级单例,
# 一旦 import 就会读取环境变量并固化下来。
os.environ.setdefault("JWT_SECRET_KEY", "test-secret-key-for-pytest-only")

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

import app.models  # noqa: F401 —— 触发所有表模型注册,create_all 才知道要建哪些表
from app.db import get_session
from app.main import app


@pytest.fixture(name="session")
def session_fixture() -> Iterator[Session]:
    """每个测试一套独立的 SQLite 内存库。

    两个参数是内存库的关键:
    - StaticPool:所有连接复用同一个物理连接。SQLite 内存库是"连接私有"的,
      换一个连接就是一个空库,不用 StaticPool 会出现"刚建的表查不到"。
    - check_same_thread=False:TestClient 可能在别的线程里执行请求。
    """
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        yield session


@pytest.fixture(name="client")
def client_fixture(session: Session, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """把 get_session 换成测试库会话,再返回一个可发请求的客户端。"""

    def get_session_override() -> Session:
        return session

    app.dependency_overrides[get_session] = get_session_override

    # main.py 的 lifespan 启动时会调用 init_db() 去连真实 MySQL 建表。
    # 测试里表已经由 session fixture 在 SQLite 上建好了,所以把它替换成空操作。
    # 这也暴露了当前设计的一个耦合点:应用启动流程硬编码了"连 MySQL 建表"这件事。
    # 生产上正确的做法是把建表交给迁移工具(Alembic),启动时不做 DDL。
    monkeypatch.setattr("app.main.init_db", lambda: None)

    with TestClient(app) as client:
        yield client
    # 用完清干净,避免影响其他测试模块
    app.dependency_overrides.clear()
