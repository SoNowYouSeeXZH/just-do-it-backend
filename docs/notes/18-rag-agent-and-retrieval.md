# 18 垂直领域 RAG Agent：手写 ReAct 循环与可溯源引用

> 对应 Track B（2026-09）。这一篇是「怎么对外讲这个模块」的底稿，不是使用文档。

## 一句话总结

把「问一句游戏攻略」做成一条能讲清取舍的工程链路：**要不要检索由模型自己决定**（不写硬编码路由）、**检索循环有界**（防死循环烧钱）、**引用编号由服务端确定性组装**（让幻觉没有落脚点）、**工具失败降级成 observation**（不变成 5xx）。四条里每一条都对应一个真实会出问题的地方，不是设计得漂亮。

---

## 核心概念

### 六层分层里 RAG 模块的位置

```text
api/chat.py        HTTP 契约 + SSE 序列化
    ↓
services/chat.py   按 rag_enabled 分流（回滚开关 + A/B 入口）
    ↓
services/rag/
  agent.py            ReAct 循环编排 + 终答综合 + 来源组装
  tools.py            OpenAI tools schema ←→ Python 执行器 一一对应
  search_provider.py  SearchProvider 协议 + 缓存入口 + DDG 实现
  wiki_provider.py    MediaWiki 实现（默认）
  page_fetcher.py     URL 抓取 + 四道 SSRF 防线
    ↓
core/cache.py      cache_aside_async（检索结果缓存，fail-open）
```

`services/rag/` 受分层守卫约束：**不许 import fastapi**。检索能力应该能被 CLI 脚本、定时任务直接复用，而不是只能从 HTTP 请求进来。守卫是递归收集的，子包和 `__init__.py` 都在范围内。

### 一次攻略问答的数据流

```text
用户提问
  │
  ├─ 第一段：检索循环（非流式、最多 rag_max_iterations 轮）
  │    ① messages + tools → 模型
  │    ② 模型返回 tool_calls？
  │         否 → break（它判定不需要检索，或信息已够）
  │         是 → 发 StageEvent{"retrieving"} 给前端
  │              执行工具 → 结果作为 role="tool" 消息追加
  │              顺便把 (标题, URL) 收进 _SourceCollector
  │              回到 ①
  │    ③ 跑满轮次没停 → 标记 hit_limit（正常出口，不是异常）
  │
  ├─ 服务端确定性组装来源编号 [1][2]…（抓过正文的排前面）
  │
  └─ 第二段：综合生成（流式）
       system prompt 注入「检索到的资料 + 编号来源」，要求只依据资料作答
       delta 逐段吐出 → 末尾拼接代码生成的「来源」小节
```

### 为什么是两段式，而不是一个流式循环

最自然的写法是「边循环边流式输出」，但做不到：

1. **function calling 那一轮必须拿到完整 `tool_calls` 才能执行工具**，而流式响应是一小段一小段来的，拼装 tool_calls 增量既繁琐又容易出错。
2. 更要紧的是**检索过程中模型的中间「思考」不该直接喷给用户**。用户想看结论，不想看「我先搜一下…嗯这个不对…再搜一下」。

代价说清楚：闲聊问题也会多一次轻量调用（第一段模型不调工具直接给文本，第二段还要再生成一遍）。换来的是**管线统一**——不用为「要不要检索」写两条互不相干的代码路径。

### 路由不写死，让它从模型行为里涌现

没有「判断这是不是游戏问题」的分类器，也没有关键词表。工具描述里写清楚什么时候该用，剩下交给 `tool_choice: "auto"`：

- 模型发起 tool_calls → 等于它判定「需要查」
- 模型直接给文本 → 等于它判定「不用查」

实测「你好，你是谁」零检索、6.9 秒直接作答；「原神里纳塔是什么地方」自动进检索。**少一个需要维护的分类器，且不会出现「分类器判错导致该查的没查」这类难排查的问题。**

### 引用溯源：让幻觉没有落脚点

这是整个模块最值得讲的一处设计。

**如果让模型自己输出来源列表，它会编造 URL** —— 这是 LLM 最稳定的失败模式之一。所以边界画成：

- 来源编号列表由 `agent.py` 从「实际被 `fetch_page` 抓过的 URL + search 返回的候选」**确定性组装**
- 模型只负责在正文里引用 `[1]` `[2]`
- 「来源」小节由代码拼接在回答末尾，**模型碰不到这段文本**

排序也有讲法：抓过正文的排在前面。因为它的内容真的进了上下文，对答案的贡献是实的；只出现在搜索结果里的页面只贡献了一句摘要。

有一条单测专门守这件事：让假模型在正文里编一个 `https://fake.example.com/我编的页面`，断言真来源小节里只有真 URL。

### SearchProvider 抽象：被现实兑现的那一次

一期本来只打算实现 `DDGProvider`（`ddgs` 包，免注册无 key）。实测后换掉了主实现，理由值得完整讲，因为它比「我设计了一个抽象层」有说服力：

- DDG 连续 5 次搜索只成功 2 次，头一两次 1.7~6.7s 正常，之后被限流且**不恢复**
- 把多个后端写在一个列表里反而更糟：`ddgs` 内部用 `return_when=FIRST_EXCEPTION` 并发批量跑，**一个秒失败的后端会把另一个正在返回结果的后端一起带走**——多后端不是冗余，是互相拖累。这个坑不看源码发现不了
- 用户明确不接受注册第三方搜索服务，Tavily / 博查那条路排除

换成 `MediaWikiProvider`（biligame 游戏 wiki）：攻略天然沉淀在 wiki 上，而 wiki 给的是**正规 API 而不是被爬的网页**——结构化、页面 URL 稳定（利于溯源）、免注册。

**换的时候 `agent.py` 和 `tools.py` 一行没动**，只改 `settings.search_provider`。这就是 Protocol 的价值兑现点：抽象不是提前设计出来的，是被现实逼出来后正好接住了。

### SSRF：因为 URL 参数来自模型输出

`fetch_page` 的 URL 最终由模型给出，而模型输出受用户输入影响。不校验就抓，用户可以诱导服务器去读 `http://169.254.169.254/latest/meta-data/`（云元数据服务，能拿到实例临时凭证）或 `http://127.0.0.1:6379`（本机 Redis）。四道防线：

1. **协议白名单**：只允许 http/https，挡掉 `file://`（读本地文件）、`gopher://`（历史上被用来打 Redis）
2. **目标 IP 校验**：禁私网/回环/链路本地/保留地址。关键细节——必须检查 `getaddrinfo` 返回的**全部** IP，一个域名可以同时解析出公网 IP 和 `127.0.0.1`，只看第一条就会放过去
3. **重定向逐跳校验**：所以**不能用 `follow_redirects=True`**。公网域名 302 到 `127.0.0.1` 是最经典的绕过手法
4. **内容类型 + 体积（2MB）+ 超时（10s）上限**

残留风险主动记录：DNS rebinding（检查时与使用时解析结果不同）没处理，彻底解决要接管连接、把已校验 IP 直接传给 socket。

---

## 面试高频问答

**Q：你这个 RAG 和「向量库 + top-k 检索」的常规 RAG 有什么区别？**

我做的是 **Agentic RAG**，检索不是一次性的：模型先决定要不要检索、搜什么词，看到搜索结果后再决定要不要抓某一页的正文，不满意可以换词再搜，最多 4 轮。常规 RAG 是「一次 embedding → 一次 top-k → 拼进 prompt」，问题问得不精确就只能拿到不相关的片段。代价是延迟更高、调用次数更多，所以我把轮数设成了配置项而不是写死。

**Q：怎么防止死循环烧钱？**

`for iteration in range(1, max_iterations + 1)` 加 **for-else**。跑满没 break 就走 else 分支，标记 `hit_limit=True` 并写 info 日志。关键点是：**达到上限不是异常**，而是一个正常出口——它会传给综合阶段，让终答里主动说明「资料可能不完整」。上限做成 `rag_max_iterations` 配置，线上出问题可以直接调成 1 退化成单轮检索。

**Q：怎么保证不出现「一本正经地编造来源链接」？**

把这件事从模型手里拿走。来源列表由服务端从「实际抓过的 URL + 搜索返回的候选」确定性组装，模型只能引用 `[1]` `[2]` 这样的编号；「来源」小节是代码字符串拼接的，不经过模型。综合阶段的 prompt 里还专门写了「不要自己写来源小节，系统会自动附上」。有一条单测让假模型在正文里编一个假 URL，断言来源小节里只有真 URL。

**Q：为什么是两段式？直接一个流式循环不行吗？**

两个原因。技术上，function calling 那一轮必须拿到完整 `tool_calls` 才能执行工具，流式下要自己拼装增量，容易错。产品上，检索过程中模型的中间思考不该喷给用户。代价是闲聊问题也多一次轻量调用，换来的是**一条统一管线**，不用为「要不要检索」维护两条代码路径。

**Q：工具执行失败了怎么办？**

`tools.execute()` 刻意**永不抛异常**，失败被转成一段模型读得懂的 observation 交回去，模型可以换关键词重试或换来源。搜索超时、页面 404、被 WAF 拦是外部世界的常态，让它冒成 500 的话用户看到「服务异常」，而模型其实完全有能力换个思路——这是 Agent 相比固定调用链的真实优势。

配套一个细节：兜底 `except Exception` 只把 `type(exc).__name__` 放进 observation，不放异常消息。因为 observation 会进上下文、可能被模型复述给用户，而未预期异常的消息里可能带内部路径或 SQL 片段。

**Q：Agent 里为什么不直接产出 SSE？**

传输协议属于接口层。`answer_stream` 产出的是 `StageEvent` / `DeltaEvent` 这样的结构化事件，SSE 组帧留在 `services/chat.py`。这样换 WebSocket 或者被定时任务复用时，Agent 一行不用改。分层守卫也强制了这件事：`services/rag/` 不许 import fastapi。

**Q：怎么测一个「行为不确定」的 Agent？**

用**脚本化的假模型**：预先排好每一轮返回什么（第一轮返回 tool_calls、第二轮返回纯文本），断言循环的编排行为——轮数、messages 里 `tool_call_id` 的配对、来源排序、hit_limit 是否置位。外部 HTTP 用 `httpx.MockTransport`，Redis 用 fakeredis。测的是**编排逻辑**，不是模型输出质量；模型质量靠真实链路手动验证。

---

## 实战方法（核心代码）

### 1. ReAct 循环：有界 + for-else 出口

`app/services/rag/agent.py`

```python
for iteration in range(1, tools.max_iterations() + 1):
    message = await llm.complete(messages, tools=tools.tool_schemas())
    tool_calls = getattr(message, "tool_calls", None)

    if not tool_calls:
        # 模型不再调工具 = 它认为信息够了(或压根不需要检索)。
        # 它这一轮的文本不直接返回给用户——终答统一由综合阶段生成,
        # 保证"有检索"和"没检索"两条路径输出风格一致。
        break

    # assistant 这条消息必须原样入列(带 tool_calls),否则下面的
    # role="tool" 结果会因为找不到对应 call_id 被上游拒绝。
    messages.append(_assistant_message_dict(message))

    for call in tool_calls:
        arguments = tools.parse_arguments(call.function.arguments)
        yield StageEvent(stage="retrieving",
                         detail=_stage_detail(call.function.name, arguments))

        result = await tools.execute(call.function.name, call.function.arguments)
        retrieval.used_tools = True
        retrieval.observations.append(result.text)     # 给模型看的文本
        for title, url in result.candidates:           # 给代码用的结构
            collector.add_candidate(title, url)
        if result.fetched_url:
            collector.mark_fetched(result.fetched_url)

        messages.append({"role": "tool", "tool_call_id": call.id,
                         "content": result.text})
else:
    # for-else:range 跑完都没 break,说明模型一直在调工具。
    # 这是"循环失控"的正常出口,不是异常。
    retrieval.hit_limit = True
    logger.info("检索循环达到迭代上限 %d,question=%s", iteration, question)
```

要讲的三个点：`tool_call_id` 严格配对、observation 与结构化来源分离、for-else 当上限出口。

### 2. 引用编号：抓过的排前面

```python
class _SourceCollector:
    def mark_fetched(self, url: str) -> None:
        # 模型可能抓一个没在搜索结果里出现过的 URL(比如它自己拼的)。
        # 仍然记进来:它的内容确实进了上下文,溯源就该体现出来。
        if url not in self._titles:
            self._titles[url] = url
            self._order.append(url)
        self._fetched.add(url)

    def build(self) -> list[Source]:
        fetched = [u for u in self._order if u in self._fetched]
        rest = [u for u in self._order if u not in self._fetched]
        return [Source(index=i, title=self._titles[url], url=url)
                for i, url in enumerate((fetched + rest)[:limit], start=1)]


def _sources_section(sources: list[Source]) -> str:
    """确定性拼装的来源小节。模型碰不到这段文本,所以编不了 URL。"""
    if not sources:
        return ""
    lines = ["\n\n---\n来源："]
    lines.extend(f"[{s.index}] {s.title}\n{s.url}" for s in sources)
    return "\n".join(lines)
```

### 3. 工具执行：永不抛异常

`app/services/rag/tools.py`

```python
async def execute(name: str, raw_arguments: str | None) -> ToolResult:
    executor = _executor(name)
    if executor is None:
        # 模型幻觉出一个不存在的工具名是真实会发生的。告诉它有哪些可用,
        # 它下一轮通常就能改对。
        return ToolResult(text=f"没有名为 {name} 的工具。可用工具:{'、'.join(TOOLS)}。")

    try:
        result = await executor(parse_arguments(raw_arguments))
    except (search_provider.SearchError, page_fetcher.FetchError) as exc:
        # 已知外部失败:原文交给模型(消息已脱敏,不含 URL 和堆栈)
        return ToolResult(text=f"工具 {name} 执行失败:{exc}。可以换个关键词或换一个来源再试。")
    except Exception as exc:  # noqa: BLE001
        # 只给类型名:observation 会进上下文、可能被模型复述给用户,
        # 而未预期异常的消息里可能有内部路径、SQL 片段。
        logger.error("工具 %s 出现未预期异常", name, exc_info=True)
        return ToolResult(text=f"工具 {name} 出现内部错误({type(exc).__name__}),请换一种方式。")

    if len(result.text) > _MAX_OBSERVATION_CHARS:   # 8000
        result.text = result.text[:_MAX_OBSERVATION_CHARS] + "\n...(内容过长已截断)"
    return result
```

`SearchError` / `FetchError` 刻意**不继承 `AppError`** —— 它们不该变成 5xx。

### 4. SSRF：手动逐跳跟随重定向

`app/services/rag/page_fetcher.py`

```python
def _resolve_and_check(host: str) -> None:
    """解析域名并校验**所有**返回的 IP。
    一个域名可以同时解析出公网 IP 和 127.0.0.1,只查第一条就可能放过去。
    """
    for info in socket.getaddrinfo(host, None):
        parsed = ipaddress.ip_address(info[4][0])
        if _is_blocked_ip(parsed):
            raise FetchError("目标地址属于内网或保留地址段,已拒绝")


def _is_blocked_ip(ip) -> bool:
    # is_private 不覆盖 loopback / link_local,必须显式加:
    # 169.254.169.254 是云元数据服务,能读实例临时凭证,危害最大
    return (ip.is_private or ip.is_loopback or ip.is_link_local
            or ip.is_reserved or ip.is_multicast or ip.is_unspecified)


async def fetch_page(url: str, *, max_chars=None) -> str:
    current = _validate_url(url)
    async with httpx.AsyncClient(follow_redirects=False, ...) as client:
        for _ in range(_MAX_REDIRECTS + 1):          # 3
            response = await client.get(current)
            if response.is_redirect:
                location = response.headers.get("location")
                # 相对跳转要先拼成绝对地址再校验
                current = _validate_url(urljoin(current, location))
                continue
            ...  # content-type 白名单 → 2MB 截断 → extract_text
```

`follow_redirects=False` 是这段的重点：自动跟随时中间跳转不经过校验，公网域名 302 到 `127.0.0.1` 就直接打进内网。

### 5. 单进程限速：monotonic + 惰性 Lock

`app/services/rag/wiki_provider.py`。连续两次请求会被 biligame 返回 HTTP 567。

```python
_last_request_at = 0.0
_throttle_lock: asyncio.Lock | None = None   # 惰性创建:模块导入时可能还没有事件循环

async def _throttle() -> None:
    global _last_request_at
    async with _get_lock():
        wait = settings.wiki_min_interval_seconds - (time.monotonic() - _last_request_at)
        if wait > 0:
            await asyncio.sleep(wait)
        _last_request_at = time.monotonic()
```

用 `time.monotonic()` 不用 `time.time()`：后者会被 NTP 校时回拨，算出负间隔或者卡很久。局限要主动说：**这是单进程的**，多 worker 或多实例下要挪到 Redis 才算真限速。

---

## 易错点

1. **`tool_calls` 和 `role="tool"` 必须严格配对。** assistant 那条带 `tool_calls` 的消息漏了没入列，或者一个 `tool_call_id` 少了对应的 tool 消息，上游会直接 400。而且报错信息通常很含糊。
2. **不要让模型输出 URL。** 它会编，而且编得很像真的。凡是需要可信的结构化信息，都从服务端已知的数据里组装，只让模型引用索引。
3. **检索关键词不要带游戏名。** 实测搜「原神 纳塔」返回的是一个 OST 页面——wiki 站本身已限定游戏，MediaWiki 搜索对多词偏 AND 语义，词越多越容易命中错的或者 0 命中。这类 prompt 规则只能靠实测发现。
4. **不要跨站凑结果数。** 一开始为了凑满 5 条会继续搜下一个游戏的 wiki，结果来源列表里混进了另一个游戏的页面。改成**命中即停**。
5. **多后端不等于冗余。** `ddgs` 内部 `return_when=FIRST_EXCEPTION` 并发跑各后端，一个秒失败的后端会把正在返回结果的那个一起带走 —— 后端列表越长越不稳。这个坑不读源码发现不了。
6. **抽正文必须先删 script/style 的内容，再删标签。** 反过来的话 JS 代码会变成正文里的乱码。
7. **整页 HTML 抽出来的前几百字基本都是导航栏。** 要先收窄到 `mw-parser-output` / `<article>` / `<main>`。但**不要用「哪个 div 文字最多」这类启发式**——猜错会静默丢掉正文，比多留点噪声严重得多。
8. **异步 cache-aside 得单独写一个。** 同步版的 `cache_aside(key, loader)` 里 `loader()` 返回的是 coroutine，直接被当值缓存起来。这就是 colored functions 问题的具体形态，只能加一个 `cache_aside_async`。
9. **observation 要限长。** 它会参与后续每一轮调用，几次 `fetch_page` 就能把上下文窗口顶满，导致后面真正相关的检索结果反而被挤掉。
10. **不要把原始异常消息交给模型。** httpx 的报错带完整 URL（可能含 query 里的敏感信息），未预期异常可能带内部路径。只传类型名。


