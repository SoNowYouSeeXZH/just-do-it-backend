# locations.inc —— 被 80 / 443 两个 server 块共同 include 的路由规则
#
# 抽出来的原因:HTTP 和 HTTPS 的转发逻辑完全一致,只有监听端口和 TLS 配置不同。
# 写两份必然会在改动时漏掉一处。
#
# 注意扩展名用 .inc 而不是 .conf:conf.d 目录下所有 *.conf 都会被主配置
# 自动 include 到 http 块,而本文件里全是 location(只能出现在 server 内),
# 如果叫 .conf 会被顶层 include 一次,Nginx 直接启动失败。

# 后端地址放进变量,而不是写在 upstream 里。
# proxy_pass 的参数一旦含变量,Nginx 就会在"每次请求"时走 resolver 重新解析域名,
# 而不是启动时解析一次并永久缓存 IP。容器重建换 IP 后不会再 502。
# 详见 justdoit.conf 里关于去掉 upstream 的说明。
set $backend_origin backend:8000;

# ===== 健康检查 =====
# 单独拆出来是为了不写 access_log:云监控可能每几秒探一次,
# 这些日志会把真实请求淹掉。
location = /api/health {
    access_log off;
    # 带变量时必须显式拼上 $request_uri(含 query string),
    # 否则转发过去的路径会丢失。
    proxy_pass http://$backend_origin$request_uri;
    include /etc/nginx/conf.d/proxy_headers.inc;
}

# ===== SSE 流式对话 =====
# 这是整份配置里最容易踩坑的地方。默认 Nginx 会缓冲后端响应
# (proxy_buffering on),攒够一个 buffer 才转发给客户端。
# 对普通 JSON 无所谓,但 SSE 是"边生成边推",一缓冲就退化成一次性返回,
# 前端打字机效果直接失效。
#
# 后端已经在 app/api/chat.py 里返回了 X-Accel-Buffering: no 来关缓冲,
# 但那依赖后端记得加这个头;这里显式再关一次,双保险。
location = /api/chat {
    proxy_pass http://$backend_origin$request_uri;
    include /etc/nginx/conf.d/proxy_headers.inc;

    proxy_buffering off;         # 关键:不缓冲,后端产出多少立刻转发多少
    proxy_cache off;
    proxy_read_timeout 300s;     # 大模型生成慢,默认 60s 会中途断流
    proxy_send_timeout 300s;
    chunked_transfer_encoding on;
    # SSE 走 HTTP/1.1 长连接,必须清掉 Connection 头
    # (默认会发 Connection: close,连接被立刻关掉)。
    proxy_http_version 1.1;
    proxy_set_header Connection "";
}

# ===== 其余业务接口 =====
location /api/ {
    proxy_pass http://$backend_origin$request_uri;
    include /etc/nginx/conf.d/proxy_headers.inc;

    proxy_connect_timeout 5s;    # 连不上后端就快速失败,别把连接堆在这
    proxy_read_timeout 60s;
    proxy_send_timeout 60s;
}

# ===== 其他路径 =====
# 不做默认转发。/docs、/openapi.json 在生产已经关闭(见 app/main.py),
# 根路径也没有前端静态资源要伺服。直接 404,避免把未知路径全丢给后端。
location / {
    return 404;
}
