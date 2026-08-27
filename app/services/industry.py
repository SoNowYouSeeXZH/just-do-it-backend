"""行业知识库业务逻辑。

这一层看起来很薄——只是「查不到就抛异常」。那它有存在价值吗?

有。它把一个业务决策显式化了:**查不到算错误**。

这个判断不属于数据层(Repository 只是如实报告「没有」),也不该由
路由层做(那样每个路由都要重复写一遍 404)。放在这里,以后如果规则变成
「查不到就返回一个默认行业」,只改这一处。
"""

from sqlmodel import Session

from app.core.exceptions import ResourceNotFoundError
from app.models.industry import Industry
from app.repositories import industry as industry_repo


def list_industries(session: Session) -> list[Industry]:
    """列出全部行业。"""
    return industry_repo.list_all(session)


def get_industry(session: Session, industry_id: str) -> Industry:
    """取单个行业,不存在则抛 ResourceNotFoundError(由全局处理器转 404)。"""
    industry = industry_repo.get_by_id(session, industry_id)
    if industry is None:
        raise ResourceNotFoundError("行业不存在")
    return industry
