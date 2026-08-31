# 知识点总结索引

每完成一个知识点，在此新增一行。总结的目标是**脱离代码也能复述**，便于记忆和面试。

## 统一结构

每份笔记按固定五段组织：

1. **一句话总结** — 面试时的开场答法
2. **核心概念** — 必须能讲清的定义与边界
3. **面试高频问答** — 可直接开口说的答法，不是知识点罗列
4. **本项目实战** — 带 `file:line` 的真实案例
5. **易错点** — 踩过的坑与常见误区

## 笔记列表

- [01 分层架构与依赖注入](01-layered-architecture.md) — 五层职责边界、DI 的实际价值、如何判断分层是否守住
- [02 异常处理与统一响应](02-exception-handling.md) — 业务异常与 HTTP 解耦、全局处理器、日志与用户提示的分野
- [03 DTO 与响应白名单](03-dto-and-schema.md) — 为什么不能直接返回 ORM 模型、校验该放哪一层
- [04 密码存储与 JWT 鉴权](04-auth-jwt.md) — bcrypt、JWT 结构、fail closed、防用户名枚举
- [05 测试基建与依赖覆盖](05-testing.md) — SQLite 内存库、dependency_overrides、该测什么
- [06 认证与授权：资源所有权](06-authorization-ownership.md) — JWT 身份、owner 外键、SQL 层过滤与越权防护
- [07 数据建模与索引约束](07-data-modeling-and-indexes.md) — 建模先于接口、主键取舍、外键与索引分工、唯一约束兜竞态
- [08 任务状态机与软删除](08-task-state-machine-and-soft-delete.md) — 状态转换表、409 语义、软删除、所有权 404、分页 total
- [09 事务边界、操作记录与幂等性](09-transactions-and-idempotency.md) — Service 控制 commit、flush/rollback、审计记录、Idempotency-Key
- [10 自动化测试工程化与 CI 门禁](10-test-engineering-and-ci.md) — 测试分层、覆盖率、架构测试、隔离与持续集成
- [11 日志与可观测性](11-logging-observability.md) — request_id、请求耗时、慢请求、健康检查与错误关联
- [12 Docker、Alembic 与部署](12-docker-alembic-deployment.md) — 镜像分层、数据卷、迁移基线、生产部署顺序
- [13 Nginx 反向代理与 HTTPS](13-nginx-reverse-proxy-and-https.md) — 为什么要反代、真实 IP 透传、SSE 不缓冲、TLS 配置、容器 DNS 缓存坑
- [14 Redis 缓存与 Cache-Aside](14-redis-cache.md) — fail open、穿透/击穿/雪崩、失效顺序、SCAN 批量删、缓存该放哪一层

## 待补（随迭代推进）

- 15 索引与慢查询分析（迭代 8）
- 16 后台任务与限流（迭代 8）
