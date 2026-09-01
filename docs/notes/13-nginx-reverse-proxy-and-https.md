# 13 Nginx 反向代理与 HTTPS

> 对应迭代 7 · 已完成（自签名证书阶段）

## 一句话总结

反向代理是"客户端只认识它，它替客户端去找后端"的一层中间件。加它的价值是收口：只有它对公网开放，后端和数据库退回内网；TLS、压缩、限流、真实 IP 提取都在这一层统一处理，应用代码不需要关心。代价是链路多了一跳，真实 IP、协议、流式响应必须显式透传，否则会出现"本地好的、线上坏的"问题。

## 核心概念

### 正向代理 vs 反向代理

方向的判断标准是"代理替谁工作"：

```text
正向代理（forward proxy）
  客户端 → 代理 → 目标服务器
  代理替客户端工作，服务器不知道真实客户端是谁
  典型场景：翻墙、公司出网网关、爬虫 IP 池

反向代理（reverse proxy）
  客户端 → 代理 → 后端服务
  代理替服务器工作，客户端不知道后端有几台、在哪
  典型场景：Nginx、云 SLB、API 网关
```

同一个软件可以做两种角色，区别只在配置在链路的哪一端。

### 为什么要在 uvicorn 前面加一层

uvicorn 自己就能监听 80，为什么还要多一跳：

```text
1. 端口收口
   只有 Nginx 暴露公网，后端从 ports 改成 expose，
   绕过 Nginx 的限流/日志/TLS 就不可能了

2. TLS 卸载（TLS termination）
   证书只配在 Nginx 上，加解密由 C 实现的 Nginx 完成，
   后端收到的是明文 HTTP，应用代码不碰证书

3. 静态资源与压缩
   gzip、缓存头、静态文件伺服交给 Nginx，
   比让 Python 进程干这些划算得多

4. 横向扩展的前提
   要加第二个后端实例时，只改 Nginx 配置，
   客户端和应用代码都不用动

5. 慢客户端隔离
   Nginx 会先把请求完整收下再转给后端，
   后端 worker 不会被一个慢速上传占住
```

第 5 点在同步框架（如 gunicorn + 同步 worker）下尤其关键——worker 数量有限，一个慢客户端就等于占掉一个并发位。

### 反代之后，后端看到的世界变了

这是加反向代理后最常踩的坑。链路里多了一跳，TCP 对端不再是真实客户端：

```text
不加反代:  客户端 IP 183.222.207.181 → 后端看到 183.222.207.181
加了反代:  客户端 → Nginx(172.18.0.5) → 后端看到 172.18.0.5
```

后端日志、限流、风控如果依赖对端 IP，全部失效。所以必须显式透传：

```text
X-Real-IP          真实客户端 IP（单个值）
X-Forwarded-For    代理链，格式 客户端, 代理1, 代理2
X-Forwarded-Proto  原始协议 http / https
X-Forwarded-Host   原始 Host
```

`X-Forwarded-Proto` 容易被忽略但很重要：HTTPS 请求到了后端变成明文 HTTP，如果后端要生成回调地址、重定向 URL 或判断是否要求安全 Cookie，不看这个头就会错误地退化成 `http://`。

安全提醒：这些头**客户端可以伪造**。之所以能信，是因为链路上只有一层可信 Nginx，并且用的是 `proxy_set_header`（覆盖），客户端自己带的 `X-Real-IP` 会被直接顶掉。如果前面再加 CDN 或 SLB，取真实 IP 的规则要重新设计（通常配 `set_real_ip_from` 指定可信代理网段）。

### SSE / 流式响应与 proxy_buffering

Nginx 默认开启 `proxy_buffering`，会先把后端响应攒进 buffer，够了再转发给客户端。对普通 JSON 无影响，但对 SSE 是致命的：

```text
buffering on（默认）
  后端: chunk0 → chunk1 → chunk2  （每 400ms 产出一个）
  客户端: ....................... [chunk0 chunk1 chunk2] 一次性到达
  打字机效果消失，用户以为卡住了

buffering off
  客户端: chunk0(+0.00s) chunk1(+0.38s) chunk2(+0.79s)
  逐块到达，流式效果保留
```

两种关闭方式：

```text
后端返回 X-Accel-Buffering: no
  这是 Nginx 认的特殊响应头，后端可以按需关闭缓冲。
  优点是后端自己控制，缺点是依赖后端记得加。

Nginx 配 proxy_buffering off
  在代理层显式关闭，不依赖后端行为。
```

推荐同时在应用层和代理层关闭缓冲，形成双保险。

流式接口还有两个配套设置容易漏：`proxy_read_timeout` 要放大（默认 60s，大模型生成经常超过），以及 `proxy_http_version 1.1` + `proxy_set_header Connection ""`（默认会发 `Connection: close`，长连接会被立刻关掉）。

另外 gzip 的 `gzip_types` 里**不能**包含 `text/event-stream`——压缩本身会引入缓冲，等于把刚关掉的缓冲又打开了。

### HTTPS / TLS 要点

```text
TLS 握手做三件事
  1. 身份认证：证书由可信 CA 签发，证明"我确实是这个域名"
  2. 密钥协商：协商出本次会话的对称密钥
  3. 之后用对称加密传输数据（非对称太慢，只用于握手）
```

配置上的关键决策：

- **只留 TLS 1.2/1.3**。1.0/1.1 已被主流浏览器废弃且有已知弱点。
- **`ssl_prefer_server_ciphers off`**。现代做法是让客户端选它更擅长的套件（很多设备有 AES 硬件加速，某些没有则 ChaCha20 更快）。
- **会话复用**（`ssl_session_cache`）。握手是 TLS 里最贵的一步，缓存后重连能跳过完整握手。
- **HTTP/2 需要 TLS**，所以 `http2 on` 只写在 443 的 server 块里。

### 自签名证书 vs CA 签发

```text
自签名
  自己给自己签，加密强度完全一样
  但没有可信 CA 背书 → 浏览器/App 报"不安全"
  只适合学习、内网、本地开发

CA 签发（Let's Encrypt 免费）
  CA 验证你确实控制这个域名后签发
  浏览器信任 CA → 信任你的证书
  必须有域名，无法只用 IP 申请
```

国内服务器额外一道关：域名解析到国内节点的 80/443 必须先做 ICP 备案，否则机房会拦截，域名买了也用不了。

**HSTS 在自签名阶段绝对不能开**。`Strict-Transport-Security` 会让浏览器记住"以后只准用 HTTPS 访问这个站"，在证书不可信时下发，等于把自己锁死——浏览器会直接拒绝访问且用户无法点"继续"。换成正式证书后再加。

### 容器化下的 DNS 缓存坑（常见）

这是这一轮最值得记住的问题。开源版 Nginx 只在**配置加载时**解析一次 `upstream` 里的主机名，然后把 IP 永久缓存：

```text
启动时:  upstream backend:8000 → 解析成 172.18.0.4，缓存
重建容器: backend 实际 IP 变成 172.18.0.3
之后:    Nginx 还在连 172.18.0.4
         → 502 connect() failed (111: Connection refused)
         → 但 backend 自己日志一切正常
```

排查时极容易怀疑错方向——后端明明健康，接口却全是 502。

解法是声明 Docker 内置 DNS，并在 `proxy_pass` 里用变量，迫使 Nginx 每次请求重新解析：

```nginx
resolver 127.0.0.11 valid=10s ipv6=off;   # Docker 内置 DNS
set $backend_origin backend:8000;
proxy_pass http://$backend_origin$request_uri;   # 含变量 → 每次解析
```

三个细节：

- `127.0.0.11` 是 Docker 给每个自定义网络内置的 DNS 服务器地址（固定值）。
- `proxy_pass` 参数**一旦含变量，就不会自动带上原始 URI**，必须显式拼 `$request_uri`（它含 query string），否则路径丢失。
- 代价：带变量的 `proxy_pass` 无法配合 `upstream` 的 `keepalive`，到后端的连接不再复用。当前量级可忽略；要复用就得保留 upstream 并在每次重建后手动 `nginx -s reload`——多一步人工操作，漏掉就是一次线上事故。

商业版 Nginx Plus 的 `server ... resolve` 能同时兼顾，开源版没有这个指令。

### conf.d 的 include 机制

官方镜像的主配置里有 `include /etc/nginx/conf.d/*.conf;`，且这行位于 `http { }` 块内部。由此推出两条实践：

- 挂载**整个 conf.d 目录**而不是单个文件，可以顶掉镜像自带的 `default.conf`，避免两个 `default_server` 抢 80 端口。
- 被 server 块 include 的公共片段（公共配置片段）**不能用 `.conf` 后缀**。否则它们会被顶层 include 到 http 块，而 `location` 只能出现在 server 内，Nginx 直接启动失败。

### reload vs restart

```nginx
nginx -t          # 先校验语法，永远先做这步
nginx -s reload   # 平滑重载：老 worker 处理完手上请求再退出，不断连接
```

`reload` 不中断服务，`restart` 会。改完配置的正确顺序是 `-t` 校验 → `reload`。跳过 `-t` 直接 reload，配置有错时 Nginx 会拒绝加载新配置并继续用旧的（还算安全），但如果是 `restart` 就直接起不来了。

## 面试高频问答

**Q：为什么要用 Nginx 做反向代理，应用直接监听 80 不行吗？**

技术上可以，但会失去几层能力：端口收口（后端可以完全不暴露公网，绕过网关就不可能）、TLS 卸载（证书集中在一处，应用不碰加解密）、静态资源和 gzip 交给 C 实现的服务处理、慢客户端隔离（Nginx 先收完整请求再转发，后端 worker 不被占住）、以及横向扩展时只改代理配置。代价是链路多一跳，真实 IP、协议、流式响应需要显式透传。

**Q：加了反向代理后，后端拿到的客户端 IP 是什么？怎么拿真实 IP？**

后端看到的是 Nginx 的 IP，因为 TCP 对端变成了代理。真实 IP 要靠 Nginx 用 `proxy_set_header` 写入 `X-Real-IP` 和 `X-Forwarded-For`。关键点是这些头客户端可以伪造，所以必须用 `proxy_set_header`（覆盖）而不是追加，并且只有在链路上代理层可信时才能采信。多层代理（CDN + SLB + Nginx）时要配 `set_real_ip_from` 指定可信网段，从 XFF 里取正确的那一跳。

**Q：X-Forwarded-Proto 有什么用？不设会怎样？**

它告诉后端"原始请求是 http 还是 https"。TLS 在 Nginx 就终止了，后端收到的是明文 HTTP，如果不看这个头，后端生成重定向地址、OAuth 回调 URL 时会退化成 `http://`，或者错误地认为连接不安全而不下发 Secure Cookie。表现通常是"HTTPS 访问后被莫名跳回 HTTP"。

**Q：SSE / 流式接口经过 Nginx 后变成一次性返回，为什么？**

Nginx 默认 `proxy_buffering on`，会攒够 buffer 再转发。流式响应因此退化成一次性输出。解决办法是 `proxy_buffering off`，或后端返回 `X-Accel-Buffering: no`（Nginx 认这个头）。同时要放大 `proxy_read_timeout`（默认 60s 会中途断流）、设 `proxy_http_version 1.1` 并清空 `Connection` 头，且 gzip 的 `gzip_types` 不能包含 `text/event-stream`——压缩会重新引入缓冲。

**Q：Nginx 反代到容器化后端，重建后端容器后全是 502，为什么？**

开源版 Nginx 只在配置加载时解析 upstream 里的主机名并永久缓存 IP。容器重建后 IP 可能变化，Nginx 还在连旧 IP，于是 `connect() failed (111: Connection refused)`，而后端自身日志完全正常。解法是配 `resolver 127.0.0.11 valid=10s`（Docker 内置 DNS）并在 `proxy_pass` 里使用变量，强制每次请求重新解析；用变量时必须显式拼 `$request_uri`。代价是失去 upstream 的 keepalive 连接复用。

**Q：HTTPS 的证书能只用 IP 申请吗？**

Let's Encrypt 只签域名，不签 IP。商业 CA 有 IP 证书但成本高。只有 IP 时的选项是：自签名证书（加密强度相同，但浏览器不信任，适合学习和内网）、或者注册域名 + 备案后申请正式证书。切换时 Nginx 配置基本只改 `server_name` 和证书路径两行。

**Q：为什么自签名阶段不能开 HSTS？**

HSTS 让浏览器记住"这个域名以后只准用 HTTPS"，并且在证书不可信时**不给用户"继续访问"的选项**。自签名证书本来就不被信任，一旦下发 HSTS，浏览器会硬拒绝访问，等于把自己锁死，而且 max-age 期间无法撤回。必须等换成 CA 签发的证书后再启用。

**Q：`nginx -s reload` 和 `restart` 有什么区别？**

`reload` 是平滑重载：master 进程加载新配置，启动新 worker，老 worker 处理完手上的请求后退出，期间不断连接。`restart` 会中断服务。正确流程是 `nginx -t` 校验语法后再 `reload`；如果配置有错，reload 会被拒绝并继续使用旧配置，服务不受影响，而 restart 则会直接起不来。

**Q：安全组放行了端口，但访问不通，怎么排查？**

分层判断。云安全组在虚拟机网卡之前，决定流量能否进入主机；主机上还得有进程真正监听那个端口。用 `ss -tlnp` 看监听、`curl` 从外部看连通性。反过来也成立：把后端从 `ports` 改成 `expose` 后，即使安全组还放行着那个端口，公网也连不上，因为门后面已经没有服务了——这时应该顺手删掉过期的安全组规则，属于纵深防御。

## 线上验证结果

```text
http  /api/health  → 200 {"status":"ok","database":"ok"}
https /api/health  → 200，HTTP/2
http  /api/tasks   → 401 + WWW-Authenticate: Bearer（鉴权正常透传）
http  /api/jobs    → 200，gzip 后 1526 → 661 字节
/ 和 /docs         → 404（不转发未知路径）
公网 :8000 / :8080 → 连接被拒（已退回内网）
容器 IP 从 172.18.0.3 变为 172.18.0.6 后未 reload nginx → 仍然 200
```

## 易错点

- **忘记透传 X-Real-IP / X-Forwarded-For。** 后端日志里全是内网 IP，限流和风控失效。
- **无条件信任 X-Forwarded-For。** 客户端可伪造。必须用 `proxy_set_header` 覆盖，多层代理时配 `set_real_ip_from`。
- **忘记 X-Forwarded-Proto。** 后端生成的重定向和回调地址退化成 http，表现为"HTTPS 访问被跳回 HTTP"。
- **SSE 忘了关 proxy_buffering。** 流式退化成一次性返回，前端打字机效果消失。
- **把 text/event-stream 放进 gzip_types。** 压缩重新引入缓冲，等于白关了 buffering。
- **流式接口不放大 proxy_read_timeout。** 默认 60s，大模型生成中途断流。
- **容器化后端用 upstream 写死主机名。** 容器重建换 IP 后全站 502，且后端日志完全正常，极易误判方向。
- **变量式 proxy_pass 忘了拼 $request_uri。** 路径和 query string 丢失，所有接口 404。
- **自签名阶段开 HSTS。** 浏览器硬拒绝访问且无法点"继续"，max-age 期间不可撤回。
- **只挂载单个 conf 文件。** 镜像自带的 default.conf 还在，两个 default_server 抢 80。
- **公共 location 片段用 .conf 后缀。** 被顶层 include 到 http 块，Nginx 直接启动失败。
- **改配置直接 reload 不先 nginx -t。** 应当先校验；`restart` 时配置有错会直接起不来。
- **改完 compose 只 reload nginx。** 端口映射变化必须重建容器，reload 不生效。
- **后端改成 expose 后忘了删安全组旧规则。** 万一 ports 被误加回来，后端立刻重新裸奔且无提示。
- **只看容器 exit code 判断成功。** 应当验证最终状态（`ss -tlnp` 看监听、curl 看响应）。

## 记忆口诀

```text
反代收口，只让 Nginx 见公网
真实 IP 靠透传，且只信可信代理这一跳
X-Forwarded-Proto 不设，HTTPS 会退化成 HTTP
流式必须关 buffering，且不能压缩
容器化配 resolver，upstream 缓存 IP 是 502 的元凶
变量式 proxy_pass 记得拼 $request_uri
自签名不开 HSTS，会把自己锁死
先 nginx -t 再 reload，端口变化要重建
```
