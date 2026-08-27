"""职业与面试题的读取 DTO。

这里有一个真实的安全考量,不只是「规范」问题。

Question 表里存着 answer_indices(正确答案下标)和 explanation(解析)。
答题接口必须返回它们(前端本地判分),但「题干列表」接口不该返回——
否则调用方能直接拿到全部答案。

用两个不同的 DTO 表达这个区别,比在同一个模型上靠 exclude 控制要可靠得多。
"""

from pydantic import BaseModel


class JobPublic(BaseModel):
    """职业列表项:元信息 + 该职业的题目数。

    带 question_count 而不带 questions:前端首页课程卡要显示
    「N 道题」,但不需要题目内容——进入具体职业时才按需拉取。
    """

    id: str
    title: str
    emoji: str
    tagline: str
    accent: str
    question_count: int


class QuestionPublic(BaseModel):
    """一道题的完整内容,含答案与解析。

    只有答题接口返回它。注意这里刻意不包含 content_hash 和 source_url——
    前者是内部去重指纹,后者是爬取来源,都属于运营信息,前端用不到。
    """

    id: int
    qtype: str
    prompt: str
    options: list
    answer_indices: list
    explanation: str
