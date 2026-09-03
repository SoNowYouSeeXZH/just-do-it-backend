"""缓存 key 的集中定义。

为什么要单独一个模块,而不是在各 Service 里就地拼字符串:

1. **失效方需要知道 key 的形状。** 题库写入接口(admin)改完数据后要清掉
   相关缓存,它必须和读取方用同一套 key 规则。字符串散落在各处时,
   改了读取方忘了改失效方,就会出现"数据更新了但接口一直返回旧值"
   这类极难排查的问题。

2. **前缀即命名空间。** 统一带上 `jd:`(项目缩写)前缀,和同一个 Redis 实例上
   其他用途的 key 区分开;按业务再分二级前缀,便于批量失效和排查。

命名约定:`jd:<业务域>:<具体对象>`,冒号分隔是 Redis 社区惯例
(RedisInsight 之类的工具会按冒号折叠成树状展示)。
"""

# 职业列表(带题目数)。全局一份,不区分用户——内容对所有人相同。
JOBS_LIST = "jd:jobs:list"

# 职业相关缓存的公共前缀。题库变更时用它批量失效:
# 新增一道题会同时影响「职业列表的题目数」和「单个职业的题目数」,
# 逐个删 key 容易漏,按前缀清更可靠。
JOBS_PREFIX = "jd:jobs:"

# 行业相关缓存前缀
INDUSTRIES_PREFIX = "jd:industries:"

# 行业列表
INDUSTRIES_LIST = "jd:industries:list"


def job_detail(job_id: str) -> str:
    """单个职业详情(含题目数)的 key。"""
    return f"jd:jobs:detail:{job_id}"


def job_question_titles(job_id: str) -> str:
    """某职业的题干列表的 key。

    注意随机抽题接口(sample_questions)故意不缓存——
    它每次都要返回不同的随机题目,缓存会让"随机"失效,
    用户重复进入同一职业会一直看到同一批题。
    这是一个「不是所有读接口都该缓存」的具体例子。
    """
    return f"jd:jobs:titles:{job_id}"


def industry_detail(industry_id: str) -> str:
    """单个行业详情的 key。"""
    return f"jd:industries:detail:{industry_id}"


# RAG 检索相关缓存前缀。
RAG_PREFIX = "jd:rag:"


def rag_search(query_fingerprint: str) -> str:
    """一次搜索查询的结果缓存 key。

    传进来的是**归一化后再哈希**的查询指纹,不是原始 query,原因有三:
    - 原始 query 是用户输入,可能带冒号——而冒号是 Redis key 的层级分隔符,
      直接拼进去会把 key 的树状结构搞乱
    - 长度不可控,哈希后定长
    - 顺带避免把用户原文明文存进 Redis 的 key 名里

    注意这里只缓存搜索结果,不缓存 LLM 回答:回答依赖完整对话上下文,
    同样一句话在不同上下文里该给出不同答案,缓存它会答错。
    """
    return f"jd:rag:search:{query_fingerprint}"
