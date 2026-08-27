"""请求级可观测性中间件。

每个请求统一做三件事:
1. 生成/透传 request_id,让一次请求的日志可以串起来
2. 在响应头返回 X-Request-ID,用户报错时可以把它提供给开发者
3. 记录方法、路径、状态码、耗时；超过阈值额外打 WARNING

中间件属于 HTTP 基础设施层,不是业务 Service。
"""

from __future__ import annotations

import logging
import re
import time
import uuid

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

logger = logging.getLogger("app.request")
_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")


def _request_id(request: Request) -> str:
    """只接受安全字符组成的外部 request id,否则重新生成。

    request id 会进入日志,不能原样相信客户端输入,否则换行等字符可能
    伪造日志行。外部 id 合法时透传,方便网关和多个服务串联同一请求。
    """
    supplied = request.headers.get("X-Request-ID", "")
    if _REQUEST_ID_RE.fullmatch(supplied):
        return supplied
    return uuid.uuid4().hex


class RequestLoggingMiddleware(BaseHTTPMiddleware):
    """记录每个 HTTP 请求的基本运行指标。"""

    def __init__(self, app, *, slow_request_ms: int = 500):
        super().__init__(app)
        self.slow_request_ms = slow_request_ms

    async def dispatch(self, request: Request, call_next) -> Response:
        request_id = _request_id(request)
        request.state.request_id = request_id
        started = time.perf_counter()

        response = await call_next(request)

        elapsed_ms = (time.perf_counter() - started) * 1000
        response.headers["X-Request-ID"] = request_id
        log = logger.warning if elapsed_ms >= self.slow_request_ms else logger.info
        log(
            "request method=%s path=%s status=%s duration_ms=%.2f request_id=%s",
            request.method,
            request.url.path,
            response.status_code,
            elapsed_ms,
            request_id,
        )
        return response
