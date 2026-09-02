"""_is_unique_violation 的跨驱动判定单测。

背景:题库批量写入依赖「把 content_hash 唯一冲突识别为重复题跳过」,
其余 IntegrityError(外键失败、字段超长、别的唯一约束)必须照常抛出。
数据库已切到 PostgreSQL,但测试跑在 SQLite、迁移脚本还会连 MySQL——
同一个判定函数要扛住三种驱动的错误形态。

所以这里用「合成的驱动异常」做参数化:psycopg3 的结构化属性
(sqlstate/constraint)、pymysql 的错误码 args[0]、sqlite3 的纯文案,
都不需要真的装三种数据库。
"""

import sqlite3

import pytest
from sqlalchemy.exc import IntegrityError

from app.services.question_bank import _is_unique_violation


class _PsycopgError(Exception):
    """模拟 psycopg3 异常:带 sqlstate / constraint / table_name 属性。"""

    def __init__(self, sqlstate: str, constraint: str | None, table_name: str | None = None):
        self.sqlstate = sqlstate
        self.constraint = constraint
        self.table_name = table_name
        super().__init__(f"pg error {sqlstate} constraint={constraint}")


class _PyMySQLError(Exception):
    """模拟 pymysql 异常:args[0] 是 MySQL 错误码,文案里带 key 名。"""

    def __init__(self, code: int, message: str):
        super().__init__(code, message)


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
        # 唯一冲突,但撞的是别的约束 → 不是重复题,必须抛出
        (_PsycopgError("23505", "uq_task_operation_user_key", "task_operation_records"), False),
        # 外键失败(23503)/字段超长(22001)→ 真错误
        (_PsycopgError("23503", "questions_job_id_fkey", "questions"), False),
        (_PsycopgError("22001", None, "questions"), False),
        # --- pymysql(MySQL:仅迁移脚本在用) ---
        (_PyMySQLError(1062, "Duplicate entry 'abc' for key 'questions.content_hash'"), True),
        (_PyMySQLError(1062, "Duplicate entry 'x' for key 'PRIMARY'"), False),
        (_PyMySQLError(1452, "Cannot add or update a child row: a foreign key constraint fails"), False),
        # --- sqlite3(测试环境) ---
        (sqlite3.IntegrityError("UNIQUE constraint failed: questions.content_hash"), True),
        (sqlite3.IntegrityError("UNIQUE constraint failed: users.username"), False),
        (sqlite3.IntegrityError("NOT NULL constraint failed: questions.prompt"), False),
    ],
    ids=[
        "pg-unique-index-hit",
        "pg-unique-constraint-hit",
        "pg-unique-other-constraint",
        "pg-foreign-key",
        "pg-value-too-long",
        "mysql-1062-content-hash",
        "mysql-1062-primary",
        "mysql-1452-foreign-key",
        "sqlite-unique-content-hash",
        "sqlite-unique-username",
        "sqlite-not-null",
    ],
)
def test_is_unique_violation_across_drivers(orig: Exception, expected: bool) -> None:
    assert _is_unique_violation(_wrap(orig), "content_hash") is expected
