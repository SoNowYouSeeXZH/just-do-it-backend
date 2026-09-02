# MySQL 时代的 Alembic 迁移归档

这里的两个 revision 是 **MySQL 谱系的历史**，已从 `alembic/versions/` 移出，
不再参与 Alembic 的版本链：

- `0001_baseline.py` —— 空基线，只做 stamp（MySQL 时代的已知 TODO：完整建表迁移缺失，表结构长期靠 `SQLModel.metadata.create_all` 生成）
- `7c94b2f5bd83_add_composite_index_on_tasks.py` —— 唯一一条真实 DDL（tasks 复合索引）

2026-09 切换 PostgreSQL 时，PG 谱系以 `cf50d924c9f7` 重新开基线（完整建表 +
`CREATE EXTENSION vector`），见 `alembic/versions/`。

保留这两个文件仅作历史记录：线上 MySQL 库的 `alembic_version` 表里记录的
就是 `7c94b2f5bd83`，如果将来需要回查 MySQL 谱系的版本状态，以这里为准。

注意：如果放回 `versions/` 目录，会因出现两个 `down_revision=None` 的根
形成 multiple heads，`alembic upgrade head` 直接报错——不要放回去。
