"""系统健康检查业务逻辑。

健康检查里区分"必需依赖"和"可选依赖":
- 数据库不可用 → 探测抛异常 → 接口 500,负载均衡应停止发流量
- 缓存不可用 → 只在响应里标记 degraded,接口仍然 200

因为缓存是 fail-open 的:Redis 挂了应用还能正常服务(只是慢一些),
把它算成"不健康"会导致实例被误摘,反而放大故障。
但也不能完全不报——所以用 degraded 让运维能看到。
"""

from sqlmodel import Session

from app.core import cache
from app.repositories import health as health_repo


def check(session: Session) -> dict[str, str]:
    """检查关键依赖是否可用。"""
    # 数据库是必需依赖:探测失败会抛异常,由全局处理器转成 5xx
    health_repo.check_database(session)
    return {"status": "ok", "database": "ok", "cache": _cache_status()}


def _cache_status() -> str:
    """返回缓存状态:ok / disabled / degraded。

    三个状态而不是两个:"关掉了"和"连不上"是不同性质的事情。
    前者是有意的配置,后者需要有人去看一眼。
    """
    client = cache.get_client()
    if client is None:
        return "disabled"
    try:
        client.ping()
        return "ok"
    except Exception:
        # 这里刻意 catch 宽异常:健康检查本身不能因为探测失败而抛错,
        # 否则缓存故障会连带把整个健康检查打成 500,数据库正常也被误判。
        return "degraded"
