"""
用户接口模块:登录 + 注册。

这两个接口是"账号体系"的入口——注册把新用户写进 users 表(密码只存哈希),
登录校验用户名密码,通过后签发一个 JWT token 给前端当"登录凭证"。
"""

# IntegrityError:SQLAlchemy 在"违反数据库约束"时抛的异常。
# 这里专门用来捕获"用户名唯一约束冲突"(注册重名),好返回友好的 409 而不是 500。
from sqlalchemy.exc import IntegrityError
# SelectOfScalar:SQLModel 里 select() 语句的类型。
# 只为给下面的查询变量标注类型,让类型检查器(pyright)满意,运行时没有实际作用。
from sqlmodel.sql._expression_select_cls import SelectOfScalar

# APIRouter:把一组相关接口(这里是 user 相关)打包成一个"子路由",再挂到主 app 上。
# HTTPException:主动抛出带 HTTP 状态码的异常(如 401/503),FastAPI 会把它转成对应响应。
from fastapi import APIRouter, HTTPException
# BaseModel:pydantic 的数据模型基类。用它定义"请求体长什么样""响应体长什么样",
# FastAPI 会自动做校验和 JSON 序列化(类比 TS 的 interface + 运行时校验)。
from pydantic import BaseModel
# select:SQLModel/SQLAlchemy 的查询构造器,等价于写 SELECT ... FROM ... 的链式 API。
from sqlmodel import select

# SessionDep:数据库会话依赖(定义在 app/db.py)。
# 接口参数里声明它,FastAPI 每个请求会自动开一个 Session、结束后关闭。
from app.db import SessionDep
# Users:users 表的 ORM 模型,一个类同时是"表定义"和"数据校验模型"。
from app.models.user import Users
# create_access_token:登录成功后用它把用户身份编码进 JWT token。
from app.services.auth import create_access_token
# hash_password / verify_password:密码哈希与校验,内部用 bcrypt(带随机盐)。
from app.services.user import hash_password, verify_password

# 创建子路由:prefix="/api" 表示下面所有路径都自动带上 /api 前缀
#(即 /login 实际是 /api/login);tags 只是给自动生成的文档分组用。
router = APIRouter(prefix="/api", tags=["user"])


# 统一的响应结构:所有 user 接口都返回这三段,前端拿到后按 code 判断成败。
class UserResponse(BaseModel):
    code: int  # 业务状态码(200 成功 / 401 认证失败 / 409 重名 / 500 异常)
    message: str  # 给人看的提示文案
    # 载荷数据:登录成功时装用户信息 + token,失败时是空字典。
    # 值的类型限定为 字符串/整数/布尔/None,够覆盖当前返回的字段。
    data: dict[str, str | int | bool | None]


# 请求体结构:登录和注册都只需要用户名 + 密码两个字段。
class UserRequest(BaseModel):
    username: str
    password: str


# @router.post 把这个函数注册成 "POST /api/login" 接口。
# response_model=UserResponse 告诉 FastAPI 用这个模型来校验并生成响应文档。
@router.post("/login", response_model=UserResponse)
def user_login(req: UserRequest, session: SessionDep) -> UserResponse:
    """
    用户登录接口
    """
    try:
        # 构造查询:按用户名查这个用户。等价于 SELECT * FROM users WHERE username = :username
        user_statement: SelectOfScalar[Users] = select(Users).where(Users.username == req.username)
        # 执行查询并取第一条;查不到时 first() 返回 None。
        user: Users | None = session.exec(user_statement).first()

        # 三个条件都满足才算登录成功:
        # 1) 用户存在;2) 用户有主键 id(理论上入库后必有,这里加判断是为让类型检查器确信 id 非空);
        # 3) 明文密码和库里的哈希匹配。
        if user and user.id is not None and verify_password(plain=req.password, hashed=user.password_hash):
            # 登录成功签发 JWT,前端后续请求带上 Authorization: Bearer <token>
            token = create_access_token(user_id=user.id, username=user.username)
            # 把用户对象转成 dict 返回,但排除 password_hash——密码哈希绝不能下发给前端。
            data = user.model_dump(mode="json", exclude={"password_hash"})
            # 把 token 和类型塞进返回数据,前端存下 access_token 后续带在请求头里。
            data["access_token"] = token
            data["token_type"] = "bearer"
            return UserResponse(code=200, message='登录成功', data=data)
        else:
            # 用户不存在 或 密码不对:统一返回 401,且不透露到底是哪一项错,避免被人枚举用户名。
            return UserResponse(code=401, message='用户名或密码错误', data={})
    except HTTPException:
        # 例如服务端没配 JWT_SECRET_KEY 时抛的 503,要原样冒泡出去,
        # 否则会被下面的兜底分支伪装成 500,配置问题就查不出来了。
        raise
    except Exception:
        # 其他意料之外的错误(如数据库连不上):兜底返回 500,不把内部异常细节暴露给前端。
        return UserResponse(code=500, message='服务异常，请稍后再试', data={})


# 注册成 "POST /api/registry" 接口。
@router.post(path="/registry", response_model=UserResponse)
def user_registry(req: UserRequest, session: SessionDep) -> UserResponse:
    """
    用户注册接口
    """

    try:
        # 新建用户对象:密码先哈希再存,数据库里永远不出现明文密码。
        new_user = Users(username=req.username, password_hash=hash_password(req.password))
        # add 把对象加入会话的"待写入"队列(还没真正落库)。
        session.add(new_user)
        # commit 真正执行 INSERT 并提交事务;若用户名重复,唯一约束会在这一步抛 IntegrityError。
        session.commit()
        # refresh 从数据库回读刚插入的行,拿到自增主键 id、created_at 等由库生成的字段。
        session.refresh(new_user)
        # 返回时同样排除 password_hash。
        return UserResponse(code=200, message='新用户注册成功', data=new_user.model_dump(mode="json", exclude={"password_hash"}))
    except IntegrityError:
        # 唯一约束冲突 = 用户名已被占用。回滚事务后返回 409。
        session.rollback()
        return UserResponse(code=409, message='用户名已存在', data={})
    except Exception:
        # 其他异常:回滚 + 兜底 500,避免半截事务残留。
        session.rollback()
        return UserResponse(code=500, message='服务异常，请稍后再试', data={})
