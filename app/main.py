"""
应用入口文件。

运行方式(本地不用 Docker 时):
    uvicorn app.main:app --reload
"app.main:app" 的意思是:去 app/main.py 里找名为 app 的对象来运行。

启动后打开 http://localhost:8000/docs 就能看到自动生成的交互式 API 文档,
可以直接在网页上测试接口——这是 FastAPI 最爽的地方之一。
"""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api import admin, careers, chat, industries, jobs, messages, tasks, user
from app.config import settings
from app.core.handlers import register_exception_handlers
from app.core.middleware import RequestLoggingMiddleware
from app.db import SessionDep, init_db
from app.services import health as health_service

# 打开 INFO 级别日志,方便看到"建表"、"存库失败"等运行状态
logging.basicConfig(level=getattr(logging, settings.log_level.upper(), logging.INFO))
logger = logging.getLogger(__name__)


# lifespan:FastAPI 推荐的"启动/关闭"钩子写法,替代旧的 on_event。
# 开发环境可自动建表;生产环境必须先执行 alembic upgrade head。
@asynccontextmanager
async def lifespan(_app: FastAPI):
    if settings.auto_create_tables and not settings.is_production:
        logger.info("正在初始化数据库表 ...")
        init_db()
        logger.info("数据库初始化完成")
    else:
        logger.info("生产模式不自动建表，请在启动前执行 alembic upgrade head")
    yield


# 创建 FastAPI 应用实例。title 会显示在 /docs 文档页上。
# 把 lifespan 传进去,启动时就会自动跑一次建表逻辑。
#
# 生产环境(ENVIRONMENT=production)把 docs_url / redoc_url / openapi_url 设为 None,
# 关掉 /docs、/redoc、/openapi.json。这三个地址会把全部接口结构、参数、模型
# 明细公开,等于给攻击者一份地图,线上没必要开着。开发环境保持开启方便调试。
_docs_enabled = not settings.is_production
app = FastAPI(
    title=settings.app_name,
    lifespan=lifespan,
    docs_url="/docs" if _docs_enabled else None,
    redoc_url="/redoc" if _docs_enabled else None,
    openapi_url="/openapi.json" if _docs_enabled else None,
)

# 注册 CORS 中间件,允许指定的前端地址跨域访问本后端。
# 中间件的概念类比前端 Express 的 middleware:每个请求都会先经过它。
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,  # 允许的前端来源
    allow_credentials=True,
    allow_methods=["*"],  # 允许所有 HTTP 方法(GET/POST...)
    allow_headers=["*"],  # 允许所有请求头
)

# 请求级可观测性:统一生成 request_id、记录状态码与耗时。
app.add_middleware(
    RequestLoggingMiddleware,
    slow_request_ms=settings.slow_request_ms,
)

# 注册全局异常处理器:业务异常 -> 对应状态码,未知异常 -> 500 + 日志堆栈。
# 挂在这里之后,各路由函数就不需要自己写 try/except 兜底了。
register_exception_handlers(app)

# 把各子模块的路由挂载到应用上
app.include_router(careers.router)
app.include_router(chat.router)
app.include_router(industries.router)
app.include_router(jobs.router)
app.include_router(messages.router)
app.include_router(tasks.router)
app.include_router(user.router)
app.include_router(admin.router)


# 健康检查接口:部署后用来确认"服务还活着"。
# 阿里云负载均衡、Docker 健康检查都会定期调用这类接口。
@app.get("/api/health")
def health(session: SessionDep) -> dict[str, str]:
    """健康检查同时探测数据库,用于容器/负载均衡判断依赖是否就绪。"""
    return health_service.check(session)
