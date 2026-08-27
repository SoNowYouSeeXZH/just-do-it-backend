"""职业转型与学习路径的读取 DTO。"""

from pydantic import BaseModel


class CareerPathPublic(BaseModel):
    """一条目标职业学习路径的公开字段。"""

    id: str
    title: str
    emoji: str
    accent: str
    summary: str
    # [{id, title, desc, quizJobId?, links?}, ...]
    steps: list


class CurrentJobPublic(BaseModel):
    """一个「当前职业」选项及其转型建议。"""

    id: str
    title: str
    emoji: str
    # [{targetId, reason, difficulty}, ...]
    recommendations: list
