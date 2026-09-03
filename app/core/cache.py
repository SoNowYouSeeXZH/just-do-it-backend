"""缓存基础设施:Redis 客户端封装 + 读写策略。

这一层属于 core(横切基础设施),不是业务 Service。Service 通过
`cache_aside` 使用它,不需要知道底下是 Redis 还是别的实现。

三个贯穿全文的设计原则:

1. **fail open(失败放行)**
   缓存是加速手段,不是数据来源。Redis 挂了应该退化成"直接查数据库",
   而不是整个接口 500。所以所有 Redis 操作都包在 try/except 里,
   出错记日志然后当作"没命中"继续走。
   反面例子:很多人把缓存当依赖,Redis 一抖动整站不可用——
   本来只是想让系统更快,结果多了一个单点故障。

2. **防穿透:缓存空结果**
   查一个不存在的 job_id,数据库返回空,如果不缓存这个"空",
   那么每次请求都会打到数据库。攻击者用随机 id 刷接口就能绕过缓存。
   解法是把"空"也缓存起来(用哨兵值区分"没缓存"和"缓存了空"),
   但 TTL 要短——它不是真数据,只是个挡板。

3. **防雪崩:TTL 抖动**
   如果一批 key 同时写入、TTL 相同,它们会在同一秒集体过期,
   那一瞬间所有请求穿透到数据库。加一个随机抖动把过期时间打散。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
import json
import logging
import random
from typing import Any, TypeVar

import redis

from app.config import settings

logger = logging.getLogger(__name__)

T = TypeVar("T")

# 哨兵值:用来在 Redis 里表达"这个 key 查过,结果确实是空"。
# 为什么不能直接存空字符串或 "null":那样无法和"业务数据本身就是 null"区分。
# 用一个业务上不可能出现的字符串,语义最清晰。
_NULL_SENTINEL = "__cache_null__"

# 模块级单例。redis-py 的客户端内部自带连接池,是线程安全的,
# 不需要(也不应该)每次请求新建——那样每次都要重新握手。
_client: redis.Redis | None = None
# 记录是否已经因为连接失败而降级,避免每个请求都打一条 ERROR 日志把日志刷满。
_degraded = False


def get_client() -> redis.Redis | None:
    """返回 Redis 客户端;未启用或连接失败时返回 None。

    返回 None 而不是抛异常,是为了让调用方用最自然的方式处理降级:
    `if client is None: 直接查库`。
    """
    global _client

    if not settings.cache_enabled:
        return None

    if _client is None:
        _client = redis.Redis(
            host=settings.redis_host,
            port=settings.redis_port,
            db=settings.redis_db,
            password=settings.redis_password or None,
            # decode_responses=True 让读出来的是 str 而不是 bytes,
            # 省掉每处都要 .decode() 的样板代码。
            decode_responses=True,
            socket_connect_timeout=settings.redis_timeout_seconds,
            socket_timeout=settings.redis_timeout_seconds,
        )
    return _client


def _ttl_with_jitter(base: int) -> int:
    """给 TTL 加上 ±10% 的随机抖动,避免同一批 key 集体过期。

    这就是"缓存雪崩"的最小成本解法。假设首页一次加载写入 50 个 key、
    TTL 都是 300 秒,那么 300 秒后这 50 个 key 会在同一瞬间失效,
    50 个请求同时穿透到数据库。抖动把它们摊到 270~330 秒之间。
    """
    jitter = int(base * 0.1)
    return base + random.randint(-jitter, jitter) if jitter else base


def get(key: str) -> Any | None:
    """读缓存。返回 None 表示未命中(或已降级)。

    注意区分两种"空":
    - 返回 None:没有缓存,调用方需要查数据库
    - 返回 _NULL_SENTINEL 对应的 CachedNull:缓存里明确记着"数据库也没有"
    """
    global _degraded

    client = get_client()
    if client is None:
        return None

    try:
        raw = client.get(key)
        _degraded = False
    except redis.RedisError as exc:
        # 只在从"正常"变成"降级"的那一次打日志,避免刷屏
        if not _degraded:
            logger.error("缓存读取失败,降级为直接查库: %s", exc)
            _degraded = True
        return None

    if raw is None:
        return None
    if raw == _NULL_SENTINEL:
        return CachedNull
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        # 缓存里的值格式不对(比如换了序列化方式、或被别的程序写脏了)。
        # 当作未命中处理比抛异常好:业务能自愈,下一次写入会覆盖掉脏值。
        logger.warning("缓存值无法反序列化,当作未命中: key=%s", key)
        return None


def set_value(key: str, value: Any, *, ttl: int | None = None) -> None:
    """写缓存。value 为 None 时写入空哨兵并使用更短的 TTL。"""
    global _degraded

    client = get_client()
    if client is None:
        return

    if value is None:
        payload = _NULL_SENTINEL
        expire = settings.cache_null_ttl_seconds
    else:
        try:
            payload = json.dumps(value, ensure_ascii=False, default=str)
        except (TypeError, ValueError) as exc:
            # 序列化失败不能影响主流程——数据已经查到了,只是存不进缓存。
            logger.warning("缓存值无法序列化,跳过写入: key=%s err=%s", key, exc)
            return
        expire = ttl if ttl is not None else settings.cache_ttl_seconds

    try:
        client.setex(key, _ttl_with_jitter(expire), payload)
        _degraded = False
    except redis.RedisError as exc:
        if not _degraded:
            logger.error("缓存写入失败,忽略: %s", exc)
            _degraded = True


def delete_prefix(prefix: str) -> int:
    """删除某个前缀下的所有 key,返回删除数量。

    用 SCAN 而不是 KEYS:KEYS 会一次遍历整个 keyspace 并阻塞 Redis
    (它是单线程的),线上几十万 key 时足以造成一次可观测的抖动。
    SCAN 是游标式分批扫描,不会长时间占住服务。
    """
    client = get_client()
    if client is None:
        return 0

    try:
        removed = 0
        # scan_iter 内部会自动带着游标翻页,不需要手动管理 cursor
        for key in client.scan_iter(match=f"{prefix}*", count=100):
            removed += client.delete(key)
        return removed
    except redis.RedisError as exc:
        logger.error("缓存失效失败(数据库已更新,缓存将等待 TTL 自然过期): %s", exc)
        return 0


class CachedNull:
    """表示"缓存里明确记录着:数据库查不到"的标记类。

    用类对象本身(而不是实例)当标记,判断时用 `is CachedNull`,
    不会和任何业务值相等,也不需要额外分配对象。
    """


def cache_aside(
    key: str,
    loader: Callable[[], T],
    *,
    ttl: int | None = None,
) -> T:
    """Cache-Aside(旁路缓存)模式的统一实现。

    流程:
        读缓存 → 命中就返回
              → 未命中 → 查数据库 → 回写缓存 → 返回

    为什么叫"旁路":缓存不在数据库前面强制拦截,而是放在旁边由应用主动查。
    应用完全掌控何时读、何时写、何时失效——这是最常见也最可控的缓存模式。

    对比另外两种:
    - Read/Write Through:应用只跟缓存打交道,缓存自己负责同步数据库。
      实现复杂,通常由缓存中间件提供。
    - Write Behind:先写缓存后异步刷库,吞吐最高但可能丢数据。

    注意这个实现没有做"缓存击穿"(热点 key 过期瞬间大量并发同时查库)的保护。
    要处理需要加分布式锁或单飞(single flight),对当前量级是过度设计;
    真正需要时应该只给少数热点 key 加,而不是所有查询都付锁的代价。
    """
    cached = get(key)
    if cached is CachedNull:
        # 缓存明确记着"数据库没有",直接返回空,不必再查库。
        # 这正是防穿透的效果所在。
        return None  # type: ignore[return-value]
    if cached is not None:
        return cached  # type: ignore[return-value]

    fresh = loader()
    set_value(key, fresh, ttl=ttl)
    return fresh


async def cache_aside_async(
    key: str,
    loader: Callable[[], Awaitable[T]],
    *,
    ttl: int | None = None,
) -> T:
    """`cache_aside` 的异步版本,给 loader 是协程的场景用(如外部检索)。

    为什么要单独一个函数而不是让 cache_aside 兼容两种 loader:
    `await` 只能出现在 async 函数里,而 async 函数的返回值是协程——
    同步调用方拿到的就不是数据了。Python 里同步/异步无法在一个函数里
    透明兼容(所谓 "colored functions" 问题),分成两个函数最诚实。

    Redis 读写本身仍走同步客户端(redis-py)。这些是毫秒级的本机/内网操作,
    且已有 0.5s 超时兜底,为它再引入一套异步客户端不划算——项目里
    数据库访问同样是同步 Session,风格一致。
    """
    cached = get(key)
    if cached is CachedNull:
        return None  # type: ignore[return-value]
    if cached is not None:
        return cached  # type: ignore[return-value]

    fresh = await loader()
    set_value(key, fresh, ttl=ttl)
    return fresh


def reset_client_for_tests() -> None:
    """清空模块级单例,仅供测试使用。

    因为 _client 是模块级缓存,测试里改了 settings(比如指向 fakeredis)
    之后必须重置,否则会继续用上一个测试建立的客户端。
    """
    global _client, _degraded
    _client = None
    _degraded = False
