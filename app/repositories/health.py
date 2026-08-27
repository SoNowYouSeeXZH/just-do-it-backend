"""系统依赖健康检查的数据访问。"""

from sqlmodel import Session, text


def check_database(session: Session) -> None:
    """执行最小数据库探针。成功返回,失败让异常冒泡给 Service。"""
    session.exec(text("SELECT 1"))
