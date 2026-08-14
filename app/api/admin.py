"""管理接口：供受信任的外部 AI 增量写入题库。"""

from __future__ import annotations

import secrets

from fastapi import APIRouter, Depends, Header, HTTPException, status

from app.config import settings
from app.db import SessionDep
from app.services.question_bank import (
    BatchQuestionsRequest,
    BatchQuestionsResponse,
    JobNotFoundError,
    batch_create_questions,
)

router = APIRouter(prefix="/api/admin", tags=["admin"])


def require_admin_api_key(
    api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> None:
    """校验题库写入接口的独立 API Key。"""

    if not settings.admin_api_key:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="题库写入接口未配置 API Key",
        )
    if not api_key or not secrets.compare_digest(api_key, settings.admin_api_key):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="API Key 无效",
        )


@router.post(
    "/questions/batch",
    response_model=BatchQuestionsResponse,
    dependencies=[Depends(require_admin_api_key)],
)
def create_questions_batch(
    request: BatchQuestionsRequest,
    session: SessionDep,
) -> BatchQuestionsResponse:
    """批量写入已有职业的题目，重复题跳过并返回逐题结果。"""

    try:
        return batch_create_questions(session, request)
    except JobNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail="题库写入失败") from exc
