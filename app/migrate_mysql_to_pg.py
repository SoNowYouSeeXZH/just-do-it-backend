"""MySQL → PostgreSQL 数据迁移脚本(收敛版)。

用法:
    docker compose up -d mysql          # 起临时 MySQL(挂旧数据卷)
    python -m app.migrate_mysql_to_pg   # 执行迁移并输出对账报告

设计要点(对应 .comate/specs/game-guide-rag-agent/doc.md 的 Track A/C):
- 只读源库:MySQL 引擎只做 SELECT,绝不写回;目标库非空直接拒绝执行,
  不提供任何 truncate/覆盖类操作。
- 收敛过滤:KEEP_JOBS 之外的职业(job/career_path)不迁;questions 整表不迁
  ——最初 LLM 生成的题混在种子数据里无法按字段甄别,题库由开源导入管道
  全量重建(见 app/import_question_banks.py);current_jobs 的转型建议
  过滤掉指向已删 targetId 的项。
- 原子性:所有写入在同一个事务里,任何一张表失败整体回滚,PG 不留半截数据。
- 序列重置:PG 的自增列靠 sequence 发号,显式插入带 id 的行不会推进 sequence,
  不重置的话迁移后第一次 INSERT 就会主键冲突——这是跨库迁移的经典坑。

迁移完成后:删除 .env 里的旧 MYSQL_* 段、docker-compose.override.yml 里的
临时 mysql 服务,以及 requirements-dev.txt 里的 PyMySQL。
"""

from __future__ import annotations

import dataclasses
import os
from typing import Any

from dotenv import load_dotenv
from sqlalchemy import create_engine, func, inspect, text
from sqlmodel import Session, select

from app.config import settings  # noqa: F401 —— 保留 import 触发 .env 加载,与 app.db 行为一致
from app.db import engine
from app.models.career import CareerPath, CurrentJob
from app.models.industry import Industry
from app.models.job import Job, Question
from app.models.message import ChatMessage
from app.models.task import Task
from app.models.task_operation import TaskOperationRecord
from app.models.user import Users

# 题库域收敛:只保留这两个职业(有开源题库和官方学习文档的)
KEEP_JOBS = {"frontend", "backend"}

# 整型自增主键的表:显式插入 id 后需要重置 sequence。
# jobs/career_paths/current_jobs/industries 是字符串主键,没有 sequence。
SERIAL_ID_TABLES = ("users", "tasks", "task_operation_records", "chat_messages")


@dataclasses.dataclass
class TableStat:
    """单表迁移统计,最后统一打印对账报告。"""

    source: int
    kept: int
    note: str = ""
    dropped: list[str] = dataclasses.field(default_factory=list)


def _source_url() -> str:
    """从 .env 迁移窗口期保留的 MYSQL_* 拼只读源库连接串。

    Settings 已不再声明 mysql_* 字段(生产代码不该知道旧库的存在),
    所以这里用 dotenv 自己读——脚本退场时这段连同 .env 的 MYSQL_* 一起删。
    """
    load_dotenv()
    user = os.getenv("MYSQL_USER", "root")
    password = os.getenv("MYSQL_PASSWORD", "")
    host = os.getenv("MYSQL_HOST", "127.0.0.1")
    port = os.getenv("MYSQL_PORT", "3306")
    database = os.getenv("MYSQL_DATABASE", "personal_ai")
    if not password:
        raise SystemExit("缺少 MYSQL_PASSWORD:.env 里迁移窗口期的旧 MySQL 配置不完整")
    return (
        f"mysql+pymysql://{user}:{password}@{host}:{port}/{database}"
        f"?charset=utf8mb4"
    )


def _count(session: Session, model: type) -> int:
    return session.exec(select(func.count()).select_from(model)).one()


def _check_target_empty(session: Session) -> None:
    """目标库必须是一张白纸:非空说明执行顺序错了,直接拒绝。"""
    models = (Users, Job, Industry, CareerPath, CurrentJob, Task, TaskOperationRecord, ChatMessage)
    non_empty = {
        model.__tablename__: _count(session, model)
        for model in models
        if _count(session, model) > 0
    }
    if non_empty:
        raise SystemExit(
            f"目标 PG 库非空,拒绝执行: {non_empty}\n"
            "如果确定要重来:docker compose down -v 会清掉 PG 数据卷"
            "(旧 MySQL 卷是另一个,不受影响),然后 alembic upgrade head 再重跑本脚本。"
        )


def _check_career_invariant(paths: list[CareerPath]) -> None:
    """不变量断言:保留的学习路径不许引用将被删除的职业。

    正常数据不会违反(seed 里 frontend/backend 路径只引用自己的 quizJobId),
    万一源库被手改过,这里拦下来比迁出一个点击 404 的悬空引用好。
    """
    violations: list[tuple[str, str]] = []
    for path in paths:
        for step in path.steps or []:
            if isinstance(step, dict):
                quiz_job_id = step.get("quizJobId")
                if quiz_job_id and quiz_job_id not in KEEP_JOBS:
                    violations.append((path.id, str(quiz_job_id)))
    if violations:
        raise SystemExit(f"保留的学习路径引用了将被删除的职业: {violations}\n先修源库数据再迁移。")


def _fresh(row: Any) -> Any:
    """把源库读出的 detached 对象复制成全新实例。

    直接 add 源对象会踩 StaleDataError:它们带着源 session 的持久化身份
    (有主键、状态是 persistent),目标 session 会当成「已存在的行」发 UPDATE
    而不是 INSERT——目标表是空的,UPDATE 匹配 0 行直接报错。
    复制成新实例后是干净的 pending 状态,正确走 INSERT。
    """
    return type(row).model_validate(row.model_dump())


def _load_source() -> tuple[dict[str, list[Any]], dict[str, TableStat]]:
    """从源库读出全部待迁数据(只读),并完成收敛过滤。

    刻意让 with 块在这里就结束:Session 关闭后对象变 detached,
    才能安全地加进目标 Session(否则两个 session 争同一个对象会报
    InvalidRequestError)。
    """
    source_engine = create_engine(_source_url(), pool_pre_ping=True)
    stats: dict[str, TableStat] = {}

    try:
        with Session(source_engine) as src:
            users = src.exec(select(Users)).all()
            all_jobs = src.exec(select(Job)).all()
            industries = src.exec(select(Industry)).all()
            all_paths = src.exec(select(CareerPath)).all()
            current_jobs = src.exec(select(CurrentJob)).all()
            tasks = src.exec(select(Task)).all()
            task_ops = src.exec(select(TaskOperationRecord)).all()
            # chat_messages 存在历史漂移:源表还是鉴权改造前的结构(没有 user_id 列)。
            # create_all 只建新表、从不 ALTER 已有表,所以模型后来加的列没落到库上
            # (docs/deployment-and-migrations.md 里记录过的已知问题)。
            # 这批"无主"的匿名聊天在新模型(user_id NOT NULL + 外键)下无处安放,
            # 硬塞一个假归属只会污染某个用户的历史——跳过,并在对账报告里如实交代。
            chat_columns = {c["name"] for c in inspect(source_engine).get_columns("chat_messages")}
            if "user_id" in chat_columns:
                messages = src.exec(select(ChatMessage)).all()
                chat_source_count = len(messages)
                chat_note = ""
            else:
                messages = []
                chat_source_count = src.connection().execute(
                    text("SELECT COUNT(*) FROM chat_messages")
                ).scalar_one()
                chat_note = (
                    f"跳过 {chat_source_count} 行匿名历史数据"
                    "(源表缺 user_id 列,属鉴权改造前的旧结构,新模型要求消息归属用户)"
                )
            question_source_count = _count(src, Question)
    except Exception as exc:  # noqa: BLE001 —— 给出可操作的提示比裸栈友好
        raise SystemExit(
            f"读源 MySQL 失败({exc.__class__.__name__}: {exc})\n"
            "排查顺序:1) docker compose up -d mysql 起了临时源库吗;"
            "2) .env 里 MYSQL_* 是旧库的真实配置吗。"
        ) from exc

    # ---- 收敛过滤(在 detached 对象上做,只改内存) ----
    jobs = [j for j in all_jobs if j.id in KEEP_JOBS]
    paths = [p for p in all_paths if p.id in KEEP_JOBS]
    _check_career_invariant(paths)

    # current_jobs 整表保留,但转型建议里指向已删 targetId 的项要剪掉
    pruned_recs = 0
    for current_job in current_jobs:
        recs = current_job.recommendations or []
        kept = [r for r in recs if isinstance(r, dict) and r.get("targetId") in KEEP_JOBS]
        pruned_recs += len(recs) - len(kept)
        current_job.recommendations = kept

    rows: dict[str, list[Any]] = {
        "users": [_fresh(r) for r in users],
        "jobs": [_fresh(r) for r in jobs],
        "industries": [_fresh(r) for r in industries],
        "career_paths": [_fresh(r) for r in paths],
        "current_jobs": [_fresh(r) for r in current_jobs],
        "tasks": [_fresh(r) for r in tasks],
        "task_operation_records": [_fresh(r) for r in task_ops],
        "chat_messages": [_fresh(r) for r in messages],
    }
    stats["users"] = TableStat(len(users), len(users))
    stats["jobs"] = TableStat(
        len(all_jobs), len(jobs),
        dropped=[j.id for j in all_jobs if j.id not in KEEP_JOBS],
    )
    stats["industries"] = TableStat(len(industries), len(industries))
    stats["career_paths"] = TableStat(
        len(all_paths), len(paths),
        dropped=[p.id for p in all_paths if p.id not in KEEP_JOBS],
    )
    stats["current_jobs"] = TableStat(
        len(current_jobs), len(current_jobs),
        note=f"转型建议剪掉 {pruned_recs} 条指向已删职业的项",
    )
    stats["tasks"] = TableStat(len(tasks), len(tasks))
    stats["task_operation_records"] = TableStat(len(task_ops), len(task_ops))
    stats["chat_messages"] = TableStat(chat_source_count, len(messages), note=chat_note)
    stats["questions"] = TableStat(
        question_source_count, 0,
        note="整表不迁移,由开源题库导入管道重建(app/import_question_banks.py)",
    )
    return rows, stats


def _reset_sequences(session: Session) -> None:
    """把自增序列拨到 MAX(id)+1。

    PG 的 serial 列靠 sequence 发号,但「显式插入带 id 的行」不会推进它——
    迁移后第一次 INSERT 会拿着 sequence 里过期的号撞主键。
    setval(..., GREATEST(MAX(id),0)+1, false) 让下一次 nextval 恰好返回
    MAX(id)+1;空表则返回 1。
    """
    for table in SERIAL_ID_TABLES:
        session.exec(
            text(
                f"SELECT setval(pg_get_serial_sequence('{table}', 'id'), "
                f"GREATEST((SELECT COALESCE(MAX(id), 0) FROM {table}), 0) + 1, false)"
            )
        )


def _print_report(stats: dict[str, TableStat], target_counts: dict[str, int]) -> None:
    print("\n=== MySQL → PG 迁移对账报告 ===")
    for name, stat in stats.items():
        dropped = ""
        if stat.dropped:
            shown = ", ".join(stat.dropped[:12])
            more = f" 等 {len(stat.dropped)} 个" if len(stat.dropped) > 12 else ""
            dropped = f"(丢弃: {shown}{more})"
        note = f"  —— {stat.note}" if stat.note else ""
        actual = target_counts.get(name, 0)
        print(
            f"{name:<24} 源 {stat.source:>4} → 迁入 {stat.kept:>4} / 实际入库 {actual:>4}"
            f"{dropped}{note}"
        )
        if actual != stat.kept:
            print(f"{'':<24} ⚠️ 期望迁入 {stat.kept},实际 {actual},请检查!")
    print(f"序列重置: {', '.join(SERIAL_ID_TABLES)} → nextval 已对齐 MAX(id)+1")
    print("迁移完成。后续:python -m app.import_question_banks 重建题库。")


def main() -> None:
    rows, stats = _load_source()

    with Session(engine) as dst:
        _check_target_empty(dst)

        # 单事务写入:任何一张表失败,整体回滚,PG 不留半截数据
        for table_rows in rows.values():
            for row in table_rows:
                dst.add(row)
        dst.commit()

        # 数据落库后再拨序列;这一步失败只影响自增发号,数据本身已经安全
        _reset_sequences(dst)
        dst.commit()

        # 对账:目标库实际行数必须等于期望迁入数(questions 期望为 0)
        target_counts: dict[str, int] = {}
        for name, table_rows in rows.items():
            target_counts[name] = _count(dst, type(table_rows[0])) if table_rows else 0
        target_counts["questions"] = _count(dst, Question)

    _print_report(stats, target_counts)


if __name__ == "__main__":
    main()
