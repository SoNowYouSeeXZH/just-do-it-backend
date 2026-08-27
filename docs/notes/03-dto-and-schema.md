# 03 DTO 与响应白名单

> 对应迭代 1 · 阶段七

## 一句话总结

API 响应必须用**独立声明的 DTO**，而不是直接返回 ORM 模型。原因是白名单默认安全：只有写进 DTO 的字段才可能出去；黑名单（`exclude={...}`）依赖开发者每次都记得排除，忘一次就泄露。

---

## 核心概念

### 三种写法的风险对比

```python
# 1. 最危险：直接返回 ORM 对象
return user
# 表里加任何字段（password_hash、last_login_ip、internal_note）
# 都会自动出现在响应里，没有任何提醒

# 2. 凑合：黑名单排除
return user.model_dump(exclude={"password_hash"})
# 依赖记忆。新增敏感字段时，所有 exclude 都要同步更新，
# 漏一个接口就泄露一个

# 3. 推荐：白名单 DTO
return UserPublic.model_validate(user, from_attributes=True)
# UserPublic 里只声明 id / username / created_at
# password_hash 根本没有出去的路径
```

安全设计的通用原则：**默认拒绝，显式放行**。白名单符合这个原则，黑名单不符合。

### 为什么 ORM 模型不适合当响应模型

它们服务于两个不同的契约：

```text
ORM 模型  →  和数据库的契约    随表结构演进
DTO       →  和前端的契约      必须保持稳定
```

用同一个类同时承担，等于把这两个契约绑死了。加一个内部用的数据库字段，就意外改变了 API 响应——前端可能因此崩溃，或者拿到本不该看到的数据。

SQLModel 允许一个类既是表又是校验模型，很方便，但便利掩盖了这个区别。

### 输入校验该放哪一层

放 Schema 层，不放业务层。

```python
class UserCredentials(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=6, max_length=128)
```

`max_length=64` 要和数据库列宽 `VARCHAR(64)` 对齐。不校验的话，超长字符串会一路走到 `INSERT` 才被数据库拒绝——用户看到 500 而不是 422，日志里还多一条无意义的错误。

原则：**尽早失败**。能在入口拦住的，不要放进业务逻辑。

---

## 面试高频问答

**Q：为什么不直接返回 ORM 模型？**

安全和稳定性两个原因。

安全上，`Users` 表里有 `password_hash`。直接返回就泄露了。用 `exclude` 排除也不可靠——那是黑名单思路，依赖每个接口都记得写。

稳定性上，数据库表结构和 API 契约演进节奏不同。加一个内部字段不该影响 API 响应，但直接返回 ORM 模型就会。

**Q：DTO 是不是增加了很多重复代码？**

有一定重复，但换来的是明确性。而且可以用继承减少：

```python
class UserPublic(BaseModel):     # 基础公开信息
    id: int
    username: str
    created_at: datetime

class LoginData(UserPublic):     # 登录时额外带 token
    access_token: str
    token_type: str = "bearer"
```

`LoginData` 复用了 `UserPublic` 的三个字段。见 `app/schemas/user.py:43`。

另外这个「重复」本身有价值——它是一道显式的边界。改表结构时不会意外改变 API，因为你得主动去改 DTO 才行。

**Q：请求校验和业务校验怎么区分？**

看这条规则是否需要查数据库或依赖业务状态。

```text
Schema 层：密码至少 6 位、邮箱格式、字段必填、数值范围
          → 纯格式判断，不查库，失败返回 422

Service 层：用户名是否已存在、任务状态是否允许流转、余额是否充足
          → 需要业务上下文，失败返回 4xx 业务错误
```

**Q：`from_attributes=True` 是干什么的？**

Pydantic 默认从 dict 读数据。`from_attributes=True`（Pydantic v1 里叫 `orm_mode`）让它从对象属性读，这样才能直接接受 ORM 实例。见 `app/api/user.py:61`。

**Q：统一响应外壳（`{code, message, data}`）有必要吗？**

有争议。好处是前端处理逻辑统一。坏处是丢掉了 HTTP 语义——如果 HTTP 状态码永远 200，监控和网关就看不出错误。

我的实践是两者结合：HTTP 状态码保持真实语义，响应体再带一层结构化信息。这个项目就是这么做的：409 冲突时 HTTP 状态码是 409，响应体里也有 `code: 409` 和 `error: "USERNAME_TAKEN"`。

---

## 本项目实战

- `app/schemas/user.py:21` `UserCredentials` — 登录注册共用请求体，含长度校验
- `app/schemas/user.py:35` `UserPublic` — 公开字段白名单，无 `password_hash`
- `app/schemas/user.py:43` `LoginData` — 继承 `UserPublic` 加 token
- `app/api/user.py:61` `UserPublic.model_validate(user, from_attributes=True)`
- `tests/test_user_api.py:39` 专门断言 `"password_hash" not in body["data"]`

### 已知待改进

`jobs.py` / `careers.py` / `industries.py` 等只读接口仍直接把 SQLModel 表模型当 `response_model`。这些表目前没有敏感字段，所以还不算漏洞，但同样的隐患在：哪天给 `Question` 表加一个 `internal_difficulty_score` 之类的内部字段，它会自动出现在给前端的响应里。

---

## 易错点

**在 DTO 里放 ORM 关系字段。** 触发意外的懒加载查询，可能变成 N+1。

**校验规则和数据库列宽不一致。** Schema 允许 100 字符但列宽 64，超长请求会在 `INSERT` 时报 500。要对齐。

**DTO 层写业务逻辑。** Pydantic 的 `@validator` 里查数据库——那是 Service 的活。DTO 只做无状态的格式判断。

**响应模型和返回类型注解不一致。** FastAPI 以 `response_model` 为准做序列化，注解只给类型检查器看。两者不一致时，实际行为跟着 `response_model`，容易误判。
