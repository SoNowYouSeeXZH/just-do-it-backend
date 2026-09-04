"""管理接口:题库增量写入 + 社区内容审核。

两类调用方都不是"某个登录用户",而是受信任的机器/运营端,
所以统一走 X-API-Key 而不是用户 JWT(理由见 require_admin_api_key)。

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
from typing import Annotated

from fastapi import APIRouter, Depends, Header, Query

from app.config import settings
from app.core.exceptions import ConfigurationError, InvalidTokenError
from app.db import SessionDep
from app.schemas.post import PostPublic, PostReviewRequest
from app.schemas.question_bank import BatchQuestionsRequest, BatchQuestionsResponse
from app.services import post as post_service
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


@router.get(
    "/posts",
    response_model=list[PostPublic],
    dependencies=[Depends(require_admin_api_key)],
)
def list_posts_for_review(
    session: SessionDep,
    status: Annotated[str | None, Query(max_length=16)] = None,
    limit: Annotated[int, Query(ge=1, le=50)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[PostPublic]:
    """审核队列:默认列出全部状态,传 status=pending 只看待审。"""
    return post_service.list_posts_for_review(
        session, status=status, limit=limit, offset=offset
    )


@router.patch(
    "/posts/{post_id}/status",
    response_model=PostPublic,
    dependencies=[Depends(require_admin_api_key)],
)
def review_post(
    post_id: int,
    request: PostReviewRequest,
    session: SessionDep,
) -> PostPublic:
    """审核通过或驳回。PATCH 而不是 PUT:只改一个字段,不是整体替换。"""
    return post_service.review_post(session, post_id=post_id, status=request.status)
