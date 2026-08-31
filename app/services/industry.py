"""行业知识库业务逻辑。

这一层看起来很薄——只是「查不到就抛异常」。那它有存在价值吗?

有。它把一个业务决策显式化了:**查不到算错误**。

这个判断不属于数据层(Repository 只是如实报告「没有」),也不该由
路由层做(那样每个路由都要重复写一遍 404)。放在这里,以后如果规则变成
「查不到就返回一个默认行业」,只改这一处。

行业数据是典型的"运营维护、几乎不变、所有用户看到的都一样",
缓存收益高、失效风险低,所以两个查询都加了缓存。

返回类型从 ORM 模型改成了 DTO(IndustryPublic):缓存里存的是 JSON,
读回来就不再是 ORM 对象了。与其让上层判断"这次拿到的是模型还是 dict",
不如在这一层统一转成 DTO——顺带把「哪些字段对外可见」的决策也收进业务层,
路由层不用再自己 model_validate。
"""

from sqlmodel import Session

from app.core import cache, cache_keys
from app.core.exceptions import ResourceNotFoundError
from app.repositories import industry as industry_repo
from app.schemas.industry import IndustryPublic


def list_industries(session: Session) -> list[IndustryPublic]:
    """列出全部行业。"""

    def load() -> list[dict]:
        return [
            IndustryPublic.model_validate(item, from_attributes=True).model_dump()
            for item in industry_repo.list_all(session)
        ]

    rows = cache.cache_aside(cache_keys.INDUSTRIES_LIST, load)
    return [IndustryPublic.model_validate(row) for row in rows]


def get_industry(session: Session, industry_id: str) -> IndustryPublic:
    """取单个行业,不存在则抛 ResourceNotFoundError(由全局处理器转 404)。

    和 job 一样,"查不到"这个结果本身也会被缓存(短 TTL),
    防止用随机 id 刷接口绕过缓存直击数据库。
    """

    def load() -> dict | None:
        industry = industry_repo.get_by_id(session, industry_id)
        if industry is None:
            return None
        return IndustryPublic.model_validate(industry, from_attributes=True).model_dump()

    row = cache.cache_aside(cache_keys.industry_detail(industry_id), load)
    if row is None:
        raise ResourceNotFoundError("行业不存在")
    return IndustryPublic.model_validate(row)
