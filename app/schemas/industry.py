"""行业知识库的读取 DTO。

这些表目前没有敏感字段,那为什么还要独立 DTO?

因为「现在没有」不等于「以后没有」。直接把 SQLModel 表模型当 response_model,
意味着任何加到表里的字段都会自动出现在 API 响应里——哪天加一个
internal_priority 或 editor_note 之类的内部字段,它会静默泄露出去,
没有任何编译错误或告警提示你。

白名单 DTO 把这件事变成显式的:想让字段出现在响应里,必须主动写进来。
"""

from pydantic import BaseModel


class IndustryPublic(BaseModel):
    """行业知识条目的公开字段。"""

    id: str
    name: str
    emoji: str
    accent: str
    summary: str
    overview: str
    # JSON 列存的嵌套结构。这里用宽松类型是因为 links 的形状是
    # [{label, url}, ...],由前端约定;若要收紧可以再定义嵌套模型。
    key_points: list[str]
    links: list
