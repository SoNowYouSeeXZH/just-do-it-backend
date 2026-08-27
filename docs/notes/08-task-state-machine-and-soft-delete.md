# 08 任务状态机与软删除

> 对应迭代 3 · 已完成

## 一句话总结

任务系统的核心不是 CRUD,而是状态机和所有权:状态转换必须经过一张集中的转换表校验,非法转换返回 409;删除默认走软删除(打 `deleted_at` 时间戳而非 `DELETE`),所有查询统一带 `deleted_at IS NULL` 过滤;按 id 操作任何任务前都要先确认它属于当前用户,不属于就返回 404——不泄露"存在但不属于你"。

## 核心概念

### 状态机:把"动作"和"状态"分开

```text
pending ──start──→ in_progress ──complete──→ completed ──archive──→ archived
   │                  │
   └──cancel──→ cancelled ←─cancel──┘

cancelled / archived:终态,不再有合法转换
```

状态机用一个集中映射表表达"当前状态 × 动作 → 新状态":

```python
_TRANSITIONS = {
    "pending": {"start": "in_progress", "cancel": "cancelled"},
    "in_progress": {"complete": "completed", "cancel": "cancelled"},
    "completed": {"archive": "archived"},
    "cancelled": {},
    "archived": {},
}
```

转换时查表,查不到就是非法:

```python
allowed = _TRANSITIONS.get(task.status, {})
if action not in allowed:
    raise TaskStateError(...)
task.status = allowed[action]
```

为什么用"动作"(`start`/`complete`)而不是直接 PATCH 改 `status` 字段?因为状态转换是业务动作,不是字段赋值。直接让客户端传任意 `status` 会绕过状态机——客户端可以把 `archived` 直接改回 `pending`。用动作 + 转换表,合法路径只有一份实现,改一处不会漏。

### 软删除:删的是可见性,不是数据

```text
物理删除  DELETE FROM tasks WHERE id = ?      行没了,找不回来
软删除    UPDATE tasks SET deleted_at = now()  行还在,只是被标记
```

软删除的查询约束统一收敛在 Repository 层,上层不用每次记得加:

```python
filters = [Task.user_id == user_id, Task.deleted_at.is_(None)]
```

为什么默认软删除:任务数据有审计、统计、误删恢复价值,物理删了就找不回来。代价是查询都要带 `deleted_at IS NULL`、且 `deleted_at` 要建索引——因为这是几乎所有查询的过滤条件。

软删除不是银弹:它让"已删除"数据持续占空间,涉及隐私合规(GDPR)时可能仍需物理删除或定期清理。是否软删除取决于业务对"可恢复性"和"合规"的权衡。

### 所有权:404 而不是 403

按 id 取任务时,把"不存在"和"不属于你"统一翻译成 404:

```python
task = task_repo.find_active_by_id(session, task_id)
if task is None or task.user_id != user_id:
    raise ResourceNotFoundError("任务不存在")
```

为什么不返回 403:403 会泄露"这个 id 在系统里存在、只是不属于你",攻击者据此能枚举出哪些任务 id 有效。统一 404 让"不存在"和"无权"在响应上无法区分,这是越权防护的常见做法。

所有按 id 的写操作(改/删/转状态)都先过 `get_owned_task`,确保不会出现"用户 A 改了用户 B 的任务"。

### 分页:total 是满足筛选条件的条数

```json
{ "items": [...], "total": 2, "page": 1, "page_size": 20 }
```

`total` 不是全表条数,而是"满足当前筛选条件(如 `status=pending`)的总条数"。否则前端按全表数算总页数,加了筛选后页数就错了。分页参数的边界(`page>=1`、`page_size` 在 1~100)交给 Query 的 `ge/le` 校验,超界直接 422,业务代码不参与。

## 面试高频问答

**Q:状态机为什么用一张转换表,而不是在业务代码里写 if/else?**

集中一处,规则只有一份实现。散落在各路由里会有多份"哪些状态能转哪些"的逻辑,改一处漏一处是必然的。用 `{当前状态: {动作: 新状态}}` 的映射表,合法路径一目了然,加状态只改这张表,转换校验是 `action in allowed` 一行。

**Q:状态转换失败该返回 409 还是 422?**

409。422 是"请求格式/参数本身不对"(如缺字段、类型错),409 是"请求格式没问题,但和当前资源状态冲突"。对已完成任务再 `complete`,请求本身合法,只是状态不允许——和"用户名已存在"同类,都是资源状态冲突。前端拿到 409 就知道该刷新本地状态重试,而不是当成表单校验错误。

**Q:软删除和物理删除怎么选?**

看业务对"可恢复性"和"合规"的权衡。任务、订单这类有审计/统计价值、可能误删的数据,默认软删除(打 `deleted_at`,查询过滤掉)。涉及隐私合规要求"彻底删除"的数据(如用户注销),仍需物理删除或定期清理。软删除的代价是查询都要带过滤、已删除数据占空间。

**Q:为什么按 id 操作资源时,不属于当前用户要返回 404 而不是 403?**

403 会泄露资源存在性:攻击者能区分"不存在"和"存在但无权",据此枚举有效 id。统一返回 404,让两种情况在响应上无法区分,是越权防护的常见做法。代价是真正无权时用户得不到"这是别人的"这种精确提示——但安全优先于提示友好度。

**Q:软删除后,查询怎么保证不漏带过滤?**

把 `deleted_at IS NULL` 收敛在 Repository 层,所有读取入口都带上。上层 Service / API 不直接写查询,自然不会漏。本项目 `list_active` / `find_active_by_id` 都在 Repository 内部加了过滤,Service 只调用这些函数。如果哪天有人绕过 Repository 直接 `session.get(Task, id)`,就会漏——这正是架构测试守"API/Service 不直接构造查询"的意义。

**Q:分页接口的 total 应该是全表数还是筛选后的数?**

筛选后的数。前端算总页数用 `ceil(total / page_size)`,如果 total 是全表数而 items 是筛选后的,页数就错了。total 必须和 items 用同一组 WHERE 条件 count 出来。

## 本项目实战

- `app/models/task.py:42` `deleted_at` — `datetime | None` + `index=True`,软删除时间戳,索引支撑"未删除"过滤
- `app/models/task.py:33` `status` — `VARCHAR(16)` + `index=True`,状态机当前节点,索引支撑按状态筛选
- `app/repositories/task.py:76` `list_active` — `user_id` + `deleted_at IS NULL` 过滤下推到 SQL,分页用 `offset/limit`,同时返回 `count`
- `app/repositories/task.py:107` `soft_delete` — 只 `UPDATE deleted_at`,不 `DELETE` 行
- `app/services/task.py:31` `_TRANSITIONS` — 状态机转换表,合法路径只有一份实现
- `app/services/task.py:42` `_EDITABLE_STATUSES` — 终态任务不可编辑正文,业务规则集中定义
- `app/services/task.py:87` `get_owned_task` — 所有权授权落地点,"不存在"和"不属于你"统一 404
- `app/services/task.py:142` `transition_task` — 查转换表,非法动作抛 `TaskStateError`(409)
- `app/core/exceptions.py:108` `TaskStateError` — `code=TASK_STATE_CONFLICT`、`status_code=409`
- `app/api/tasks.py:95` `POST /tasks/{task_id}/{action}` — 用动作名而非 PATCH status,防止绕过状态机
- `app/api/tasks.py:43` `GET /tasks` — 分页 + 状态筛选,`Query(ge=1)` / `Query(ge=1, le=100)` 兜底校验
- `app/schemas/task.py:60` `TaskPage` — 统一分页外壳 `{items, total, page, page_size}`
- `tests/test_tasks_api.py` — 越权 404、状态机合法/非法转换、终态不可编辑、软删除行仍在库、分页筛选

## 易错点

- **直接 PATCH status 字段做状态转换。** 客户端能传任意 status 绕过状态机,必须用动作 + 转换表。
- **非法转换返回 422。** 422 是格式错,状态冲突是 409,语义不同,前端处理逻辑也不同。
- **软删除后查询漏带 `deleted_at IS NULL`。** 把过滤收敛在 Repository,别让上层直接 `session.get`。
- **"不属于你"返回 403。** 泄露资源存在性,统一 404。
- **按 id 写操作不做所有权校验。** 改/删/转状态都要先过 `get_owned_task`,否则越权。
- **分页 total 用全表数。** 必须和 items 同条件 count,否则筛选后页数错。
- **分页参数不校验边界。** `page=0` 或 `page_size=10000` 要靠 Query 的 `ge/le` 拦下,别让业务代码处理。
- **以为软删除等于合规删除。** 隐私合规场景仍需物理删除或定期清理,软删除只是"对用户不可见"。
- **状态机转换表和实际业务规则脱节。** 改了业务允许的转换却忘了改表,规则就散了——所以表必须集中且唯一。

## 记忆口诀

```text
状态机用转换表,动作不是改字段
非法转换返 409,不是 422 别搞混
删除默认走软删,deleted_at 打时间戳
查询过滤收在 Repository,别让上层漏
按 id 操作先验主,不属于你返 404
分页 total 同条件,边界交给 Query 拦
```
