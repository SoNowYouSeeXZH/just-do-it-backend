# proxy_headers.inc —— 转发给后端时要设置的请求头
#
# 为什么必须显式设置这些:
# 加了反向代理之后,后端看到的 TCP 对端变成了 Nginx 容器的内网 IP。
# 如果不透传下面这些头,应用层就永远只能看到 172.x.x.x,
# 日志里的来源 IP、限流、风控全都失效。
#
# 安全提醒:这些头是"客户端可伪造"的。之所以能信,是因为链路上
# 只有一层可信的 Nginx,并且这里用的是 proxy_set_header(覆盖),
# 而不是 add 或 append——客户端自己带的 X-Real-IP 会被直接顶掉。
# 如果以后前面再加一层 CDN/SLB,取真实 IP 的规则要重新设计。

proxy_set_header Host              $host;
proxy_set_header X-Real-IP         $remote_addr;
proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
# 后端据此判断"原始请求是 http 还是 https",生成回调地址时才不会退化成 http
proxy_set_header X-Forwarded-Proto $scheme;
proxy_set_header X-Forwarded-Host  $host;

# 把 Nginx 生成的 request_id 传给后端。
# app/core/middleware.py 里的 _request_id() 会校验它符合
# ^[A-Za-z0-9._-]{1,128}$ 后原样使用,于是 Nginx 日志的 request_id
# 和应用日志、错误响应体里的 request_id 是同一个值。
proxy_set_header X-Request-ID      $request_id;
