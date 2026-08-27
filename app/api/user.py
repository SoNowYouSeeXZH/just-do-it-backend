"""用户接口模块:登录 + 注册。

这一层现在只做三件事,一行业务逻辑都没有:
1. 声明 HTTP 契约(路径、方法、请求体、响应模型)
2. 把请求参数转交给 service
3. 把 service 的返回值装进响应结构

对比重构前:原来这个文件里有 SQL 查询、密码校验、token 签发、
异常兜底、事务回滚 —— 五件事挤在两个路由函数里。
现在它们各自回到了 repositories / core.security / services / 全局异常处理器。

错误处理去哪了?业务异常由 main.py 注册的全局处理器统一翻译,
所以这里看不到一个 try/except —— 这正是解耦后的效果。
"""

from fastapi import APIRouter

from app.db import SessionDep
from app.schemas.user import (
    LoginData,
    LoginResponse,
    RegisterResponse,
    UserCredentials,
    UserPublic,
)
from app.services import user as user_service

router = APIRouter(prefix="/api", tags=["user"])


@router.post("/login", response_model=LoginResponse)
def user_login(req: UserCredentials, session: SessionDep) -> LoginResponse:
    """用户登录:校验通过后签发 JWT。"""
    user, token = user_service.authenticate(
        session, username=req.username, password=req.password
    )
    return LoginResponse(
        code=200,
        message="登录成功",
        # model_validate 从 ORM 对象里按 LoginData 声明的字段挑值。
        # password_hash 没有出现在 LoginData 里,所以它根本没机会被带出去——
        # 这就是白名单 DTO 相比 exclude={"password_hash"} 更可靠的地方。
        data=LoginData(
            id=user.id,  # type: ignore[arg-type]  service 已保证登录成功时 id 非空
            username=user.username,
            created_at=user.created_at,
            access_token=token,
        ),
    )


@router.post("/registry", response_model=RegisterResponse)
def user_registry(req: UserCredentials, session: SessionDep) -> RegisterResponse:
    """用户注册:用户名重复时由全局处理器返回 409。"""
    user = user_service.register(
        session, username=req.username, password=req.password
    )
    return RegisterResponse(
        code=200,
        message="新用户注册成功",
        data=UserPublic.model_validate(user, from_attributes=True),
    )
