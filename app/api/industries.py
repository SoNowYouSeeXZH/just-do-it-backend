"""行业知识库接口。

两个接口:
- GET /api/industries               列出所有行业(知识库列表页用)
- GET /api/industries/{industry_id} 取单个行业详情

重构后这一层只剩「声明契约 + 转交」。404 不再由路由抛出——
业务层抛 ResourceNotFoundError,全局处理器统一翻译。
"""

from fastapi import APIRouter

from app.db import SessionDep
from app.schemas.industry import IndustryPublic
from app.services import industry as industry_service

router = APIRouter(prefix="/api", tags=["industries"])


@router.get("/industries", response_model=list[IndustryPublic])
def list_industries(session: SessionDep) -> list[IndustryPublic]:
    """列出全部行业。"""
    industries = industry_service.list_industries(session)
    return [
        IndustryPublic.model_validate(item, from_attributes=True)
        for item in industries
    ]


@router.get("/industries/{industry_id}", response_model=IndustryPublic)
def get_industry(industry_id: str, session: SessionDep) -> IndustryPublic:
    """取单个行业详情,不存在返回 404。"""
    industry = industry_service.get_industry(session, industry_id)
    return IndustryPublic.model_validate(industry, from_attributes=True)
