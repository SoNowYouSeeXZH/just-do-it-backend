"""add location fields to posts (lat/lng/district)

「附近广场」转型:帖子加经纬度 + 模糊行政区名,支持按距离过滤。
lat/lng 加索引用于附近查询的经纬度包围盒粗筛;district 只用于展示不参与过滤,
不加索引。三列都可空——用户未授权定位时发的帖子没有位置。

Revision ID: c9d2f1a4b8e3
Revises: b7c1e8d4a2f0
Create Date: 2026-09-20 09:05:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import sqlmodel


revision: str = 'c9d2f1a4b8e3'
down_revision: Union[str, None] = 'b7c1e8d4a2f0'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('posts', sa.Column('lat', sa.Float(), nullable=True))
    op.add_column('posts', sa.Column('lng', sa.Float(), nullable=True))
    op.add_column(
        'posts',
        sa.Column('district', sqlmodel.sql.sqltypes.AutoString(length=32), nullable=True),
    )
    op.create_index(op.f('ix_posts_lat'), 'posts', ['lat'], unique=False)
    op.create_index(op.f('ix_posts_lng'), 'posts', ['lng'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_posts_lng'), table_name='posts')
    op.drop_index(op.f('ix_posts_lat'), table_name='posts')
    op.drop_column('posts', 'district')
    op.drop_column('posts', 'lng')
    op.drop_column('posts', 'lat')
