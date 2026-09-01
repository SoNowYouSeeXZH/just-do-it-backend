# 04 密码存储与 JWT 鉴权

> 对应迭代 1 · 阶段二（认证部分）· 阶段十

## 一句话总结

密码用 bcrypt 这类**慢哈希**存储（不是 MD5/SHA），JWT 靠**签名**保证不可伪造但内容是明文可读的。两个必须记住的实践：密钥缺失时 fail closed（宁可 503 也不用默认密钥），登录失败不区分「用户不存在」和「密码错误」。

---

## 核心概念

### 为什么不能用 MD5/SHA256 存密码

不是因为它们「不安全」，而是因为它们**太快**。

```text
SHA256   现代 GPU 每秒能算数十亿次   → 彩虹表 + 暴力破解可行
bcrypt   故意设计成慢（可调 cost）    → 每次约 100ms，暴力破解不经济
```

bcrypt 还自带随机盐（salt），所以两个用户用同样的密码，存进库的哈希也不同——攻破一个不等于攻破另一个。

```text
$2b$12$LQv3c1yqBWVHxkd0LHAkCO...
 │   │  └─ 盐 + 哈希（盐存在哈希串里，不用单独建字段）
 │   └─ cost=12，即 2^12 次迭代
 └─ 算法标识
```

同类可选：argon2（更现代，抗 GPU 更好）、scrypt。三者都可以，别用 MD5/SHA。

### JWT 的结构

三段用 `.` 连接，每段是 base64url 编码：

```text
eyJhbGciOiJIUzI1NiJ9 . eyJzdWIiOiIxIiwiZXhwIjoxNzMwfQ . 4f2a8b...
     Header                    Payload                    Signature
   算法声明              用户身份 + 过期时间            签名，防篡改
```

关键认知：**Payload 是明文，任何人都能解开看**。base64 是编码不是加密。所以：

- 绝不能把密码、手机号、身份证放进 payload
- 能保证的是「内容没被改过」（改了签名就不匹配），不是「内容保密」

标准字段（claims）：`sub`（这个 token 代表谁）、`exp`（过期时间，解码时自动校验）、`iat`（签发时间）。

### 无状态的代价

服务端不存 session，验签就知道真伪——这是 JWT 的优点，也是它的问题：

**签发出去的 token 无法主动作废。** 用户改密码、账号被封，旧 token 在过期前依然有效。

常见对策：

- 短期 Access Token（15 分钟）+ 长期 Refresh Token（7 天），Refresh Token 存库可撤销
- 维护黑名单（但这就重新引入了状态，削弱了 JWT 的优势）
- payload 里放 `password_changed_at`，校验时和库里比对

如果 Access Token 有效期过长且没有 Refresh 机制，虽然实现简单，但安全性会下降。

### fail closed

配置缺失时的两种态度：

```python
# 危险：fail open
secret = settings.jwt_secret_key or "default-secret"
# 等于把签名密钥公开在代码里，任何人都能伪造 token

# 正确：fail closed
if not settings.jwt_secret_key:
    raise ConfigurationError("未配置 JWT_SECRET_KEY")
```

宁可服务报 503 让人立刻发现，也不能用一个人人皆知的默认密钥继续跑。安全相关的配置缺失，永远选择拒绝服务。

---

## 面试高频问答

**Q：JWT 和 Session 的区别，各自适合什么场景？**

```text
Session  服务端存状态，客户端只拿 sessionId
         优点：可以随时作废
         缺点：多实例部署要共享存储（Redis）

JWT      服务端不存状态，签名自证
         优点：天然支持水平扩展、跨服务
         缺点：签发后无法主动作废
```

选择依据：需要精细控制登录态（强制下线、单点登录）用 Session；微服务、多端、无状态扩展优先用 JWT。

**Q：JWT 存在 localStorage 还是 Cookie？**

各有风险。localStorage 容易被 XSS 偷（任何注入的 JS 都能读）；Cookie 配 `HttpOnly` 能防 XSS，但要额外防 CSRF。

推荐：`HttpOnly` + `Secure` + `SameSite=Lax` 的 Cookie，配合 CSRF token。移动端 App 不受 XSS 影响，用安全存储（Keychain / Keystore）即可。

**Q：为什么登录失败不告诉用户到底是账号不存在还是密码错？**

防用户名枚举。如果分开提示，攻击者可以拿一个字典批量试，先摸清系统里有哪些账号存在，再对这批真实账号做撞库——攻击成本大幅降低。

正确做法是无论「用户不存在」还是「密码错误」，都返回同一段提示文案，两种失败在响应上完全无法区分。

**Q：401 和 403 的区别？**

401 = 未认证，「你是谁我不知道」，客户端应该去登录。
403 = 已认证但无权限，「知道你是谁，但你不能干这个」，重新登录也没用。

401 按 HTTP 规范必须带 `WWW-Authenticate` 响应头。

**Q：token 过期了怎么处理，用户体验怎么保证？**

Access Token 短期（15 分钟）+ Refresh Token 长期（7 天）。Access 过期时，客户端用 Refresh Token 静默换一个新的，用户无感。Refresh Token 存数据库，可以撤销——这样既保留了 JWT 无状态的好处，又拿回了作废能力。

**Q：怎么防登录接口被暴力破解？**

多层：
- 限流：同 IP / 同账号每分钟最多 N 次
- 递增延迟：连续失败后逐次拉长响应时间
- 验证码：失败若干次后触发
- 账号锁定：需谨慎，会被用来恶意锁别人的账号

如果登录接口没有限流，就存在暴力破解风险，需要补充限流和异常防护。


---

## 易错点

**用 MD5/SHA 存密码。** 快哈希不适合存密码，这是最经典的错误。

**自己实现加盐。** bcrypt 已经把盐存在哈希串里了，再手动拼盐是多余且容易出错的。

**JWT payload 里放敏感信息。** payload 是明文，谁都能解开。

**密钥回退到默认值。** fail open，等于没有鉴权。

**忘了校验 `exp`。** PyJWT 的 `decode` 默认会校验，但如果显式传了 `options={"verify_exp": False}` 就跳过了——不要这么做。

**只做认证不做授权。** 这个项目 `/api/messages` 当前的真实问题：验证了「已登录」，但没验证「这些消息是不是你的」，任何登录用户都能读全部人的聊天记录。这是迭代 2 要修的。
