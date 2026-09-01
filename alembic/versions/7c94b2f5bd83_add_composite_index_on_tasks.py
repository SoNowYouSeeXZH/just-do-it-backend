"""add composite index on tasks

Revision ID: 7c94b2f5bd83
Revises: 0001_baseline
Create Date: 2026-08-31 18:24:07.606333

给 tasks 加一个复合索引,覆盖 repositories/task.py 里 list_active 的完整查询形状:
    WHERE user_id = ? AND deleted_at IS NULL [AND status = ?]
    ORDER BY created_at DESC

四个字段各自的单列索引只能让查询命中其中一个,剩下的条件仍要逐行比对。
列的顺序是关键:等值过滤字段(user_id/deleted_at/status)在前,
排序字段(created_at)放最后——索引本身就按 created_at 排好序,
配合 ORDER BY ... LIMIT 能省掉一次额外的 filesort。
"""
from typing import Sequence, Union

from alembic import op


revision: str = '7c94b2f5bd83'
down_revision: Union[str, None] = '0001_baseline'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_index(
        "ix_tasks_user_deleted_status_created",
        "tasks",
        ["user_id", "deleted_at", "status", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_tasks_user_deleted_status_created", table_name="tasks")
