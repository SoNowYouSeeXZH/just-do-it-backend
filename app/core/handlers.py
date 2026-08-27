"""全局异常处理器。

作用:把业务层抛出的 AppError 统一翻译成 HTTP 响应。

为什么这件事必须集中做:
- 之前每个路由函数都要写一遍 try/except + 拼响应结构,
  重复且容易漏(漏了就变成 FastAPI 默认的 500,前端拿不到有意义的信息)
- 错误响应格式一旦要调整(比如加 request_id),集中在这里改一处即可
- 未预期的异常必须兜底:堆栈只写日志,绝不返回给前端。
  错误详情里可能包含 SQL、表名、文件路径,这些都是攻击者想要的信息。

给用户看的提示 和 给开发者看的日志,是两码事——这是本文件的核心。
"""

import logging

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.core.exceptions import AppError

logger = logging.getLogger(__name__)


def register_exception_handlers(app: FastAPI) -> None:
    """在应用启动时挂上处理器。main.py 调用一次即可。"""

    @app.exception_handler(AppError)
    async def handle_app_error(request: Request, exc: AppError) -> JSONResponse:
        """已知业务异常:记录 request_id,方便从用户响应定位日志。"""
        request_id = getattr(request.state, "request_id", "unknown")
        logger.info(
            "业务异常 code=%s request_id=%s: %s",
            exc.code,
            request_id,
            exc.message,
        )
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "code": exc.status_code,
                "message": exc.message,
                "error": exc.code,
                "request_id": request_id,
                "data": None,
            },
        )

    @app.exception_handler(Exception)
    async def handle_unexpected_error(request: Request, exc: Exception) -> JSONResponse:
        """未预期的异常:说明有 bug 或依赖故障,必须带堆栈写进日志。

        exc_info=True 会把完整堆栈打进日志,方便排查;
        但返回给前端的只有一句通用文案,不泄露任何内部细节。
        """
        logger.error(
            "未处理异常 %s %s: %s request_id=%s",
            request.method,
            request.url.path,
            exc,
            getattr(request.state, "request_id", "unknown"),
            exc_info=True,
        )
        return JSONResponse(
            status_code=500,
            content={
                "code": 500,
                "message": "服务异常，请稍后再试",
                "error": "INTERNAL_ERROR",
                "request_id": getattr(request.state, "request_id", "unknown"),
                "data": None,
            },
        )
