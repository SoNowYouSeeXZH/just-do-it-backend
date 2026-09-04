"""内容审核:敏感词匹配。

为什么 UGC 必须有这一层:国内 app 上线的合规硬要求是「平台对用户发布的内容
负责」。完全不做过滤,一条违规内容就可能导致整个应用下架。

这里只做第一道闸,不追求"识别所有违规内容"——那需要语义模型或第三方审核服务。
敏感词匹配的定位是:成本近乎为零,能挡掉最直接的一批,并把可疑内容推进人工复核。

绕过手段与对策:
- 大小写变形(SB / sb)          -> 统一转小写
- 插入空格/标点(赌 博、v.p.n)  -> 匹配前剔除所有非字母数字汉字的字符
- 同音字、拼音、谐音、图片      -> 本层挡不住,需要语义审核,属于后续迭代

刻意不做的事:不在日志里打印命中的完整正文。审核日志会被广泛访问,
把用户原文抄一遍等于把敏感内容又扩散了一层,只记 id 和命中词即可。
"""

import re

from app.config import settings

# 默认词表放代码里而不是数据库:它需要被 code review、需要有测试覆盖,
# 且改动频率远低于业务数据。环境相关的追加词走 settings。
# 这里只放少量占位词用于验证机制;真实词表应按运营要求维护。
_DEFAULT_BANNED_WORDS = (
    "代练",
    "外挂",
    "私服",
    "刷钻",
    "博彩",
)

# 只保留字母、数字、汉字——其余字符(空格、标点、emoji)都是常见的插入式绕过载体。
_NOISE_PATTERN = re.compile(r"[^0-9a-z\u4e00-\u9fff]+")


def _normalize(text: str) -> str:
    return _NOISE_PATTERN.sub("", text.lower())


def banned_words() -> tuple[str, ...]:
    """当前生效的词表 = 内置默认 + 环境追加。"""
    extra = tuple(
        word.strip() for word in settings.moderation_extra_banned_words if word.strip()
    )
    return _DEFAULT_BANNED_WORDS + extra


def find_banned_word(*texts: str) -> str | None:
    """返回第一个命中的敏感词,没命中返回 None。

    多个字段(标题 + 正文)一起传进来,避免调用方重复写循环。
    """
    normalized = _normalize(" ".join(texts))
    for word in banned_words():
        if _normalize(word) in normalized:
            return word
    return None
