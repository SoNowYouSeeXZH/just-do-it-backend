"""add guide chunks table

Revision ID: b7c1e8d4a2f0
Revises: a46af3eca670
Create Date: 2026-09-03

攻略语料分块表 + HNSW 向量索引,支撑 pgvector 预爬语料检索。

手写而不是 autogenerate:向量列来自 pgvector 的自定义类型,
alembic 自动比对能生成,但 HNSW 索引(自定义算符)它不会写,
这里一起补上,避免"迁移建了表却忘了索引,上线检索变成全表扫"。
"""

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector
import sqlmodel

# revision identifiers, used by Alembic.
revision = "b7c1e8d4a2f0"
down_revision = "a46af3eca670"
branch_labels = None
depends_on = None

# 与 app/models/guide_chunk.py 的 EMBEDDING_DIMENSIONS 保持一致。
# HNSW 索引要求列维度固定,所以这里不能引用 settings。
EMBEDDING_DIMENSIONS = 1024


def upgrade() -> None:
    op.create_table(
        "guide_chunks",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("game_slug", sqlmodel.sql.sqltypes.AutoString(length=32), nullable=False),
        sa.Column("source_url", sqlmodel.sql.sqltypes.AutoString(length=500), nullable=False),
        sa.Column("title", sqlmodel.sql.sqltypes.AutoString(length=200), nullable=False),
        sa.Column("chunk_index", sa.Integer(), nullable=False),
        sa.Column("chunk_text", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("embedding", Vector(dim=EMBEDDING_DIMENSIONS), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("source_url", "chunk_index", name="uq_guide_chunks_url_index"),
    )
    op.create_index(
        op.f("ix_guide_chunks_game_slug"), "guide_chunks", ["game_slug"], unique=False
    )
    # 检索走 HNSW 而不是 IVFFlat:语料规模在几千~几万块量级,
    # HNSW 不需要先训练(IVFFlat 要先 ANALYZE 出聚类中心)、召回率更高。
    # vector_cosine_ops = 余弦距离,与嵌入模型(智谱 embedding-3)的度量方式一致。
    op.execute(
        "CREATE INDEX ix_guide_chunks_embedding_hnsw "
        "ON guide_chunks USING hnsw (embedding vector_cosine_ops)"
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_guide_chunks_game_slug"), table_name="guide_chunks")
    op.drop_table("guide_chunks")
