# 07 数据建模与索引约束

> 对应迭代 3 · 已完成

## 一句话总结

数据建模先于接口设计：先想清业务实体、关系和不变量，再决定表结构。约束（唯一、非空、外键）应优先交给数据库兜底，索引则按「实际查询条件」而非「字段多就加」来建——加错了既浪费写性能，也救不了慢查询。

## 核心概念

### 建模先于接口

接口是业务能力的入口，表结构才是业务的底座。先回答业务问题，再落到字段：

```text
什么是任务？有哪些状态？谁拥有它？
  ↓
users 1 ──── N tasks
  ↓
tasks 表：id / owner_id / title / status / due_at / created_at
```

字段一旦上线就难改（历史数据、依赖方），所以**字段类型、长度、是否可空、是否唯一**都要在第一次建表时想清楚，而不是事后打补丁。

### 主键的两种选择

```python
# 自增 int：适合「会持续累积、数量无上限」的实体
id: int | None = Field(default=None, primary_key=True)

# 业务字符串主键：适合「有限且稳定的枚举」，省去一层 id 转换
id: str = Field(primary_key=True, max_length=32)
```

判断标准：这个实体的数量会不会无限增长。用户、消息、题目会持续累积，用自增；职业、行业是有限枚举且前端路由直接用业务 id，用字符串主键。

### 外键保证正确性，索引保证性能

两者职责不同，不要混淆：

```python
user_id: int = Field(foreign_key="users.id", index=True)
```

- **外键**：数据库拒绝插入一个指向不存在用户的 `user_id`，保证引用完整性（正确性）。
- **索引**：让 `WHERE user_id = ?` 走索引而非全表扫描（性能）。

外键不带索引也能保证正确性，但按 owner 过滤的查询会退化成全表扫描。所以「按某列查询」的列，外键与否都要加索引。

### 唯一约束兜底竞态

「先查一下不存在，再插入」在并发下会双双插入：

```text
请求 A: SELECT ... 没有 → INSERT
请求 B: SELECT ... 没有 → INSERT   ← 两次查询都说"不存在"
```

正确做法是依赖数据库唯一索引，把插入失败翻译成业务含义：

```python
try:
    return user_repo.insert(session, username=username, ...)
except IntegrityError as exc:
    session.rollback()
    raise UsernameTakenError("用户名已存在") from exc
```

数据库层和代码层共同保证：**数据库兜底正确性，代码层负责给出友好提示**。

### 索引该建在「真实查询条件」上

索引不是越多越好。每加一个索引，写入要多维护一棵树。判断依据是**实际查询模式**：

| 查询 | 该建的索引 |
|------|-----------|
| `WHERE username = ?`（登录） | `username` 唯一索引 |
| `WHERE user_id = ? ORDER BY created_at DESC` | `user_id` 索引 + `created_at` 索引 |
| `WHERE job_id = ? ORDER BY rand() LIMIT 10` | `job_id` 索引 |
| 不按 `password_hash` 查 | 不建索引 |

「按时间倒序取最近 N 条」要给 `created_at` 建索引，否则 `ORDER BY` 配合 `LIMIT` 仍可能全表排序。

### 何时拆子表，何时用 JSON 列

```text
拆子表：子数据需要独立查询（按 job_id 随机抽题）
JSON 列：子数据永远和整行一起读、一起写（选项列表、要点列表）
```

题目选项永远和题目一起读、一起写，没有独立查询需求，用 JSON 列省去 join；题目本身要按 job_id 抽样，必须独立成行。

## 面试高频问答

**Q：唯一性应该由代码 if 判断，还是由数据库约束保证？**

优先数据库。代码层「先查后插」存在竞态：两个并发请求都查到「不存在」然后双双插入。唯一索引让数据库兜底，代码捕获 `IntegrityError` 翻译成「用户名已存在」的友好提示。两者配合：数据库保证正确，代码保证体验。

**Q：外键和索引有什么区别？必须一起加吗？**

外键保证引用完整性（不能指向不存在的行），索引保证查询性能。它们解决不同问题。外键本身不会自动建索引（MySQL/PostgreSQL 行为不同），所以「按外键列查询」时通常要显式加索引。本项目 `chat_messages.user_id` 同时声明 `foreign_key` 和 `index=True`，就是两者各司其职。

**Q：主键用自增 int 还是业务字符串？**

看实体是否无限增长。用户、消息、题目会持续累积，用自增 int；职业、行业是有限枚举且前端路由直接用业务 id，用字符串主键省去「业务 id ↔ 自增 id」的转换层。没有银弹，按业务特征选。

**Q：索引建多了有什么坏处？**

每次插入/更新都要同步维护所有相关索引，写入变慢、存储变大。索引应该建在「真实查询条件」上，而不是「字段看起来重要」就加。不按某列查询就不要给它建索引，比如 `password_hash`。

**Q：什么时候该拆子表，什么时候用 JSON 列？**

看子数据有没有独立查询需求。题目要按 job_id 随机抽样，必须独立成行才能用 SQL 高效采样，所以拆 Question 子表。题目选项永远和题目一起读一起写，没有独立查询，用 JSON 列省 join。判断标准是「会不会单独 WHERE 这个子数据」。

**Q：`created_at` 为什么要建索引？**

「按时间倒序取最近 N 条」这种查询，没有索引时数据库要么全表扫描再排序，要么无法高效定位。给 `created_at` 建索引后，配合 `ORDER BY created_at DESC LIMIT N` 可以直接走索引逆序取前 N 行。本项目 `list_recent` 就是这个模式。

## 本项目实战

- `app/models/user.py:23` `id` — 自增主键，`Optional[int]` + `default=None` 是 SQLModel 惯用法
- `app/models/user.py:27` `username` — `unique=True` + `index=True`，登录走索引、注册靠唯一约束兜底竞态
- `app/models/user.py:38` `created_at` — `default_factory=datetime.now` + `index=True`，支持按时间倒序查询
- `app/models/message.py:27` `user_id` — `foreign_key="users.id"` + `index=True`，外键保正确、索引保性能
- `app/models/message.py:31` `role` + `:41` `created_at` — 两个索引分别支撑按角色、按时间过滤
- `app/models/job.py:31` `Job.id` — 业务字符串主键（`frontend`/`backend`...），省去 id 转换层
- `app/models/job.py:53` `Question.job_id` — 外键 + 索引，让「按职业抽题」先缩范围再随机
- `app/models/job.py:65` `options` / `:71` `answer_indices` — JSON 列，永远和题目一起读写，不拆子表
- `app/models/job.py:82` `content_hash` — `unique=True` + `index=True`，爬取去重的兜底约束
- `app/models/industry.py:30` `key_points` — JSON 列，纯只读知识条目无独立查询需求
- `app/services/user.py:55-66` `register` — 不先查重，直接插入并捕获 `IntegrityError` 翻译为业务异常
- `app/repositories/user.py:40-43` `insert` — `commit` 后 `refresh` 回读自增 id 与 `created_at`
- `app/repositories/message.py:14-20` `list_recent` — `WHERE user_id` + `ORDER BY created_at DESC` + `LIMIT`，三个索引共同支撑
- `app/repositories/question_bank.py:13-25` `existing_hashes` — 按 `content_hash` 索引取已有指纹集合做去重
- `docs/migrations/001_add_chat_message_owner.sql:24-28` — 生产迁移示例：先加可空列、回填、再收紧 NOT NULL 并建索引与外键

## 易错点

- **「先查后插」防重。** 并发下两次查询都说不存在，必须靠唯一约束兜底。
- **以为外键自带索引。** 外键保证正确性不保证性能，按外键列查询要显式加索引。
- **索引按「字段重要」建，不按「查询条件」建。** 不查询的列建索引只拖慢写入。
- **`ORDER BY ... LIMIT` 没有索引就高效。** 没有匹配索引时仍可能全表排序，排序字段要建索引。
- **历史表直接加 NOT NULL 外键。** 旧数据没有 owner 会失败，必须先加可空列、回填、再收紧。
- **依赖 `create_all()` 做生产迁移。** 它通常只建不存在的表，不改已有表结构，生产迁移要用 Alembic 或审核过的 SQL。
- **所有子数据都拆子表。** 没有独立查询需求的子数据用 JSON 列更简单；有独立查询（如随机抽样）才拆表。
- **`default=datetime.utcnow()` 而非 `default_factory`。** 模块加载时取一次时间，所有行共享同一个时间戳。
- **捕获 `IntegrityError` 后忘记 `rollback`。** 事务不回滚，该 session 后续所有操作都会失败。

## 记忆口诀

```text
先想业务再建表，字段一次定清楚
主键看是否无限增，自增字符串各有所长
外键保正确，索引保性能，两者职责不一样
唯一约束兜竞态，先查后插不可靠
索引建在查询条件上，不查的列别乱加
子表还是 JSON 列，看它要不要独立查
```
