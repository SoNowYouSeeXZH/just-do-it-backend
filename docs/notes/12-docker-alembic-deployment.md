# 12 Docker 部署与 Alembic 迁移

> 对应迭代 7 · 数据库备份、Alembic 接入与后端部署部分已完成

## 一句话总结

生产部署要把“代码发布”和“数据库结构变更”分开管理：镜像固化代码，Alembic 按版本升级数据库，应用启动不偷偷执行 DDL；部署前先备份，迁移后验证表结构、容器状态和健康接口，出现问题才有回滚路径。

## 核心概念

### 镜像、容器、数据卷

```text
Dockerfile  → 如何构建镜像
镜像        → 固化的应用包
容器        → 镜像的运行实例
数据卷      → 独立于容器生命周期的持久化数据
```

本项目的 MySQL 数据在 named volume `justdoit-backend_mysql_data` 中。重建 backend 容器不会删除 MySQL 数据；`docker compose down -v` 才会删除数据卷，是破坏性操作。

### `create_all()` 和 Alembic

```text
create_all()  发现没有表就创建；不记录版本，不安全处理已有表结构变化
Alembic       每次变化一个 revision，有 upgrade/downgrade，可审查、可追踪
```

应用启动时执行 `create_all()` 会把数据库 DDL 混进应用生命周期，多个实例同时启动也可能竞争；生产应在部署步骤中显式执行迁移。

### baseline / stamp

已有数据库没有迁移历史时，不能直接执行一个假定的 initial migration。先人工确认线上结构与代码一致，再：

```bash
alembic stamp 0001_baseline
```

`stamp` 只记录版本号，不执行建表或修改字段。之后新结构变化必须创建新的 revision。

本次服务器实际结构此前由手工 SQL 创建，确认一致后在新容器中执行了：

```text
alembic stamp 0001_baseline
alembic current → 0001_baseline (head)
```

### 迁移前后验证

```text
迁移前：备份数据库 + 核对目标表/字段
迁移中：只执行明确、已审核的 SQL
迁移后：SHOW CREATE TABLE + 数据量 + 应用健康检查
```

数据库迁移不是“命令返回 0 就算成功”。本次第一次聊天字段命令返回成功，但核验发现字段没有落表，重新执行后才确认 `SHOW COLUMNS` 中存在 `user_id`。这说明迁移必须以最终结构检查为准。

## 面试高频问答

**Q：为什么生产不用 `create_all()`？**

它只能创建不存在的表，不提供版本历史、升级顺序、数据回填和可靠回滚。生产结构变更需要 Alembic revision，让每次变化可审查、可追踪、可在不同环境重复执行。应用启动只负责启动，不应偷偷改数据库结构。

**Q：已有数据库没有 Alembic 历史，怎么接入？**

先备份，再把线上实际表结构与当前模型和历史迁移逐项对比。确认一致后用 `alembic stamp` 写入基线版本，不执行 DDL；之后所有变化新增 revision。不能直接把当前模型当作 initial migration 去执行，因为它可能重复创建已有表或错误处理历史数据。

**Q：为什么迁移前必须备份？**

DDL 可能自动提交，部分数据库的结构操作不能像普通 DML 一样完整回滚；数据回填也可能不可逆。备份提供最后恢复路径。备份后还要校验文件存在、大小合理，最好保存校验和。

**Q：Docker Compose 的 `depends_on` 能保证数据库可用吗？**

普通 `depends_on` 只保证容器启动顺序；配置 `condition: service_healthy` 后可以等待 healthcheck，但它仍不负责数据库迁移，也不代表业务查询一定成功。迁移和健康探针是两个独立步骤。

**Q：为什么数据库密码不能写在 alembic.ini？**

配置文件会进入代码仓库和镜像，密码泄露后影响整个数据库。迁移配置应从环境变量读取，和应用使用同一份受保护的运行时配置。本项目 `alembic/env.py` 从 `settings.database_url` 读取。

**Q：部署失败怎么回滚？**

代码回滚和数据库回滚必须分别评估。代码可以切换到上一镜像；数据库只有在对应 downgrade 经过演练、且数据变更可逆时才能 downgrade，否则应停止发布并从备份恢复。不能把 `docker compose down -v` 当作回滚，那会删除持久化数据。

## 本项目实战

- `Dockerfile:20` — 安装生产依赖，包含 Alembic
- `Dockerfile:25` — 复制 `app/`、`alembic.ini` 和 `alembic/` 进入镜像
- `docker-compose.yml:50` — backend 自动重启；MySQL 使用 healthy 条件
- `docker-compose.yml:84` — named volume 持久化 MySQL 数据
- `app/main.py:31` — 生产环境跳过 `create_all()`，要求提前迁移
- `alembic/env.py:18` — 从 settings 读取数据库 URL，加载 SQLModel metadata
- `alembic/versions/0001_baseline.py:1` — 已有结构的空基线，不自动改表
- `docs/migrations/001_tasks.sql` — tasks 表迁移 SQL
- `docs/migrations/002_task_operation_records.sql` — 操作记录表迁移 SQL
- `docs/migrations/001_add_chat_message_owner.sql` — 聊天 owner 字段的两阶段迁移说明
- `/home/ubuntu/backup-justdoit-20260819-143041/personal_ai-before-task-migration.sql` — 生产迁移前备份

本次服务器实际结果：

```text
backend 镜像构建成功
backend 容器重建成功
MySQL healthy，未重启
Alembic current = 0001_baseline (head)
/api/health = 200，database=ok
/api/tasks 未认证 = 401
/api/messages 未认证 = 401
```

## 易错点

- **把数据库迁移放进应用启动。** 启动失败和结构变化耦合，多个实例还有竞争问题。
- **只看迁移命令退出码。** 必须用数据库查询确认结构真的变化。
- **生产直接 `alembic upgrade head` 而没有 baseline。** 没有历史版本时可能重复创建已有表。
- **把 `stamp` 当成迁移。** `stamp` 只写版本号，不执行任何结构变化。
- **迁移前不备份。** DDL 和数据回填可能无法完整回滚。
- **用 `docker compose down -v` 清理部署。** 会删除 MySQL 数据卷，不能当普通重启命令。
- **覆盖服务器 `.env`。** 上传代码时必须排除生产配置和密钥。
- **只验证容器 Up。** 容器在运行不等于 API 可用，还要检查健康接口和关键受保护路由。

## 记忆口诀

```text
镜像固化代码，数据卷保存数据
生产不自动建表，迁移按版本管理
已有数据库先备份，对齐结构再 stamp
迁移不能只看退出码，结构验证才算数
代码和数据库分开发布，健康检查最后验
```
