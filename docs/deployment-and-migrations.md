# 部署与数据库迁移学习记录

## 当前部署结构

> **注意本地与线上现在不是同一套存储。** 2026-09 完成了 MySQL → PostgreSQL 全量迁移，
> 但只在本地执行并对账；云节点（139.155.96.143）仍在跑迁移前的 MySQL 版本，尚未重新部署。
> 下面先写 compose 定义的目标形态，再单列线上现状，两者不要混着看。

### compose 定义（本地已验证）

```text
客户端
  ↓ HTTP :80 / HTTPS :443（唯一公网入口）
nginx 容器: 反向代理 + TLS + gzip
  ↓ compose 内网服务名 backend:8000
backend 容器: uvicorn app.main:app
  ├─→ 服务名 postgres:5432 ── postgres 容器: pgvector/pgvector:pg18
  │                             ↓
  │                           named volume: postgres_data
  └─→ 服务名 redis:6379 ───── redis 容器: 缓存(appendonly)
                                ↓
                              named volume: redis_data
```

- `Dockerfile` 使用 `python:3.12-slim`，生产镜像只复制 `app/` 和生产依赖。
- `docker-compose.yml` 定义 5 个服务：nginx、backend、postgres、redis、adminer。
- 只有 nginx 映射公网端口（80/443）。backend 用 `expose` 仅内网可见，postgres 不映射 5432（本地开发靠 `docker-compose.override.yml` 额外绑 `127.0.0.1:5432`），adminer 绑定 `127.0.0.1:8080`（需 SSH 隧道访问），公网扫描器打不到它们。
- 镜像用 `pgvector/pgvector:pg18` 而不是官方 `postgres:18`，因为 RAG 模块需要 `vector` 扩展，改用带扩展的镜像比进容器手工编译省事。
- **PG 18 的卷挂载点是 `/var/lib/postgresql`，不是 `/var/lib/postgresql/data`。** 18+ 的官方镜像改成 `pg_ctlcluster` 兼容布局，挂错层级容器会反复重启。这个坑踩过一次。
- `depends_on.condition: service_healthy` 只解决启动顺序和数据库就绪，不等于数据库迁移。postgres 的健康检查用 `pg_isready`。
- `postgres_data` / `redis_data` / `nginx_logs` 是持久化卷，普通 `docker compose down` 不会删除数据；带 `-v` 是破坏性操作，除非明确要清空数据库，否则不要执行。

### 线上现状（待重新部署）

云节点仍是迁移前的形态：backend + MySQL 8.0 容器 + `mysql_data` 卷，**没有 redis，也没有 postgres**。
所以线上的缓存层是空转的（`app/core/cache.py` 是 fail-open 设计，连不上 Redis 不报错、直接回落到查库），这一点在看线上性能数据时要记得。

重新部署到线上的顺序见文末「已知待办」的第一条，它是一次带数据迁移的部署，不能当普通发版做。

## Nginx 反向代理

配置文件在 `nginx/conf.d/`：

```text
justdoit.conf        http 级配置(log_format/gzip/resolver) + 80/443 两个 server 块
locations.inc        被两个 server 共同 include 的 location 规则
proxy_headers.inc    转发给后端时设置的请求头
```

原理和踩坑详见 `docs/notes/13-nginx-reverse-proxy-and-https.md`，这里只记操作。

改完配置的正确顺序（先校验，再平滑重载，不断连接）：

```bash
sudo docker exec personal-ai-nginx nginx -t
sudo docker exec personal-ai-nginx nginx -s reload
```

注意 `reload` 只对 Nginx 配置生效。改了 compose 的端口映射必须重建容器：

```bash
sudo docker compose up -d --no-deps nginx
```

### 证书

当前是自签名证书（`nginx/certs/`，已在 .gitignore 中排除，不进版本库）：

```bash
cd nginx/certs && openssl req -x509 -nodes -newkey rsa:2048 -days 3650 \
  -keyout selfsigned.key -out selfsigned.crt \
  -subj "/CN=139.155.96.143" -addext "subjectAltName=IP:139.155.96.143"
chmod 600 selfsigned.key
```

浏览器和 App 会提示不安全——这是预期行为，自签名证书没有可信 CA 背书。加密强度与正式证书相同。

换成正式证书需要：注册域名 → ICP 备案（国内节点访问 80/443 必须）→ certbot 申请 Let's Encrypt 证书 → 改 `server_name` 和 `ssl_certificate` 路径两处。**HSTS 必须等到那时再开**，自签名阶段下发会让浏览器硬拒绝访问且无法撤回。

### 访问验证

```bash
curl http://139.155.96.143/api/health          # 200 {"status":"ok","database":"ok"}
curl -k https://139.155.96.143/api/health      # 200，-k 跳过自签名校验
curl http://139.155.96.143/api/tasks           # 401 + WWW-Authenticate: Bearer
curl http://139.155.96.143:8000/api/health     # 连接被拒(已退回内网)
```

### 访问 Adminer

不再对公网开放，走 SSH 隧道：

```bash
ssh -i <key> -L 8080:localhost:8080 ubuntu@139.155.96.143
# 保持连接，本机浏览器打开 http://localhost:8080
```

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
alembic/versions/cf50d924c9f7_postgres_baseline_full_schema_from_.py
                            PostgreSQL 全量基线（down_revision=None，唯一根节点）
alembic/legacy_mysql/       MySQL 时代的旧 revision，已下线归档
```

`alembic/legacy_mysql/` 里的文件**不要挪回 `versions/`**。它们的 `down_revision` 链和新基线互不相接，一旦被 Alembic 扫到就是两个 head，`upgrade head` 会直接报 `Multiple head revisions are present`。归档原因写在该目录的 README.md 里。

### schema 的唯一来源

**Alembic 是唯一的 schema 变更来源。** `docs/migrations/*.sql` 那三份手写 MySQL SQL 是历史记录，不是可执行的迁移路径：

```text
docs/migrations/001_tasks.sql                  迭代 3，已应用到线上 MySQL
docs/migrations/001_add_chat_message_owner.sql 迭代 2，已应用到线上 MySQL
docs/migrations/002_task_operation_records.sql 迭代 4，已应用到线上 MySQL
```

保留它们的唯一理由是：**线上那台库现在的结构就是这几份 SQL 手工执行出来的**，在云节点迁到 PG 之前，它们是解释线上现状的凭据。等线上迁完就可以删。新的结构变更一律走 `alembic revision --autogenerate` + 人工 review，不要再往这个目录里加文件。

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

> 这一节描述的是 MySQL 时代的处境：当时 `0001_baseline` 是**空迁移**，故意不创建、不删除业务表，
> 因为现有环境已经通过 `create_all()` 和手工 SQL 建过表，自动猜测结构有数据风险。
> **该问题已在 2026-09 的 PG 迁移中一并解决**，下面保留是因为「已有库怎么补基线」是通用问题，
> 换任何项目都会再遇到一次。

当只能拿到一个「结构已存在但没有迁移历史」的库时，正确流程：

```text
1. 备份数据库
2. 对比线上表结构和当前 models
3. 处理未完成的历史变更
4. 确认结构一致后执行 alembic stamp <baseline_revision>
5. 后续所有变更新增 revision
```

`stamp` 只写版本号，不执行 upgrade。它只能在确认结构已经存在且匹配时使用。

## 新环境和生产环境的区别

```text
新环境
  alembic upgrade head 一步到位（cf50d924c9f7 是全量建表迁移）

已有环境
  需要先对齐结构，再 stamp 基线
  不能直接 upgrade 一个假定结构不存在的迁移
```

现状：**新环境已经可以从零 `upgrade head`**。`cf50d924c9f7` 是从 SQLModel metadata 生成并人工 review 过的全量基线，`down_revision=None`，且 `upgrade()` 开头会 `CREATE EXTENSION IF NOT EXISTS vector`，不需要预先手工装扩展。空 baseline 时代「新环境无法从零建库」的缺口已经补上。

## Docker 部署建议流程

```text
1. 构建新镜像
2. 备份 PostgreSQL 数据（见下方备份命令）
3. 启动/确认 postgres healthy（pg_isready）
4. 执行 alembic upgrade head
5. 检查迁移结果和 alembic current
6. 启动 backend
7. 调用 /api/health 检查 readiness（会同时探 db 和 cache）
8. 观察 request_id、错误率和慢请求日志
```

迁移失败时不要继续启动一个依赖新结构的应用版本。先根据日志定位，必要时执行经过审核的 downgrade 或恢复备份。

### 备份与恢复

`pg_dump` 走容器内执行，不需要在宿主机装客户端：

```bash
# 逻辑备份（自定义格式，支持并行恢复和按表挑选）
sudo docker exec personal-ai-postgres \
  pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DATABASE" -Fc \
  > "backup-$(date +%Y%m%d-%H%M%S).dump"

# 只导结构，用于和 models 对账
sudo docker exec personal-ai-postgres \
  pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DATABASE" --schema-only > schema.sql
```

恢复到一个**空库**（`-c` 会先 DROP 已有对象，误用在有数据的库上不可逆，先确认目标库）：

```bash
cat backup-xxx.dump | sudo docker exec -i personal-ai-postgres \
  pg_restore -U "$POSTGRES_USER" -d "$POSTGRES_DATABASE" --no-owner
```

两个容易忽略的点：

- **`pg_dump` 不备份角色和全局对象**（用户、权限）。单库单用户的场景够用，多库要配 `pg_dumpall --globals-only`。
- **恢复后必须检查序列**。用 `pg_restore` 恢复通常会带上序列状态，但如果是手工 `INSERT` 显式 id 导入数据，序列不会自动前进，下次插入就撞主键。修法是 `setval(pg_get_serial_sequence('表名','id'), GREATEST(MAX(id),0)+1, false)`，`app/migrate_mysql_to_pg.py` 里的 `_reset_sequences()` 就是干这个的。

## 已知待办

- **线上重新部署到 PostgreSQL**：云节点仍是 MySQL 版本。这是一次带数据迁移的部署，顺序应为：备份线上 MySQL → 起 postgres + redis → 跑 `alembic upgrade head` → 用 `app/migrate_mysql_to_pg.py` 搬数据并对账 → 切 backend 镜像 → 观察后再删旧 mysql 卷。**旧卷的删除放在最后且要单独确认，`docker volume rm` 不可逆。**
- **正式 TLS 证书**：需域名 + ICP 备案，之后接入 Let's Encrypt 并启用 HSTS。
- **日志轮转**：Nginx 日志写入 `nginx_logs` 卷，目前无 logrotate，长期运行需配置。
- **安全组清理**：8000 端口的放行规则已失效（后端改用 expose），建议在云控制台删除，避免误改 compose 后后端重新暴露。

### 已闭环（原待办，留档）

- ~~**完整 initial migration**~~：2026-09 随 PG 迁移完成，`cf50d924c9f7` 即全量基线，新环境可从零 `upgrade head`。
- ~~**chat_messages.user_id 第二阶段**~~：PG 基线里该列直接是 `NOT NULL`。迁移时源库有 11 条早于鉴权改造的匿名消息，**按"不伪造归属"原则跳过**而非硬塞一个 user_id，理由记在 `docs/notes/16-mysql-to-postgres-migration.md`。
