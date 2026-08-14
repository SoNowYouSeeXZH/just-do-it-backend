"""
行业知识库接口。

两个接口:
- GET /api/industries              列出所有行业(知识库列表页用)
- GET /api/industries/{industry_id} 取单个行业详情(overview/keyPoints/links)

内容纯只读、数据量小(个位数条目),不需要像 jobs 那样做题目数聚合或随机抽样,
直接整表/按主键查询即可。
"""

from fastapi import APIRouter, HTTPException
from sqlmodel import select

from app.db import SessionDep
from app.models.industry import Industry

router = APIRouter(prefix="/api", tags=["industries"])


@router.get("/industries", response_model=list[Industry])
def list_industries(session: SessionDep) -> list[Industry]:
    """列出全部行业。按 id 排序,保证前端展示顺序稳定。"""
    return list(session.exec(select(Industry).order_by(Industry.id)).all())


@router.get("/industries/{industry_id}", response_model=Industry)
def get_industry(industry_id: str, session: SessionDep) -> Industry:
    """取单个行业详情。业务 id 是字符串主键,直接按主键取,查不到返回 404。"""
    industry = session.get(Industry, industry_id)
    if industry is None:
        raise HTTPException(status_code=404, detail="行业不存在")
    return industry
