"""工具注册表:OpenAI tools schema 与 Python 执行器的一一对应。

## 为什么要"一一对应"这件事单独成文件

function calling 的坑几乎都出在两边不一致上:schema 里写了参数 `query`,
执行器却读 `q`;schema 声明了三个工具,执行器只实现了两个,模型偏偏调了
第三个。这类错误在运行时才暴露,而且报出来的是 KeyError 之类的底层异常,
排查时看不出是"契约不一致"。

所以这里把 schema 和执行器放在同一个字典里定义,并写了一条单测断言两边
的名字集合完全相等——不一致会在 CI 就红,而不是等模型某天调到它。

## 工具的错误处理原则

**工具抛错不终止请求。** 搜索超时、页面 404、被 WAF 拦,这些都是外部世界的
常态。正确做法是把错误文本作为 observation 交回模型,让它改写关键词重试、
换一个来源,或者声明检索失败后凭常识作答。

如果让工具异常冒到上层变成 500,用户看到的是"服务异常",而实际上模型完全
有能力换个思路。这是 Agent 相比传统调用链的一个真实优势,别把它堵死。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from app.config import settings
from app.services.rag import page_fetcher, search_provider

logger = logging.getLogger(__name__)

# 单次工具结果的最大字符数。工具结果会被塞进 messages 参与后续每一轮调用,
# 不设上限的话几次 fetch 就能把上下文窗口顶满,后面的检索结果反而被挤掉。
_MAX_OBSERVATION_CHARS = 8000


@dataclass
class ToolResult:
    """一次工具调用的产出。

    为什么不只返回字符串:`text` 是给模型看的 observation,而 Agent 还需要
    结构化的来源信息来确定性地组装引用编号。如果只返回文本,Agent 就得去
    解析自己刚生成的格式——那是最容易在改文案时静默失效的一类耦合。

    所以两者分开:文本给模型,结构给代码。
    """

    text: str
    # 搜索发现的候选来源,(标题, URL),按结果顺序
    candidates: list[tuple[str, str]] = field(default_factory=list)
    # 本次真正抓取了正文的 URL(fetch_page 才有)
    fetched_url: str | None = None


async def _run_search(arguments: dict[str, Any]) -> ToolResult:
    query = (arguments.get("query") or "").strip()
    if not query:
        return ToolResult(text="工具调用缺少 query 参数,请重新给出要搜索的关键词。")

    hits = await search_provider.search_guides(query)
    if not hits:
        # 明确区分"检索不到"和"检索失败"。前者可能是关键词太长太具体
        # (MediaWiki 的搜索对多词偏 AND),提示模型换更短的词比只说"没结果"有用。
        return ToolResult(
            text=(
                f"没有找到与「{query}」相关的 wiki 页面。"
                "可以试试更短、更接近词条名的关键词(比如只留游戏内的专有名词)。"
            )
        )

    lines = [f"搜索「{query}」得到 {len(hits)} 条结果:"]
    for index, hit in enumerate(hits, start=1):
        lines.append(f"{index}. {hit.title}\n   URL: {hit.url}\n   摘要: {hit.snippet}")
    return ToolResult(
        text="\n".join(lines),
        candidates=[(hit.title, hit.url) for hit in hits],
    )


async def _run_fetch(arguments: dict[str, Any]) -> ToolResult:
    url = (arguments.get("url") or "").strip()
    if not url:
        return ToolResult(text="工具调用缺少 url 参数,请从搜索结果里挑一个 URL。")

    text = await page_fetcher.fetch_page(url)
    return ToolResult(text=f"页面 {url} 的正文:\n{text}", fetched_url=url)


# schema 与执行器成对定义。加新工具时两边必须同时写,漏一边单测会红。
TOOLS: dict[str, dict[str, Any]] = {
    "search_guides": {
        "schema": {
            "type": "function",
            "function": {
                "name": "search_guides",
                "description": (
                    "在游戏 wiki 里检索攻略页面,返回标题、URL 和摘要。"
                    "回答具体游戏内容(角色、任务、道具、地图、数值)时必须先用它,"
                    "不要凭记忆回答——记忆里的版本可能已经过期。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": (
                                "检索关键词。尽量短、贴近 wiki 词条名,"
                                "长句子会因为搜索引擎按词取交集而查不到。"
                            ),
                        }
                    },
                    "required": ["query"],
                },
            },
        },
        "run": _run_search,
    },
    "fetch_page": {
        "schema": {
            "type": "function",
            "function": {
                "name": "fetch_page",
                "description": (
                    "抓取一个 URL 的正文。搜索结果的摘要通常只有一两句,"
                    "需要具体数值、步骤、条件时用它读完整页面。"
                    "URL 必须来自 search_guides 的结果,不要自己编。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "url": {
                            "type": "string",
                            "description": "要抓取的页面地址,必须是 http/https。",
                        }
                    },
                    "required": ["url"],
                },
            },
        },
        "run": _run_fetch,
    },
}


def tool_schemas() -> list[dict[str, Any]]:
    """给 LLM 的 tools 参数。"""
    return [entry["schema"] for entry in TOOLS.values()]


def _executor(name: str) -> Callable[[dict[str, Any]], Awaitable[ToolResult]] | None:
    entry = TOOLS.get(name)
    return entry["run"] if entry else None


def parse_arguments(raw: str | None) -> dict[str, Any]:
    """解析模型给的参数 JSON。

    模型偶尔会给出不合法的 JSON(截断、多一个尾逗号)。这里返回空字典而不是
    抛异常,让执行器走"缺参数"分支给模型一条可读的提示——比抛 JSONDecodeError
    让整个请求挂掉友好得多。
    """
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        logger.info("工具参数不是合法 JSON,按空参数处理: %r", raw)
        return {}
    return parsed if isinstance(parsed, dict) else {}


async def execute(name: str, raw_arguments: str | None) -> ToolResult:
    """执行一次工具调用,永远返回一个可用的 ToolResult。

    这个函数刻意**不抛异常**。它的 text 会被原样塞进 messages 交回模型,
    所以"失败"也必须是一段模型读得懂的话,而不是一个异常。
    """
    executor = _executor(name)
    if executor is None:
        # 模型幻觉出一个不存在的工具名是真实会发生的。告诉它有哪些可用,
        # 它下一轮通常就能改对。
        available = "、".join(TOOLS)
        return ToolResult(text=f"没有名为 {name} 的工具。可用工具:{available}。")

    arguments = parse_arguments(raw_arguments)
    try:
        result = await executor(arguments)
    except (search_provider.SearchError, page_fetcher.FetchError) as exc:
        # 已知的外部失败:原文交给模型。这些异常的消息已经做过脱敏
        # (只保留类型名,不含 URL 和堆栈),可以安全地进上下文。
        logger.info("工具 %s 执行失败: %s", name, exc)
        return ToolResult(
            text=f"工具 {name} 执行失败:{exc}。可以换个关键词或换一个来源再试。"
        )
    except Exception as exc:  # noqa: BLE001 - 兜底,不让未预期异常打断整个对话
        # 这里只给类型名。未预期异常的消息里可能有内部路径、SQL 片段,
        # 而 observation 会进上下文、最终可能被模型复述给用户。
        logger.error("工具 %s 出现未预期异常", name, exc_info=True)
        return ToolResult(
            text=f"工具 {name} 出现内部错误({type(exc).__name__}),请换一种方式。"
        )

    if len(result.text) > _MAX_OBSERVATION_CHARS:
        result.text = result.text[:_MAX_OBSERVATION_CHARS] + "\n...(内容过长已截断)"
    return result


def max_iterations() -> int:
    return settings.rag_max_iterations
