"""
数据库连接与会话管理。

这里做三件事:
1. 创建 SQLAlchemy Engine(连接池,全局只需要一个)
2. 提供一个 get_session 依赖,让 FastAPI 接口按需取一个 Session 用
3. 提供 init_db,应用启动时调用,自动建表(学习期用,生产期换 Alembic)

概念对照(前端类比):
- Engine ≈ axios 实例(全局配置好的"连接客户端",内部维护连接池)
- Session ≈ 一次请求上下文里的"数据库操作句柄",增删改查都通过它
- Model  ≈ TS interface,只不过它同时告诉 ORM "怎么映射到表"
"""
from typing import Annotated

from collections.abc import Iterator

from sqlmodel import Session, SQLModel, create_engine

from fastapi import Depends

from app.config import settings

# create_engine:创建连接池。
# - echo=False:调试时可以打开,会把每条 SQL 打到日志里,非常适合学习
# - pool_pre_ping=True:每次从池里拿连接前先 ping 一下,
#   MySQL 默认 8 小时不活动就断连,加这个能避免"第一条 SQL 报断连"
# - pool_recycle=3600:连接用满 1 小时就回收重建,双保险
engine = create_engine(
    settings.database_url,
    echo=False,
    pool_pre_ping=True,
    pool_recycle=3600,
)


def init_db() -> None:
    """启动时建表。

    SQLModel.metadata.create_all 会扫描所有已导入的 SQLModel 子类,
    对不存在的表执行 CREATE TABLE。已存在的表不会被改动(所以改字段要走迁移工具)。

    重要:表定义必须先被 import 过,SQLModel 才知道有哪些表要建。
    这里只 import 一次 app.models 包,包的 __init__ 会自动把包内所有模型
    模块都导入进来(见 app/models/__init__.py),新增表时不用再改这里。
    放在函数体里 import,是为了避免循环导入。
    """
    import app.models  # noqa: F401 —— 触发包内所有模型注册,不直接使用

    SQLModel.metadata.create_all(engine)


def get_session() -> Iterator[Session]:
    """FastAPI 依赖:每个请求分配一个 Session,请求结束自动关闭。

    用法:
        @router.get("/xxx")
        def handler(session: SessionDep):
            ...

    yield 之前的代码在请求进入时执行,yield 之后的代码在响应发完后执行,
    整体是"上下文管理器"的写法,类比前端中间件的 next 前后。
    """
    with Session(engine) as session:
        yield session


# 依赖别名:Annotated 的第一个参数是真实类型(给类型检查器看),
# 后面的 Depends 是元数据(给 FastAPI 看)。
# 比 `session: Session = Depends(get_session)` 好在:
# 1. 不占用参数默认值位置,不会触发 linter 的 B008 告警
# 2. 也不会因为"无默认值参数排在有默认值参数后面"而报错
# 3. 依赖声明只写一次,各路由模块直接复用
SessionDep = Annotated[Session, Depends(get_session)]