"""用户业务逻辑层(Service)。

这一层回答的问题是:"注册/登录这件事,业务上要做哪几步、什么算成功"。

它的边界(判断分层有没有守住,就看这几条):
- 不 import fastapi —— 不知道 HTTP、状态码、请求响应的存在
- 不写 SQL —— 数据读写全部委托给 repositories
- 出错时抛 core.exceptions 里的业务异常,不抛 HTTPException

好处很具体:这两个函数可以被 HTTP 接口调用,也可以被
命令行脚本、定时任务、单元测试直接调用,不需要起一个 web 服务。
"""

from sqlalchemy.exc import IntegrityError
from sqlmodel import Session

from app.core.exceptions import InvalidCredentialsError, UsernameTakenError
from app.core.security import hash_password, verify_password
from app.models.user import Users
from app.repositories import user as user_repo
from app.services.auth import create_access_token


def authenticate(session: Session, *, username: str, password: str) -> tuple[Users, str]:
    """校验用户名密码,成功则返回 (用户对象, JWT token)。

    失败统一抛 InvalidCredentialsError —— 不告诉调用方到底是
    "用户不存在"还是"密码错误",避免用户名被枚举。
    """
    user = user_repo.find_by_username(session, username)

    # 注意这里的顺序:先确认用户存在,再校验密码。
    # user.id is None 理论上不会发生(入库后必有主键),
    # 加这个判断是为了让类型检查器确信下面传给 create_access_token 的是 int。
    if user is None or user.id is None:
        raise InvalidCredentialsError("用户名或密码错误")

    if not verify_password(plain=password, hashed=user.password_hash):
        raise InvalidCredentialsError("用户名或密码错误")

    token = create_access_token(user_id=user.id, username=user.username)
    return user, token


def register(session: Session, *, username: str, password: str) -> Users:
    """创建新用户,返回入库后的用户对象。

    重名判断为什么不先查一次 SELECT 再插入?
    因为"查完到插入"之间存在时间窗口,两个请求同时注册同一个用户名时,
    两次查询都会说"不存在",然后双双插入 —— 这就是竞态条件(race condition)。
    正确做法是依赖数据库的唯一索引:让数据库来保证唯一性,
    插入失败时把异常翻译成业务含义。这是"约束应该由谁保证"的典型案例。
    """
    try:
        return user_repo.insert(
            session,
            username=username,
            # 密码先哈希再落库,数据库里永远不出现明文
            password_hash=hash_password(password),
        )
    except IntegrityError as exc:
        # 唯一约束冲突 = 用户名已被占用。
        # 事务必须回滚,否则这个 session 后续所有操作都会失败。
        session.rollback()
        raise UsernameTakenError("用户名已存在") from exc
