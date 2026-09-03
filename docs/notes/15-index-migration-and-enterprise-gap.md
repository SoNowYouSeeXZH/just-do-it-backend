# 15 索引迁移与企业级变更流程

> 对应迭代 8 · 索引与慢查询分析
>
> **本篇示例基于 MySQL 时代。** 项目已于 2026-09 迁到 PostgreSQL（见笔记 16），
> 文中 `EXPLAIN` 的 type/key/Extra 列、`SET GLOBAL slow_query_log`、gh-ost / pt-osc
> 都是 MySQL 特有的东西。方法论（先看查询再设计索引、迁移要可审查可回滚）不变，
> 命令要换。PG 对照见下面的「换到 PostgreSQL 之后」一节。

## 一句话总结

索引优化不能只停留在“给字段加索引”。完整流程是：从真实查询出发，用 `EXPLAIN` 找到瓶颈，设计合适的索引，通过版本化迁移安全变更数据库，并在执行前后验证效果。小项目可以简化流程，企业级项目必须增加测试环境、人工审核、备份和可回滚方案。

## 核心概念

### 先分析查询，再设计索引

```sql
EXPLAIN SELECT * FROM tasks
WHERE user_id = 1
  AND deleted_at IS NULL
  AND status = 'pending'
ORDER BY created_at DESC
LIMIT 20;
```

重点关注：

- `type`：`ALL` 通常表示全表扫描，`ref`、`range` 等通常表示使用了索引
- `key`：实际使用的索引名，`NULL` 表示没有使用索引
- `rows`：优化器估算需要扫描的行数，越接近全表行数越值得关注
- `Extra`：出现 `Using filesort` 表示排序没有直接利用索引

`EXPLAIN` 是优化器的执行计划，不是实际耗时报告。它应与真实数据量、慢查询日志和接口监控结合使用。

### 复合索引的列顺序

对于下面的查询：

```sql
WHERE user_id = ?
  AND deleted_at IS NULL
  AND status = ?
ORDER BY created_at DESC
```

可以考虑建立：

```sql
CREATE INDEX ix_tasks_user_deleted_status_created
ON tasks (user_id, deleted_at, status, created_at);
```

通常将等值过滤字段放在前面，将排序字段放在后面，使索引尽可能同时服务于过滤和排序。但这不是机械规则，最终仍需用 `EXPLAIN` 验证，因为选择性、数据分布、查询比例和数据库版本都会影响优化器决策。

索引遵循最左前缀原则：复合索引 `(a, b, c)` 通常能支持 `a`、`a+b`、`a+b+c` 的查询，但不能充分支持只按 `b` 或 `c` 查询。索引列越多，写入维护成本和存储成本越高，因此不要盲目增加索引。

### Alembic 迁移

数据库结构变更应该写成版本化的 migration，而不是只在某个环境手动执行 SQL：

```python
def upgrade() -> None:
    op.create_index(
        "ix_tasks_user_deleted_status_created",
        "tasks",
        ["user_id", "deleted_at", "status", "created_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_tasks_user_deleted_status_created",
        table_name="tasks",
    )
```

`upgrade` 描述向前升级要做什么，`downgrade` 描述撤销这次变更要做什么。迁移工具通过版本表记录当前数据库版本，执行升级时只运行尚未执行的 revision。

### 写得出 downgrade 不等于回滚安全

索引是数据的冗余副本，`drop_index` 不会丢任何一行业务数据，因此上面这个 revision 的回滚风险只在性能和锁，不在数据。但并非所有变更都这样，按"能否安全 downgrade"可以分三档：

- 可逆：加索引、加可空列、加新表、加默认值。回滚只影响性能或删掉尚无人依赖的对象
- 有条件可逆：加非空列、重命名列。需要确认回滚时没有代码仍在依赖，重命名通常还要经过双写过渡期
- 不可逆：缩短字段长度、删列、删表、类型收窄、数据转换与回填。回滚必然损失信息

第三类最容易被 downgrade 的存在骗过去。以扩长字段为例：

```python
# upgrade: VARCHAR(50) -> VARCHAR(255)
op.alter_column("tasks", "title", type_=sa.String(255))
```

升级后一旦写入长度超过 50 的数据，反向执行时严格模式会以 `Data too long for column` 中断，把库留在中间状态；非严格模式则静默截断，数据无声丢失。另外 `VARCHAR` 跨过 255 字节边界时长度前缀由 1 字节变为 2 字节，本身就需要重建表，不属于 instant DDL。

对这类变更，正确做法不是补一个看起来能跑的 downgrade，而是显式挡住：

```python
def downgrade() -> None:
    raise NotImplementedError(
        "缩短 title 长度不可逆，回滚请走备份恢复流程"
    )
```

这样 CI 验证 downgrade 时会立即失败，迫使团队在发布前就确认这次变更没有回头路，而不是在生产事故中才发现。

### 慢查询日志

应用层慢请求日志和数据库慢查询日志解决的是不同问题：

- 应用层日志：一次 HTTP 请求整体花了多久
- 数据库慢查询日志：某条 SQL 在数据库中执行了多久

可以在测试环境或经过评估的生产窗口开启 MySQL 慢查询日志：

```sql
SET GLOBAL slow_query_log = 'ON';
SET GLOBAL long_query_time = 0.1;
```

生产环境还需要考虑日志文件位置、磁盘容量、保留周期和脱敏要求。开启日志不是终点，还要定期分析慢 SQL，并结合执行计划和业务场景决定是否优化。

### 大表加索引

直接执行 `CREATE INDEX` 需要扫描数据并构建索引，会消耗 CPU、磁盘和 I/O。即使数据库支持 Online DDL，也不代表完全没有锁等待或业务影响。

大表变更通常需要：

1. 在接近生产数据量的环境评估耗时和锁影响
2. 选择低峰期执行并设置超时、监控和终止方案
3. 必要时使用 `gh-ost` 或 `pt-online-schema-change` 等在线变更工具
4. 执行后验证索引、慢查询和接口延迟

### 换到 PostgreSQL 之后

上面四处 MySQL 专有做法在 PG 里的对应物，逐条换：

- **看执行计划**：`EXPLAIN` 换成 `EXPLAIN (ANALYZE, BUFFERS)`。MySQL 看 type/key/rows/Extra 这几列，PG 输出的是计划树，要看的是节点类型（`Seq Scan` / `Index Scan` / `Bitmap Heap Scan`）、`actual time` 与 `rows` 的**估算值 vs 实际值差距**（差得大说明统计信息过期，该 `ANALYZE`），以及 `Buffers` 里的 shared hit/read 比例。注意带 `ANALYZE` 会真的执行语句，写操作要包在事务里回滚。
- **慢查询日志**：没有 `slow_query_log` 开关，直接设 `log_min_duration_statement = 100`（毫秒，`-1` 关闭、`0` 记全部）。可以 `ALTER SYSTEM SET` 后 `SELECT pg_reload_conf()`，也可以只对某个库或某个用户设。
- **找出该优化哪条 SQL**：MySQL 靠翻慢日志 + `pt-query-digest`，PG 更好用的是 `pg_stat_statements` 扩展——它按「语句模板」聚合累计耗时、调用次数、平均耗时，直接 `ORDER BY total_exec_time DESC` 就是优化清单，不用先攒日志。
- **大表加索引**：不需要 gh-ost / pt-osc 这类外部工具，PG 自带 `CREATE INDEX CONCURRENTLY`（`DROP INDEX CONCURRENTLY` 同理）。代价是：不能在事务块里执行（所以 Alembic 迁移里要 `autocommit_block()`）、要扫两遍表因此更慢、失败会留下一个 `INVALID` 的废索引需要手工 drop 后重建。

一个反直觉的差异值得记住：**PG 的加列比 MySQL 更省事**。PG 11 起「加带默认值的非空列」是纯元数据操作，不重写表；而 MySQL 的很多 DDL 仍要重建表，这正是 gh-ost 这类工具存在的原因。所以「大表变更必须上在线 DDL 工具」这条经验是 MySQL 语境下的，换到 PG 要重新判断。

## 企业级迁移流程

```text
需求与查询分析
  ↓
EXPLAIN 评估索引设计
  ↓
编写 migration 并进行代码审查
  ↓
CI 验证 upgrade / downgrade
  ↓
staging 使用接近生产的数据验证
  ↓
生产变更审批、备份、低峰期执行
  ↓
验证结构、健康状态、错误率和延迟
  ↓
观察窗口结束后完成发布
```

已有数据库没有迁移历史时，不能把当前模型直接当作初始迁移执行。应先备份并核对实际结构，确认一致后用 `stamp` 标记基线。`stamp` 只记录版本，不执行 DDL；之后的结构变更再通过新的 revision 完成。

数据库迁移和应用代码发布也应尽量解耦。优先设计向后兼容的变更，例如先新增字段或索引，再发布使用它的代码；删除字段等破坏性变更则要经过更长的兼容周期。

## 面试高频问答

**Q：为什么索引不是越多越好？**

索引能加速读取，但每次插入、更新和删除都要维护相关索引，同时增加存储和缓存压力。应从真实查询条件出发，用执行计划验证收益，而不是给每个字段都建索引。

**Q：单列索引和复合索引怎么选择？**

如果查询经常同时使用多个条件并带有固定排序，应评估复合索引。一般将等值过滤列放前面，将范围条件或排序列放后面，但最终要用真实数据和 `EXPLAIN` 验证。还要遵守最左前缀原则，避免建立无法被主要查询利用的索引。

**Q：`EXPLAIN` 中哪些信息最重要？**

先看是否全表扫描，再看实际使用的 `key`、预估扫描行数 `rows`，最后看 `Extra` 中是否出现 `Using filesort` 或 `Using temporary`。不过 `EXPLAIN` 是估算结果，仍需结合真实耗时和慢查询日志。

**Q：为什么生产环境不能只手动执行一条 `CREATE INDEX`？**

手动执行缺少版本记录、审查记录和跨环境一致性，也无法自然重放。应该将变更写入 migration，由发布流程在指定环境执行，并在执行前备份、执行后验证。大表还要评估锁等待和资源消耗。

**Q：`stamp` 和 `upgrade` 有什么区别？**

`stamp` 只修改迁移版本记录，不执行表结构变化，适合把已经人工确认过的数据库对齐到基线。`upgrade` 才会执行 revision 中的 DDL，将数据库实际结构向目标版本推进。

**Q：数据库迁移失败后一定要 `downgrade` 吗？**

不一定。只有在 downgrade 经过演练且变更可逆时才适合回退。涉及数据删除、数据转换或不可逆操作时，应停止发布并根据备份恢复。代码回滚和数据库回滚必须分别评估。

**Q：字段长度从 50 扩到 255 之后还能回滚吗？**

不能当作可逆变更。升级后只要写入过超过 50 的数据，反向 `alter_column` 在严格模式下会因 `Data too long for column` 失败，在非严格模式下会静默截断并永久丢数据。这类 revision 的 downgrade 应直接抛异常，把回滚路径明确指向备份恢复。加索引这类只影响冗余结构的变更才真正可逆。

**Q：为什么迁移要和应用发布解耦？**

应用代码可以切换到上一版本，但数据库结构变化不一定能安全撤销。解耦后可以先执行向后兼容的迁移，再发布代码，并分别观察和处理两个环节的问题。

## 易错点

- 只凭字段名称判断是否需要索引，不分析真实查询
- 把 `EXPLAIN` 的估算结果当成实际性能结论
- 只建立单列索引，不考虑多条件过滤和排序的组合
- 忽略复合索引的最左前缀原则
- 改了依赖清单却不重新构建应用镜像
- 在没有 staging 验证的情况下直接对大表执行 DDL
- 只看迁移命令退出码，不核对最终表结构
- 把 `stamp` 误认为已经执行了数据库变更
- 迁移前没有备份，失败后只能依赖未经演练的回滚脚本
- 因为 revision 里写了 downgrade 就认定这次变更可以安全回滚
- 给字段长度收窄、删列、数据回填这类不可逆变更补一个"能跑通"的 downgrade
- 把数据库迁移和应用代码发布绑定成一个不可拆分的操作
- 用 `docker compose down -v` 作为普通清理命令，误删持久化数据
- 生产环境开启慢查询日志时没有规划磁盘、保留和脱敏

## 记忆口诀

```text
先看查询，再设计索引
执行计划只是估算，真实数据还要验证
等值条件靠前，排序字段靠后
迁移版本化，升级可追踪
写得出 downgrade，不等于回滚安全
索引可逆，缩字段删列不可逆
生产先备份，执行后核对
小表直接评估，大表在线变更
代码和数据库分开发布
```
