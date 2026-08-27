# 部署与数据库迁移学习记录

## 当前部署结构

```text
客户端
  ↓
宿主机 8000
  ↓
backend 容器: uvicorn app.main:app
  ↓ compose 内网服务名 mysql
mysql 容器: MySQL 8.0
  ↓
named volume: mysql_data
```

- `Dockerfile` 使用 `python:3.12-slim`，生产镜像只复制 `app/` 和生产依赖。
- `docker-compose.yml` 负责 backend、mysql、adminer；MySQL 不映射宿主机 3306，减少暴露面。
- `depends_on.condition: service_healthy` 只解决启动顺序和数据库就绪，不等于数据库迁移。
- `mysql_data` 是持久化卷，普通 `docker compose down` 不会删除数据；带 `-v` 是破坏性操作，除非明确要清空数据库，否则不要执行。

## 为什么要 Alembic

`SQLModel.metadata.create_all()` 只能创建不存在的表，不能记录“从版本 A 到版本 B 改了什么”，也不会安全地处理已有表加字段、改类型、回填数据等变更。

生产需要可追踪的迁移历史：

```text
当前数据库版本
  ↓ alembic_version
执行缺失的 revision
  ↓
数据库达到目标版本
```

每个 revision 都有：

```python
revision = "本版本 ID"
down_revision = "上一个版本 ID"

def upgrade():    # 升级

def downgrade():  # 回滚
```

## 当前代码的安全策略

```text
开发环境 + AUTO_CREATE_TABLES=true
  → 允许 init_db() 方便学习

生产环境
  → 不自动 create_all()
  → 启动前显式执行 alembic upgrade head
```

即使错误地把 `AUTO_CREATE_TABLES=true` 配到生产，`main.py` 仍通过 `not settings.is_production` 拦截，不会启动时执行 DDL。

## 已接入的 Alembic 骨架

```text
alembic.ini                 迁移入口配置，不存数据库密码
alembic/env.py              读取 settings.database_url、加载 SQLModel metadata
alembic/script.py.mako      新 revision 模板
alembic/versions/0001_baseline.py  已有数据库的人工确认基线
```

执行常用命令：

```bash
# 查看当前数据库版本
.venv/bin/alembic current

# 查看迁移链
.venv/bin/alembic history

# 生成迁移草稿（必须人工 review）
.venv/bin/alembic revision --autogenerate -m "describe change"

# 只生成 SQL，不执行
.venv/bin/alembic upgrade head --sql

# 执行迁移（只对已经确认过的环境执行）
.venv/bin/alembic upgrade head

# 回退一个版本（生产执行前必须确认影响）
.venv/bin/alembic downgrade -1
```

## 已有数据库如何建立基线

当前 `0001_baseline` 是空迁移，故意不创建、不删除业务表。原因是现有环境可能已经通过 `create_all()` 和手工 SQL 建过表，自动猜测结构会有数据风险。

正确流程：

```text
1. 备份数据库
2. 对比线上表结构和当前 models
3. 处理未完成的历史变更
4. 确认结构一致后执行 alembic stamp 0001_baseline
5. 后续所有变更新增 revision
```

`stamp` 只写版本号，不执行 upgrade。它只能在确认结构已经存在且匹配时使用。

## 新环境和生产环境的区别

```text
新环境
  需要一份完整、可审查的 initial migration
  不能依赖空 baseline

已有环境
  需要先对齐结构，再 stamp baseline
  不能直接 upgrade 一个假定的 initial migration
```

这是当前阶段明确保留的后续工作：生成完整 initial migration，并在临时 MySQL 上演练 upgrade / downgrade。没有经过演练前，不宣称数据库迁移已经完成。

## Docker 部署建议流程

```text
1. 构建新镜像
2. 备份 MySQL 数据
3. 启动/确认 mysql healthy
4. 执行 alembic upgrade head
5. 检查迁移结果和 alembic current
6. 启动 backend
7. 调用 /api/health 检查 readiness
8. 观察 request_id、错误率和慢请求日志
```

迁移失败时不要继续启动一个依赖新结构的应用版本。先根据日志定位，必要时执行经过审核的 downgrade 或恢复备份。
