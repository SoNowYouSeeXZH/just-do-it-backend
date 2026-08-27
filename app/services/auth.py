"""JWT token 的签发与校验(纯逻辑,不依赖 HTTP)。

重构要点:这个文件原来 import 了 fastapi,并且直接抛 HTTPException。
问题在于 create_access_token 是被业务层(services/user.py)调用的,
于是"HTTP 状态码"这个概念就顺着调用链渗进了业务层。

现在它只做两件事:把用户身份编码进 token、把 token 解码回用户 id。
配置缺失时抛 ConfigurationError(业务异常),token 无效时抛 InvalidTokenError,
由 API 层的依赖(app/api/deps.py)和全局异常处理器决定怎么变成 HTTP 响应。

概念对照(前端类比):有点像浏览器的登录态 cookie,只不过
token 是无状态的——服务端不用存 session,校验签名就知道真伪。
"""

from datetime import datetime, timedelta, timezone

import jwt

from app.config import settings
from app.core.exceptions import ConfigurationError, InvalidTokenError


def _require_secret() -> str:
    """没配 JWT_SECRET_KEY 时直接拒绝服务(fail closed)。

    绝不能回退到某个默认密钥——那等于把签名密钥公开在代码里,
    任何人都能伪造 token。宁可服务报 503 让人立刻发现配置缺失。
    """
    if not settings.jwt_secret_key:
        raise ConfigurationError("服务端未配置 JWT_SECRET_KEY,鉴权功能不可用")
    return settings.jwt_secret_key


def create_access_token(*, user_id: int, username: str) -> str:
    """登录成功后调用:把用户身份编码进 token。"""
    secret = _require_secret()
    expire = datetime.now(timezone.utc) + timedelta(
        minutes=settings.access_token_expire_minutes
    )
    # sub(subject)是 JWT 标准字段,存"这个 token 代表谁";
    # exp 是标准过期时间字段,解码时 PyJWT 会自动校验,过期直接抛异常。
    payload = {"sub": str(user_id), "username": username, "exp": expire}
    return jwt.encode(payload, secret, algorithm=settings.jwt_algorithm)


def decode_access_token(token: str) -> int:
    """校验 token 并取出用户 id。

    签名不对、已过期、payload 结构异常,统一抛 InvalidTokenError——
    对外不区分具体原因,避免给伪造 token 的人提供调试反馈。
    """
    secret = _require_secret()
    try:
        payload = jwt.decode(token, secret, algorithms=[settings.jwt_algorithm])
        return int(payload["sub"])
    except (jwt.PyJWTError, KeyError, ValueError) as exc:
        raise InvalidTokenError("未登录或登录已过期") from exc
