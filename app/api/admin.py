"""管理接口:供受信任的外部 AI 增量写入题库。

重构后这一层不再有 try/except——`ResourceNotFoundError` 由全局处理器
转 404,未预期异常由兜底处理器转 500 并记录堆栈。

原来的写法有个隐患:
    except Exception as exc:
        raise HTTPException(status_code=500, detail="题库写入失败") from exc
这会把所有异常(包括真正的 bug)压成一句「题库写入失败」,
且没有记录堆栈——线上出问题时日志里什么都没有。
"""

from __future__ import annotations

import secrets

from fastapi import APIRouter, Depends, Header

from app.config import settings
from app.core.exceptions import ConfigurationError, InvalidTokenError
from app.db import SessionDep
from app.schemas.question_bank import BatchQuestionsRequest, BatchQuestionsResponse
from app.services.question_bank import batch_create_questions

router = APIRouter(prefix="/api/admin", tags=["admin"])


def require_admin_api_key(
    api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> None:
    """校验题库写入接口的独立 API Key。

    为什么不复用用户 JWT:这是机器对机器的调用,没有「用户」概念。
    而且出站的大模型 Key 和入站的管理 Key 要分开管理,
    一个泄露不影响另一个。

    compare_digest 而不是 != :常量时间比较,防时序攻击。
    `!=` 会在第一个不匹配的字符处提前返回,攻击者能从响应时间的
    微小差异推断出正确前缀,逐位猜出整个密钥。
    """
    if not settings.admin_api_key:
        raise ConfigurationError("题库写入接口未配置 API Key")
    if not api_key or not secrets.compare_digest(api_key, settings.admin_api_key):
        raise InvalidTokenError("API Key 无效")


@router.post(
    "/questions/batch",
    response_model=BatchQuestionsResponse,
    dependencies=[Depends(require_admin_api_key)],
)
def create_questions_batch(
    request: BatchQuestionsRequest,
    session: SessionDep,
) -> BatchQuestionsResponse:
    """批量写入已有职业的题目,重复题跳过并返回逐题结果。"""
    return batch_create_questions(session, request)
