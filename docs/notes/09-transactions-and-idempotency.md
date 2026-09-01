# 09 事务边界、操作记录与幂等性

> 对应迭代 4 · 已完成

## 一句话总结

事务边界属于业务用例，因此由 Service 控制，不由单个 Repository 提前 `commit`；任务状态更新和操作记录必须在同一个事务中，任何一步失败就一起回滚。幂等性则保证同一个业务请求重试时不会重复产生状态变化或操作记录。

## 核心概念

### Service 控制事务边界

Repository 只负责：

```text
session.add()
session.flush()
查询
```

Service 负责：

```text
调用一个或多个 Repository
  ↓
全部成功 → session.commit()
任意失败 → session.rollback()
```

为什么不能让 Repository 自己 `commit`？因为 Repository 不知道当前操作是不是完整业务流程的一部分。

```text
错误：task_repo.insert() → commit
      operation_repo.insert() → 失败
      结果：任务有了，操作记录没了

正确：task_repo.insert() → flush
      operation_repo.insert() → flush
      Service → commit
      任意失败 → 两者一起 rollback
```

### `flush` 和 `commit` 的区别

```text
flush   把 SQL 发给数据库，拿到自增 id，但事务仍未结束，可以 rollback
commit  正式提交事务，修改持久化
rollback 撤销当前事务中尚未提交的所有修改
```

所以 Repository 可以 `flush` 来获得 `task.id`，但不要提前 `commit`。

### 操作记录（Audit Trail）

状态字段只能告诉我们“现在是什么”：

```text
status = completed
```

操作记录还能告诉我们“谁在什么时候做了什么”：

```text
user_id=1
 task_id=10
 action=complete
 from_status=in_progress
 to_status=completed
 created_at=...
```

这对排查、审计、统计、用户展示都重要。操作记录不是普通日志：日志可能被轮转或丢失，操作记录是业务数据，应持久化在数据库里。

### 幂等性

> 同一个业务请求执行一次和执行多次，最终结果应该一致。

移动端网络不稳定时，客户端可能不知道第一次请求是否成功，于是重试：

```text
第一次 POST /tasks/10/start
服务端已成功，但响应在网络中丢失
客户端重试同一个请求
```

没有幂等处理，第二次会看到任务已经 `in_progress`，可能返回 409，甚至重复记账。带上同一个 `Idempotency-Key` 后，服务端记录第一次处理结果，后续相同 key 直接返回，不再次执行动作。

```http
Idempotency-Key: start-task-once
```

幂等键的作用域应该明确。常见做法是使用 `(user_id, idempotency_key)` 唯一约束，同一个用户不能把一个 key 用于两个不同操作；不同用户可以各自使用同名 key。

### 应用判断 + 数据库兜底

实现通常分两层：

```text
前置 SELECT：大多数重试直接快速返回
数据库 UNIQUE：并发请求同时通过 SELECT 时，仍保证不会重复
```

因为两个并发请求可能同时查到“没有”，所以不能只靠前置查询。唯一约束是最终防线。

## 面试高频问答

**Q：事务应该放在哪一层？**

放在 Service 层。事务边界对应一个完整业务用例，而不是某个 SQL 操作。一个用例如果要更新任务、写操作记录、扣积分，这些操作必须整体成功或整体失败，Service 最清楚这个边界；Repository 只负责数据访问，使用 `flush` 而不 `commit`。

**Q：为什么 Repository 不应该自己 commit？**

因为它不知道自己是否会被其他 Repository 组合。如果每个 Repository 都提前提交，多步骤业务中前面成功、后面失败就无法整体回滚，产生部分提交。Repository 可以 flush 拿到主键，最终 commit 交给 Service。

**Q：flush 和 commit 有什么区别？**

flush 将待执行 SQL 发给数据库，使数据库生成的自增 id 可用，但当前事务还没有提交，仍能 rollback；commit 正式结束事务，数据持久化。flush 解决“我需要 id”，commit 解决“业务流程成功了”。

**Q：什么是幂等性？为什么重要？**

同一请求执行一次或多次，最终结果一致。网络重试、客户端重复点击、消息队列至少一次投递都会造成重复请求。通过 `Idempotency-Key` 识别同一个业务请求，并用数据库唯一约束兜底，避免重复扣款、重复创建订单、重复状态变化等问题。

**Q：操作记录和日志有什么区别？**

日志记录系统运行过程，服务于开发排障，可能采样、轮转或丢失；操作记录是业务数据，记录谁对哪个资源做了什么，通常需要长期保存、可查询、可审计。订单支付、权限变更、任务状态变化都更适合操作记录。

**Q：幂等键被复用于另一个操作怎么办？**

返回 409 冲突，不能静默把它当成第一次请求。一个幂等键代表一个明确的业务请求，复用到不同任务或不同动作说明客户端使用错误。数据库唯一约束负责阻止重复，Service 负责把冲突翻译成 `IDEMPOTENCY_KEY_CONFLICT`。

**Q：异常时为什么一定要 rollback？**

数据库 Session 进入失败事务状态后，后续查询和写入通常都会被拒绝；更重要的是，如果不 rollback，当前修改可能留在事务上下文里，造成后续请求使用脏状态。捕获异常后先 rollback，再决定重新抛出或转换成业务异常。

## 易错点

- **Repository 内提前 commit。** 多步业务无法整体回滚。
- **把 flush 当成 commit。** flush 后事务仍可回滚，别误以为已经持久化。
- **只靠 SELECT 判断幂等。** 并发请求会同时通过，必须有数据库唯一约束。
- **只记录 to_status，不记录 from_status。** 无法还原发生了哪次状态变化。
- **幂等键只存内存。** 服务重启或多实例部署后会失效，必须持久化或使用共享存储。
- **操作记录和主操作分开提交。** 会出现“状态已变但无审计记录”，两者必须同事务。
- **捕获异常不 rollback。** Session 会留在 failed transaction 状态，后续操作继续报错。
- **把所有 POST 都机械地做成幂等。** 幂等语义要针对具体业务；状态变更、支付、创建订单通常需要，纯查询不需要。

## 记忆口诀

```text
Repository 负责操作，Service 决定事务
flush 拿 id，commit 定成功
主操作和操作记录，同一事务一起走
重试请求带幂等键，唯一约束做兜底
前置查询为性能，数据库约束保正确
```
