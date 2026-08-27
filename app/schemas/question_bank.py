"""题库批量写入接口的请求/响应 DTO。

这批模型原来定义在 app/services/question_bank.py 里,和业务逻辑同文件。
搬到 schemas/ 的理由和其他模块一致:它们是**对外契约**,
描述「AI 调用方要传什么、我返回什么」,不是业务规则本身。
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class QuestionInput(BaseModel):
    """AI 提交的单道题目。

    extra="forbid" 很关键:传了未定义的字段会直接 422,而不是静默忽略。
    对机器调用方来说这是好事——字段名拼错时立刻报错,而不是数据悄悄丢了。
    也顺手挡住了「试图直接指定 id 或 content_hash」这类越权写入。
    """

    model_config = ConfigDict(extra="forbid")

    qtype: Literal["single", "multi"]
    prompt: str
    options: list[str]
    answer_indices: list[int]
    explanation: str
    source_url: str | None = None


class BatchQuestionsRequest(BaseModel):
    """批量接口请求体。单批最多 100 道,防止一次请求打满事务。"""

    model_config = ConfigDict(extra="forbid")

    job_id: str = Field(min_length=1, max_length=32)
    questions: list[QuestionInput] = Field(min_length=1, max_length=100)


class QuestionResult(BaseModel):
    """单道题的处理结果。

    逐题返回而不是整批成功/失败,是因为「其中 3 道格式不对」时,
    另外 97 道没理由跟着一起失败。调用方按 index 定位问题即可重试。
    """

    index: int
    status: Literal["created", "skipped", "failed"]
    question_id: int | None = None
    reason: str | None = None


class BatchQuestionsResponse(BaseModel):
    total: int
    created: int
    skipped: int
    failed: int
    results: list[QuestionResult]
