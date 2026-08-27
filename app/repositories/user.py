"""users 表的数据访问封装。

原来这些查询语句是直接写在 app/api/user.py 的路由函数里的,
路由因此"知道了"表结构和 SQL 语法。搬到这里之后:

- 想加"只查未禁用的用户"这类过滤条件,只改本文件
- 想换 ORM 或加缓存,上层业务代码完全不用动
- 业务层读起来就是 find_by_username(...),意图一目了然
"""

from sqlmodel import Session, select
from sqlmodel.sql._expression_select_cls import SelectOfScalar

from app.models.user import Users


def find_by_username(session: Session, username: str) -> Users | None:
    """按用户名查用户,查不到返回 None。

    等价 SQL: SELECT * FROM users WHERE username = :username LIMIT 1
    username 上有唯一索引(见 models/user.py),所以这个查询走索引,很快。
    """
    statement: SelectOfScalar[Users] = select(Users).where(Users.username == username)
    return session.exec(statement).first()


def insert(session: Session, *, username: str, password_hash: str) -> Users:
    """插入一个新用户并返回带主键的对象。

    注意这里做了 commit —— 因为"注册"就是一个完整的业务操作,
    没有别的写入需要和它凑成同一个事务。
    将来如果出现"注册的同时还要写一条初始化记录"这种需求,
    就要把 commit 上提到业务层统一控制(这是后面学事务边界时的重点)。

    用户名重复时,数据库唯一约束会在 commit 这一步抛 IntegrityError,
    本层不捕获——由业务层决定"重名"该怎么解释。
    """
    user = Users(username=username, password_hash=password_hash)
    session.add(user)
    session.commit()
    # refresh 从库里回读刚插入的行,拿到自增 id、created_at 等由数据库生成的字段
    session.refresh(user)
    return user
