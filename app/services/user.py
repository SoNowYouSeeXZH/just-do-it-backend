from passlib.context import CryptContext

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

def hash_password(password: str) -> str:
    """注册时用:把明文密码哈希成可存库的字符串(已含随机盐)。"""
    return pwd_context.hash(secret=password)

def verify_password(plain: str, hashed: str) -> bool:
    """登录时用:校验明文密码是否匹配库里的哈希。"""
    return pwd_context.verify(secret=plain, hash=hashed)