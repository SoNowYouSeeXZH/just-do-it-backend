"""_is_unique_violation 的跨驱动判定单测。

背景:题库批量写入依赖「把 content_hash 唯一冲突识别为重复题跳过」,
其余 IntegrityError(外键失败、字段超长、别的唯一约束)必须照常抛出。
生产库是 PostgreSQL、测试跑在 SQLite——同一个判定函数要扛住两种驱动的
错误形态,而它们给出的信号结构完全不同。

所以这里用「合成的驱动异常」做参数化:psycopg3 的真实 diag 结构、
sqlite3 的纯文案,都不需要真的启动两种数据库。
"""

import sqlite3

import pytest
from sqlalchemy.exc import IntegrityError

from app.services.question_bank import _is_unique_violation


class _PsycopgError(Exception):
    """模拟 psycopg3 异常:诊断字段位于 orig.diag。"""

    def __init__(self, sqlstate: str, constraint: str | None, table_name: str | None = None):
        self.sqlstate = sqlstate
        self.diag = type(
            "Diagnostic",
            (),
            {"constraint_name": constraint, "table_name": table_name},
        )()
        super().__init__(f"pg error {sqlstate} constraint={constraint}")


def _wrap(orig: Exception) -> IntegrityError:
    """包成 SQLAlchemy 抛出来的样子:orig 挂在异常链上。"""
    return IntegrityError("INSERT ...", {}, orig)


@pytest.mark.parametrize(
    ("orig", "expected"),
    [
        # --- psycopg3(PostgreSQL:线上与本地) ---
        # 23505 + 唯一索引名含 content_hash → 重复题
        (_PsycopgError("23505", "ix_questions_content_hash", "questions"), True),
        (_PsycopgError("23505", "questions_content_hash_key", "questions"), True),
        (_PsycopgError("23505", "questions_content_hash_key", "archived_questions"), False),
        (_PsycopgError("23505", "content_hash", "questions"), False),
        # 唯一冲突,但撞的是别的约束 → 不是重复题,必须抛出
        (_PsycopgError("23505", "uq_task_operation_user_key", "task_operation_records"), False),
        # 外键失败(23503)/字段超长(22001)→ 真错误
        (_PsycopgError("23503", "questions_job_id_fkey", "questions"), False),
        (_PsycopgError("22001", None, "questions"), False),
        # --- sqlite3(测试环境) ---
        (sqlite3.IntegrityError("UNIQUE constraint failed: questions.content_hash"), True),
        (sqlite3.IntegrityError("UNIQUE constraint failed: archived_questions.content_hash"), False),
        (sqlite3.IntegrityError("UNIQUE constraint failed: questions.content_hash, questions.job_id"), False),
        (sqlite3.IntegrityError("UNIQUE constraint failed: users.username"), False),
        (sqlite3.IntegrityError("NOT NULL constraint failed: questions.prompt"), False),
    ],
    ids=[
        "pg-unique-index-hit",
        "pg-unique-constraint-hit",
        "pg-right-constraint-wrong-table",
        "pg-ambiguous-constraint-name",
        "pg-unique-other-constraint",
        "pg-foreign-key",
        "pg-value-too-long",
        "sqlite-unique-content-hash",
        "sqlite-right-column-wrong-table",
        "sqlite-multi-column-unique",
        "sqlite-unique-username",
        "sqlite-not-null",
    ],
)
def test_is_unique_violation_across_drivers(orig: Exception, expected: bool) -> None:
    assert _is_unique_violation(_wrap(orig), "content_hash") is expected
