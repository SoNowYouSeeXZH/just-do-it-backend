from pydantic import BaseModel, Field


class GuideSearchHit(BaseModel):
    """攻略搜索结果的公开契约。"""

    title: str
    url: str
    snippet: str


class GuideSearchQuery(BaseModel):
    """攻略搜索参数。"""

    q: str = Field(min_length=1, max_length=100)
    limit: int = Field(default=5, ge=1, le=10)


class GuideContent(BaseModel):
    """单篇攻略正文的公开契约。

    content 是从 wiki 页面抽取的纯文本(不是 markdown):
    page_fetcher 的定位是给阅读和模型上下文用的干净正文,
    前端按纯文本段落渲染即可。
    """

    url: str
    content: str
