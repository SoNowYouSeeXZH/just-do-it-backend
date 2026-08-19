"""
JWT 鉴权工具。

作用:登录成功后签发一个 token,前端把它放进请求头
Authorization: Bearer <token>,后端用它确认"这是谁在请求",
不用每次请求都重新验证用户名密码。

概念对照(前端类比):有点像浏览器的登录态 cookie,只不过
token 是无状态的——服务端不用存 session,校验签名就知道真伪。
"""

from datetime import datetime, timedelta, timezone

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer

from app.config import settings

# tokenUrl 只是给 /docs 里的"Authorize"按钮用,指向登录接口,不影响实际校验逻辑。
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/login", auto_error=False)


def _require_secret() -> str:
    """没配 JWT_SECRET_KEY 时直接拒绝服务(fail closed)。

    绝不能回退到某个默认密钥——那等于把签名密钥公开在代码里,
    任何人都能伪造 token。宁可服务报 503 让人立刻发现配置缺失。
    """
    if not settings.jwt_secret_key:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="服务端未配置 JWT_SECRET_KEY,鉴权功能不可用",
        )
    return settings.jwt_secret_key


def create_access_token(*, user_id: int, username: str) -> str:
    """登录成功后调用:把用户身份编码进 token。"""
    secret = _require_secret()
    expire = datetime.now(timezone.utc) + timedelta(
        minutes=settings.access_token_expire_minutes
    )
    payload = {"sub": str(user_id), "username": username, "exp": expire}
    return jwt.encode(payload, secret, algorithm=settings.jwt_algorithm)


def get_current_user_id(token: str | None = Depends(oauth2_scheme)) -> int:
    """受保护路由的依赖:校验 token 并返回其中的用户 id。

    token 缺失、签名不对、或者已过期,统一返回 401——
    前端看到 401 就知道该跳回登录页重新拿 token。
    """
    secret = _require_secret()
    if token is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="未登录或登录已过期",
            headers={"WWW-Authenticate": "Bearer"},
        )
    try:
        payload = jwt.decode(token, secret, algorithms=[settings.jwt_algorithm])
        return int(payload["sub"])
    except (jwt.PyJWTError, KeyError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="未登录或登录已过期",
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc
