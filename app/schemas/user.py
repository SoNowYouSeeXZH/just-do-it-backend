"""用户相关接口的请求/响应结构(DTO)。

为什么要独立出 schemas 层,不直接拿 ORM 模型当返回值:

1. 安全。Users 模型里有 password_hash 字段。直接返回 ORM 对象,
   一旦哪天忘了写 exclude={"password_hash"},密码哈希就泄露了。
   用独立的 UserPublic 模型是"白名单"思路——只有写进来的字段才会出去,
   默认安全,不依赖开发者每次都记得排除。

2. 解耦。数据库表结构和 API 契约是两件事。表里加一个内部字段
   (比如 last_login_ip),不该自动出现在 API 响应里。

3. 可读。看一眼 schemas 就知道接口收什么、返什么,不用去翻表定义。
"""

from datetime import datetime

from pydantic import BaseModel, Field


class UserCredentials(BaseModel):
    """登录和注册的请求体——都只需要用户名 + 密码。

    这里的 Field 约束就是"输入校验"这一层的职责:
    - min_length/max_length 和数据库列宽对齐(username VARCHAR(64)),
      不然超长字符串会一路走到 INSERT 才被数据库拒绝,报 500 而不是 422
    - 密码给个下限,挡住空密码这种明显无效的输入
    校验不通过 FastAPI 自动返回 422,业务代码里一行 if 都不用写。
    """

    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=6, max_length=128)


class UserPublic(BaseModel):
    """可以安全对外暴露的用户信息。注意:没有 password_hash。"""

    id: int
    username: str
    created_at: datetime


class LoginData(UserPublic):
    """登录成功时额外带上 token。"""

    access_token: str
    token_type: str = "bearer"


class LoginResponse(BaseModel):
    """登录接口的统一响应外壳。

    code 是业务状态码,和 HTTP 状态码分开:HTTP 层说"这次通信怎么样",
    code 说"这次业务怎么样"。保留它是为了兼容既有约定。
    失败时 data 为 null。
    """

    code: int
    message: str
    data: LoginData | None = None


class RegisterResponse(BaseModel):
    """注册接口的统一响应外壳。"""

    code: int
    message: str
    data: UserPublic | None = None
