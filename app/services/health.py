"""系统健康检查业务逻辑。"""

from sqlmodel import Session

from app.repositories import health as health_repo


def check(session: Session) -> dict[str, str]:
    """检查关键依赖是否可用。"""
    health_repo.check_database(session)
    return {"status": "ok", "database": "ok"}
