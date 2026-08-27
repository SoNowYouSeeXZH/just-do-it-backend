# 01 分层架构与依赖注入

> 对应迭代 1 · 阶段一

## 一句话总结

分层的本质是**让每种变化只影响一个地方**：换数据库只动 Repository，改业务规则只动 Service，调接口格式只动 Schema。判断分层有没有做到位，不看目录长得像不像，而看**依赖方向是否单向**、每层是否只 import 它该 import 的东西。

---

## 核心概念

### 五层职责

| 层 | 回答什么问题 | 允许依赖 | 禁止出现 |
|---|---|---|---|
| Router | 这个 URL 收什么、返什么 | Schema、Service | SQL、业务判断 |
| Schema | 输入合法吗、输出暴露哪些字段 | 无 | ORM 模型、业务逻辑 |
| Service | 业务上算成功还是失败 | Repository、core | `fastapi`、SQL |
| Repository | 数据怎么读写 | Model、sqlmodel | 业务判断、HTTPException |
| Model | 表长什么样 | 无 | 一切 |

依赖方向必须单向向下：`api → services → repositories → models`。反向依赖（Service import Router）就是循环耦合。

### 三条硬性边界

判断分层是否守住，只需 grep 三次：

```text
services/ 里出现 "from fastapi"        → 业务层被 HTTP 污染
services/ 里出现 "select(" 或 SQL      → 越过了数据访问层
api/ 里出现业务 if 判断                → 业务逻辑泄漏到接口层
```

### 依赖注入（DI）

DI 不是「框架特性」，是一种**把依赖的创建权交给外部**的设计。

```python
# 没有 DI：函数自己造依赖，无法替换
def list_users():
    session = Session(engine)   # 死死绑定生产数据库

# 有 DI：依赖从参数进来，谁调用谁决定
def list_users(session: SessionDep):
    ...
```

DI 的价值在测试时才真正体现——见笔记 05。

---

## 面试高频问答

**Q：为什么不能把逻辑都写在路由函数里？**

三个具体代价。第一，无法复用：定时任务想复用「注册用户」逻辑，就得伪造一个 HTTP 请求。第二，无法测试：想验证密码校验规则，必须启动 web 服务发请求。第三，改一处漏一处：同一条业务规则在多个路由里各有一份实现。

**Q：Service 层和 Repository 层的边界怎么划？**

一句话：**Repository 回答「数据在哪」，Service 回答「这样做对不对」**。

`find_by_username` 是 Repository——它只是取数据，查不到返回 `None`，不做评价。「查不到就算登录失败」是 Service 的判断。所以 Repository 从不抛业务异常。

**Q：为什么 Service 层不能 import fastapi？**

因为一旦 import 了，业务层就知道了 HTTP 状态码、请求响应这些概念，它就再也不是纯业务了。具体症状：想写一个批量导入用户的 CLI 脚本，调用 Service 却要处理 `HTTPException`，很荒谬。

我在这个项目里踩到过一个更隐蔽的版本：`services/auth.py` 原来抛 `HTTPException`，而它被 `services/user.py` 调用——HTTP 概念顺着调用链渗进了业务层。修法是把它拆成纯逻辑的 `decode_access_token()` 和 API 层的 `get_current_user_id()`。

**Q：这套分层是不是过度设计？小项目有必要吗？**

有成本，但成本主要是「多几个文件」，不是「多写逻辑」。真正的收益点在改需求时：这个项目 user 模块拆完后，`api/user.py` 从 117 行降到 61 行，`try/except` 归零——代码总量没增加多少，但每个文件都能一眼看懂。

反过来说，如果一个项目确定只有三个接口且永不变，直接写在路由里也没错。分层是为**演进**付的保险费。

**Q：什么是依赖注入，它解决什么问题？**

把「依赖从哪来」的决定权从函数内部移到调用方。解决的是**替换困难**：函数内部 `new` 出来的对象，外部没有任何办法换掉它。

FastAPI 的 `Depends` 是 DI 的实现，`dependency_overrides` 则是它最有说服力的应用——测试时一行代码就把真实 MySQL 换成 SQLite 内存库，业务代码不动。

---

## 本项目实战

### 重构前的样子

`api/user.py` 的两个路由函数里挤了五件事：SQL 查询、密码校验、token 签发、异常兜底、事务回滚。

### 重构后的落点

- `app/api/user.py:32` `user_login` — 只做「转交 → 装响应」，无业务判断
- `app/services/user.py:24` `authenticate` — 业务规则：什么算登录成功
- `app/repositories/user.py:17` `find_by_username` — 唯一写 SQL 的地方
- `app/core/security.py:19` `hash_password` — 通用安全能力，不属于任何业务
- `app/db.py:74` `SessionDep` — DI 的声明点，`Annotated[Session, Depends(get_session)]`

### 一个值得记住的细节

为什么密码哈希放 `core/security.py` 而不是 `services/user.py`？

因为它**不含业务规则**。「密码要哈希后存储」是通用安全实践，不是「用户模块的业务」。同理 JWT 签发也在这一层。判断标准：如果另一个完全不相关的模块也会用到它，它就属于 core。

---

## 易错点

**把 DTO 和 ORM 模型混用。** SQLModel 一个类能同时当表定义和校验模型，很方便，但也让人忘记它们是两件事。表结构加字段不该自动改变 API 响应。详见笔记 03。

**Repository 里做业务判断。** 常见写法是 `find_by_username` 查不到就抛异常。这样 Repository 就绑定了「查不到是错误」这个假设——但注册时「查不到」恰恰是正常情况。让它老实返回 `None`。

**分层了但依赖方向是乱的。** 有目录不代表有分层。真正要盯的是 import 语句：`services/` 里出现 `from fastapi` 就已经破了。

**为了分层而分层。** `repositories/` 里塞一堆只有一行 `session.get(Model, id)` 的函数没什么价值。分层的目的是隔离变化，不是凑齐目录。
