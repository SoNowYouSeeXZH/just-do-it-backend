# 06 认证与授权：资源所有权

> 对应迭代 2 · 已完成

## 一句话总结

认证只回答「你是谁」，授权回答「你能访问什么」。后端必须把当前用户身份一路传到 Service 和 Repository，并在数据库查询条件中加入资源所有者过滤；只检查 JWT 有效而不检查资源归属，就是典型越权漏洞。

## 核心概念

### Authentication vs Authorization

```text
认证 Authentication
  Authorization: Bearer <token>
  ↓ 验签、解析 sub
  当前用户 user_id = 1

授权 Authorization
  SELECT ... FROM chat_messages
  WHERE user_id = 1
```

JWT 的 `sub` 只能证明请求代表用户 1，不能证明用户 1 有权访问数据库中的任意记录。

### 资源所有权

资源表必须保存 owner：

```text
chat_messages
├── id
├── user_id  ← 所有者
├── role
└── content
```

查询资源时，必须同时带上资源 id 和当前用户 id：

```python
select(ChatMessage).where(
    ChatMessage.user_id == current_user_id
)
```

**授权条件必须下推到 SQL 层。** 先把所有消息查出来，再在 Python 里过滤，会把不属于当前用户的数据先加载进进程，容易泄露，也浪费数据库和内存。

### 外键 + 索引

```python
user_id: int = Field(foreign_key="users.id", index=True)
```

外键保证每条消息的 owner 真实存在；索引让按 owner 查询高效。两者职责不同：外键保证正确性，索引保证性能。

### 创建和读取必须同时绑定 owner

只给查询加过滤不够。如果写入时没保存 `user_id`，之后无法知道这条数据属于谁。

```text
创建：当前用户 user_id → INSERT chat_messages.user_id
读取：当前用户 user_id → WHERE chat_messages.user_id = user_id
```

这是一条贯穿写入和读取的业务不变量：**每条消息都有且只有一个所有者，用户只能读自己的消息。**

## 面试高频问答

**Q：认证和授权有什么区别？**

认证是身份确认，回答「你是谁」；授权是权限判断，回答「你能做什么」。JWT 验签成功只完成认证，不能自动完成授权。访问某个资源时还要检查当前用户是否拥有该资源，或者是否具备访问它的角色。

**Q：为什么不能只在 API 层判断权限？**

可以做入口校验，但不能只依赖入口。数据访问层的查询必须也带 owner 条件，形成最后一道约束。否则未来新增一个调用入口（后台任务、CLI、另一个 API）时，绕过原来的 API 检查就会越权。更可靠的是让 Service 明确接收 `user_id`，Repository 提供 `list_recent(session, user_id, limit)`，调用者无法无意中查全表。

**Q：401 和 403 的区别？**

401 是未认证：没有有效身份，应该登录；403 是已认证但无权访问，重新登录也不能解决。比如缺 token 返回 401，用户 A 请求用户 B 的资源返回 403，或者在“只返回自己的列表”这种设计里直接不返回 B 的数据。

**Q：为什么不把 user_id 放在请求体里？**

因为客户端是不可信的。客户端可以把 `user_id` 改成别人的值。所有权必须从服务端已经验证过的身份（JWT `sub`）获得，而不是从请求体或 URL 直接相信调用方传来的 user_id。

**Q：历史数据新增 NOT NULL 外键怎么办？**

不能直接加。旧记录没有 owner，直接改成 NOT NULL 会失败；更不能随便把历史数据归给某个用户。正确流程是先明确历史数据归属策略，再备份，先加 nullable 字段，回填合法用户，校验无 NULL，最后改为 NOT NULL 并建立外键。生产迁移必须使用 Alembic 或经过审核的 SQL，不依赖 `create_all()`。

## 易错点

- **只验 token，不验资源归属。** 这是本阶段修复的原始漏洞。
- **从请求体读取 owner_id。** 攻击者可以改成别人的 id，必须使用服务端解析的 JWT 身份。
- **只在 Python 里过滤。** 必须在 SQL `WHERE` 中过滤。
- **只给读取加 owner，不给创建加 owner。** 没有绑定关系，读取授权永远无法可靠实现。
- **直接把新增字段设为 NOT NULL。** 先处理历史数据，再收紧约束。
- **误以为 `create_all()` 会修改已有表。** 它通常只创建不存在的表，不负责安全的结构迁移。
- **消息业务降级时忽略 owner。** 即使聊天记录写入失败不阻塞对话，成功写入的记录仍必须带正确 owner。

## 记忆口诀

```text
认证看 Token，确认你是谁
授权看资源，确认你能看谁
owner 从服务端身份来
过滤必须下推到 SQL
写入读取都要绑 owner
```
