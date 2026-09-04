# 知识点总结索引

每完成一个知识点，在此新增一行。总结的目标是**脱离代码也能复述**，便于记忆和面试。

## 统一结构

每份笔记按固定五段组织：

1. **一句话总结** — 面试时的开场答法
2. **核心概念** — 必须能讲清的定义与边界
3. **面试高频问答** — 可直接开口说的答法，不是知识点罗列
4. **实战方法** — 通过通用示例、命令和场景理解知识点
5. **易错点** — 踩过的坑与常见误区

## 笔记列表

- [01 分层架构与依赖注入](01-layered-architecture.md) — 六层职责边界、DI 的实际价值、如何判断分层是否守住
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
- [15 索引迁移与企业级变更流程](15-index-migration-and-enterprise-gap.md) — 复合索引设计、迁移完整链路、企业级变更流程、大表加索引的替代方案（示例基于 MySQL 时代，PG 对照见文内）
- [16 MySQL → PostgreSQL 全量迁移实战](16-mysql-to-postgres-migration.md) — 为什么整库切换、Alembic 基线重建、序列重置、detached 对象踩坑、方言判重适配
- [17 题库重建管道：开源直导 + LLM 有锚点改编](17-question-bank-rebuild-pipeline.md) — LLM 当适配器而非作者、Markdown 状态机解析、批量改编与溯源
- [18 垂直领域 RAG Agent：手写 ReAct 循环与可溯源引用](18-rag-agent-and-retrieval.md) — 两段式检索/综合、有界循环、服务端确定性引用组装、SearchProvider 抽象、SSRF 四道防线
- [19 聊天服务接线与灰度开关](19-chat-service-wiring-and-rag-rollout.md) — RAG 开关分流、Agent 事件到 SSE 的适配、旧链路回退与落库语义
- [20 UGC 内容审核](20-ugc-moderation.md) — 先审后发 vs 先发后审、敏感词归一化与绕过、审核状态流、可见性作为安全约束、API Key 常量时间比较

## 待补（随迭代推进）

- 预爬语料 + pgvector 向量检索（替掉实时抓取 wiki）
