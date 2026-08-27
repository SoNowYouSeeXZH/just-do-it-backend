"""API 层的公共依赖(Dependency)。

这个文件是"业务概念"和"HTTP 概念"的翻译层,专门放路由要用的依赖项。

为什么单独建一个文件而不是继续放在 services/auth.py:
从 HTTP 请求头里取 token、401 要带 WWW-Authenticate 响应头,
这些全是 HTTP 协议细节,只有 API 层该知道。
services/auth.py 只负责"token 字符串 <-> 用户 id"的纯逻辑。

分工:
    services/auth.py   decode_access_token(token) -> user_id   纯逻辑,可单测
    api/deps.py        从请求头取 token,失败翻译成 401 + 标准响应头
"""

from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer

from app.core.exceptions import InvalidTokenError
from app.services.auth import decode_access_token

# auto_error=False:token 缺失时不让 FastAPI 直接报错,而是给我们 None,
# 由下面的函数统一处理,保证"没带 token"和"token 无效"返回完全一样的响应。
# tokenUrl 只是给 /docs 里的 Authorize 按钮用,不影响实际校验逻辑。
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/login", auto_error=False)

# 401 必须带上这个响应头,这是 HTTP 规范对认证失败的要求,
# 客户端据此知道该用哪种认证方式重试。
_AUTH_HEADERS = {"WWW-Authenticate": "Bearer"}


def get_current_user_id(token: str | None = Depends(oauth2_scheme)) -> int:
    """受保护路由的依赖:校验 token 并返回其中的用户 id。

    用法:
        @router.get("/xxx")
        def handler(user_id: Annotated[int, Depends(get_current_user_id)]):
            ...

    这里没有直接依赖全局异常处理器,是因为 401 需要额外带
    WWW-Authenticate 响应头,而 HTTPException 才能方便地附加响应头。
    """
    if token is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="未登录或登录已过期",
            headers=_AUTH_HEADERS,
        )
    try:
        return decode_access_token(token)
    except InvalidTokenError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=exc.message,
            headers=_AUTH_HEADERS,
        ) from exc
