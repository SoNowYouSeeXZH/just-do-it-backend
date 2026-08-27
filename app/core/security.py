"""密码哈希工具。

放在 core 而不是 services,是因为它是"通用安全能力",不含任何业务规则——
和 JWT 签发一样属于底层设施,任何业务都可能用到。

之前它在 app/services/user.py 里,和用户业务逻辑混在一个文件。
拆开后 services/user.py 只剩业务规则,读起来更干净。
"""

from passlib.context import CryptContext

# CryptContext 是 passlib 的算法调度器。
# schemes 列表里第一个是"新密码用哪个算法哈希";
# deprecated="auto" 的意思是:列表里其余算法只用于校验老密码,
# 将来换算法时(比如上 argon2),老用户下次登录能被自动识别出"该升级了"。
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


def hash_password(password: str) -> str:
    """注册时用:把明文密码哈希成可存库的字符串(已含随机盐)。"""
    return pwd_context.hash(secret=password)


def verify_password(plain: str, hashed: str) -> bool:
    """登录时用:校验明文密码是否匹配库里的哈希。"""
    return pwd_context.verify(secret=plain, hash=hashed)
