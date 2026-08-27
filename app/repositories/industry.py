"""industries 表的数据访问封装。

原来这两个查询直接写在 app/api/industries.py 的路由里。
搬过来的收益不在于「这两行 SQL 变好看了」,而在于:
路由层从此不知道数据是从哪张表、用什么语句取的。
"""

from sqlmodel import Session, select

from app.models.industry import Industry


def list_all(session: Session) -> list[Industry]:
    """按 id 升序取全部行业。

    排序不是可选项:不指定 ORDER BY 时,数据库返回顺序取决于内部存储,
    换个版本、加个索引都可能变。前端列表顺序跟着抖动,却查不出原因。
    """
    return list(session.exec(select(Industry).order_by(Industry.id)).all())


def get_by_id(session: Session, industry_id: str) -> Industry | None:
    """按主键取单个行业,不存在返回 None。

    注意这里返回 None 而不是抛异常——「查不到」对数据层来说是
    一个正常结果,「查不到算不算错误」由业务层决定。
    """
    return session.get(Industry, industry_id)
