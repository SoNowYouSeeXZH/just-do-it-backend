# 05 测试基建与依赖覆盖

> 对应迭代 1 · 阶段八

## 一句话总结

用 **SQLite 内存库 + `dependency_overrides`** 替换真实数据库：测试跑得快、不污染开发数据、不需要先启 Docker，而且不 mock 数据库读写——只是换了个更轻的实现。这也是依赖注入价值最有说服力的证明：一行代码换掉外部依赖，业务代码不动。

---

## 核心概念

### 测试分层

```text
单元测试   单个函数，无外部依赖        最快，数量最多
集成测试   多层协作 + 真实数据库读写   通常作为主要验证手段
E2E 测试   完整链路，含前端            最慢，只覆盖关键路径
```

这个项目的 10 条用例都是集成测试：从 HTTP 请求进，经过 Router → Schema → Service → Repository → 数据库，再回到响应。因为要验证的恰恰是**分层之间的协作**是否正确。

### 为什么不 mock 数据库

mock 掉数据库，就测不到这些东西：

- 唯一约束是否真的生效（`test_register_duplicate_username_returns_409` 靠的就是真实约束）
- 事务回滚行为
- 字段长度限制
- SQL 语句本身写对没有

换 SQLite 而不是 mock，保留了「真的执行 SQL」这件事，只是换了个轻量引擎。

代价是 SQLite 和生产库（本项目是 PostgreSQL）有方言差异（JSON/JSONB 函数、upsert 语法、字段长度不强制等）。所以这套方案适合验证业务逻辑，不适合验证数据库特有行为——那类要用真实 PostgreSQL 跑。

### SQLite 内存库的两个必要参数

```python
engine = create_engine(
    "sqlite://",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
```

`StaticPool` 是关键。SQLite 内存库是**连接私有**的——换一个连接就是一个全新的空库。不用 StaticPool，会出现「刚建的表查不到」这种诡异现象。StaticPool 让所有连接复用同一个物理连接。

`check_same_thread=False` 是因为 TestClient 可能在别的线程执行请求。

### dependency_overrides

```python
def get_session_override() -> Session:
    return session                       # 返回 SQLite 会话

app.dependency_overrides[get_session] = get_session_override
```

生产代码里路由通过 `Depends(get_session)` 拿会话。测试时把这个依赖替换掉，业务代码一行都不用改。

这就是 DI 的实际价值。如果 Repository 内部是 `session = Session(engine)` 硬写的，就没有任何办法从外部替换。

用完必须 `app.dependency_overrides.clear()`，否则污染后续测试模块。

### fixture 的作用域

```python
@pytest.fixture(name="session")     # 默认 function 作用域
def session_fixture():
    ...
```

默认每个测试函数一套全新的库。这保证了**测试隔离**：用例之间不互相影响，执行顺序无关。

代价是慢一点。如果建表开销大，可以考虑 `scope="module"` 配合每个用例后清表，但要小心引入隐式依赖。

---

## 面试高频问答

**Q：测试该不该连真实数据库？**

看测什么。测业务逻辑用轻量替代（SQLite 内存库）足够，快且隔离好。测数据库特有行为（PostgreSQL 的 JSONB 函数、索引效果、锁行为、迁移脚本）必须连真实 PostgreSQL——通常在 CI 里用 Docker 起一个。

要避免的是 mock 掉数据库层：那样测试通过了但上线报错，因为 mock 的行为和真实数据库不一致。

**Q：怎么保证测试之间互不影响？**

三个层面。数据隔离：每个用例一套新库或事务回滚。状态隔离：全局状态（这里是 `dependency_overrides`）用完清理。顺序无关：任何用例单独跑都能通过。

判断方法很简单——随机化执行顺序（`pytest-randomly`），如果开始挂了，说明存在隐式依赖。

**Q：一个接口该测哪些场景？**

按四类想：

```text
正常路径    参数合法、业务允许
边界值      分页 page=0、limit 超上限、空列表
非法输入    缺字段、类型错、超长
权限        未登录、token 无效、越权访问别人的资源
```

这个项目 10 条用例的分布：正常 3 条、非法输入 1 条、权限相关 6 条。权限占比高，因为那是最容易出真实安全问题的地方。

**Q：测试覆盖率要追求多少？**

覆盖率是参考不是目标。80% 的覆盖率可能全是无意义的 getter 测试，而关键的权限判断分支一条没覆盖。

我更关注：核心业务路径是否覆盖、异常分支是否覆盖、权限校验是否覆盖。这三块比数字重要。

**Q：`test_register_stores_hashed_password` 为什么要直接查数据库？**

因为要验证的是「存进去的到底是什么」，而这个信息不会出现在响应里（响应里根本没有 `password_hash` 字段，这正是 DTO 白名单的效果）。所以只能绕过 API 直接查库。

```python
user = session.exec(select(Users).where(Users.username == _USERNAME)).first()
assert user.password_hash != _PASSWORD
assert user.password_hash.startswith("$2")     # bcrypt 哈希特征
```

这条用例守的是一个绝不能退化的安全底线：库里永远不能出现明文密码。


---

## 易错点

**忘记清理 `dependency_overrides`。** 污染后续测试，表现为「单独跑能过，一起跑就挂」。

**SQLite 不加 StaticPool。** 表建完却查不到，因为换了连接就是新的空库。

**在 import 之后设置环境变量。** 配置单例已经固化，设了不生效。

**测试之间共享数据。** 用例 A 建的用户被用例 B 依赖，一旦调整执行顺序就崩。

**只测正常路径。** 异常和权限分支才是真实 bug 和安全问题的高发区。

**为了让测试通过而改测试。** 测试挂了先问「是不是代码真有问题」。这次的 bcrypt 版本冲突就是真实的环境问题——如果当时把断言从 `startswith("$2")` 改宽松，就会掩盖掉真正的依赖不一致。
