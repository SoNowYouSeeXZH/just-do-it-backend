"""ReAct 循环编排 + 终答综合。

## 为什么是两段式而不是一个流式循环

最自然的写法是"边循环边流式输出",但那样做不到:function calling 的那一轮
必须拿到完整的 `tool_calls` 才能执行工具,而流式响应是一小段一小段来的,
拼装 tool_calls 增量既繁琐又容易出错。更要紧的是——检索过程中模型的中间
"思考"不应该直接喷给用户,用户想看的是结论。

所以拆成两段:

1. **检索循环(非流式、有界)**:把对话 + 工具定义交给模型;模型返回
   tool_calls 就执行工具、把结果作为 observation 追加,继续下一轮;
   返回纯文本或达到迭代上限则结束。产出检索上下文 + 用过的来源。
2. **综合生成(流式)**:一次独立的 stream 调用,system prompt 里注入检索
   上下文和编号来源,要求正文用 [1][2] 引用。delta 直接透传,
   保持现有打字机体验。

代价是闲聊问题也会多一次轻量调用(第一段模型不调工具、直接给文本,
但第二段还要再生成一遍)。换来的是管线统一——不用为"要不要检索"写两条
互不相干的代码路径,而路由决策交给模型自己(它不调工具就等于判定"不用查")。

## 引用溯源是服务端保证的,不依赖模型自觉

来源编号列表由本模块从「实际被 fetch_page 抓过的 URL + search 返回的候选」
**确定性组装**,再拼在回答末尾。模型只负责在正文里引用编号。

这条边界很重要:如果让模型自己输出来源列表,它会编造 URL——这是 LLM 最
稳定的失败模式之一。让它引用编号、编号由代码维护,幻觉就没有落脚点。
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

from app.config import settings
from app.services import llm
from app.services.rag import tools

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = (
    "你是一个游戏攻略助手。\n"
    "- 回答具体游戏内容(角色、任务、道具、地图、数值、版本改动)时,必须先用 "
    "search_guides 检索,再按需用 fetch_page 读正文,不要凭记忆回答——"
    "游戏内容版本迭代快,记忆里的信息很可能已经过期。\n"
    "- **检索关键词只写游戏内的专有名词,不要带游戏名。** 检索的是各游戏各自的 "
    "wiki 站,站点本身已经限定了游戏;把游戏名写进关键词会因为搜索引擎按词取交集"
    "而搜不到正确词条。比如问「原神的纳塔是什么地方」,应该搜「纳塔」而不是「原神 纳塔」。\n"
    "- 关键词也要短。多个词会被当作 AND 条件,越长越容易 0 命中;"
    "一次搜不到就换一个更短或更常见的词条名重试。\n"
    "- 检索不到时,明确说明没查到,再给通用建议。**不要编造具体数值、"
    "道具名、任务步骤或页面地址。**\n"
    "- 不是游戏相关的问题,直接回答即可,不用检索。"
)


@dataclass(frozen=True)
class Source:
    """一条来源。index 是给模型引用用的编号,从 1 开始。"""

    index: int
    title: str
    url: str


@dataclass
class StageEvent:
    """检索阶段的进度事件,给前端显示"正在搜索…"。"""

    stage: str
    detail: str = ""


@dataclass
class DeltaEvent:
    """终答的一小段文本。"""

    text: str


AgentEvent = StageEvent | DeltaEvent


@dataclass
class Retrieval:
    """检索循环的产出。"""

    observations: list[str] = field(default_factory=list)
    sources: list[Source] = field(default_factory=list)
    used_tools: bool = False
    hit_limit: bool = False

    @property
    def context(self) -> str:
        return "\n\n".join(self.observations)


class _SourceCollector:
    """按出现顺序收集来源,并记住哪些被真正抓取过。

    为什么要区分"搜到的"和"抓过的":抓过的页面内容真的进了上下文,
    它对答案的贡献是实的;只出现在搜索结果里的页面只贡献了一句摘要。
    排序时把抓过的放前面,编号 [1][2] 就更可能对应真正的依据。
    """

    def __init__(self) -> None:
        self._titles: dict[str, str] = {}
        self._order: list[str] = []
        self._fetched: set[str] = set()

    def add_candidate(self, title: str, url: str) -> None:
        if url and url not in self._titles:
            self._titles[url] = title or url
            self._order.append(url)

    def mark_fetched(self, url: str) -> None:
        if not url:
            return
        # 模型可能抓一个没在搜索结果里出现过的 URL(比如它自己拼的)。
        # 仍然记进来:它的内容确实进了上下文,溯源就该体现出来。
        if url not in self._titles:
            self._titles[url] = url
            self._order.append(url)
        self._fetched.add(url)

    def build(self) -> list[Source]:
        fetched = [u for u in self._order if u in self._fetched]
        rest = [u for u in self._order if u not in self._fetched]
        ordered = fetched + rest
        return [
            Source(index=i, title=self._titles[url], url=url)
            for i, url in enumerate(ordered[: settings.rag_search_max_results], start=1)
        ]


def _stage_detail(name: str, arguments: dict[str, Any]) -> str:
    """给前端看的一句人话。不要把原始参数 JSON 直接吐出去。"""
    if name == "search_guides":
        return f"正在搜索：{arguments.get('query', '')}".strip()
    if name == "fetch_page":
        return "正在阅读攻略页面"
    return f"正在调用 {name}"


async def retrieve(question: str) -> tuple[Retrieval, list[dict[str, Any]]]:
    """检索循环(非流式、有界)。返回产出 + 完整的 messages 轨迹。

    messages 一并返回是因为综合阶段要接着用:OpenAI 协议要求 assistant 的
    tool_calls 和后续 role="tool" 的结果严格配对,自己重新拼一份很容易错配。
    """
    events = [event async for event in _retrieve_events(question)]
    # _retrieve_events 把结果放在最后一项,这里只是给不关心进度的调用方
    # 提供一个同步风格的入口(测试和非流式 reply 用)。
    result = events[-1]
    assert isinstance(result, tuple)
    return result


async def _retrieve_events(
    question: str,
) -> AsyncIterator[StageEvent | tuple[Retrieval, list[dict[str, Any]]]]:
    """检索循环的内部实现,边跑边吐进度事件,最后一项是结果元组。"""
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": question},
    ]
    retrieval = Retrieval()
    collector = _SourceCollector()

    for iteration in range(1, tools.max_iterations() + 1):
        message = await llm.complete(messages, tools=tools.tool_schemas())
        tool_calls = getattr(message, "tool_calls", None)

        if not tool_calls:
            # 模型不再调工具,说明它认为信息够了(或者压根不需要检索)。
            # 它这一轮的文本不直接返回给用户——终答统一由综合阶段生成,
            # 保证有检索和没检索两条路径的输出风格一致。
            break

        # assistant 这条消息必须原样入列(带 tool_calls),否则下面的
        # role="tool" 结果会因为找不到对应的 call_id 被上游拒绝。
        messages.append(_assistant_message_dict(message))

        for call in tool_calls:
            name = call.function.name
            raw_arguments = call.function.arguments
            arguments = tools.parse_arguments(raw_arguments)

            yield StageEvent(stage="retrieving", detail=_stage_detail(name, arguments))

            result = await tools.execute(name, raw_arguments)
            retrieval.used_tools = True
            retrieval.observations.append(result.text)

            for title, url in result.candidates:
                collector.add_candidate(title, url)
            if result.fetched_url:
                collector.mark_fetched(result.fetched_url)

            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call.id,
                    "content": result.text,
                }
            )
    else:
        # for-else:循环把 range 跑完了都没 break,说明模型一直在调工具。
        # 这是"循环失控"的正常出口,不是异常——记下来让综合阶段知道
        # 信息可能不完整。
        retrieval.hit_limit = True
        logger.info("检索循环达到迭代上限 %d,question=%s", iteration, question)

    retrieval.sources = collector.build()
    yield (retrieval, messages)


def _assistant_message_dict(message: Any) -> dict[str, Any]:
    """把 SDK 的 assistant message 转成可以放回 messages 的 dict。

    只保留协议需要的字段。直接把 SDK 对象塞回去在部分版本里能work,
    但依赖了它的序列化行为——显式转换更稳。
    """
    return {
        "role": "assistant",
        "content": message.content or "",
        "tool_calls": [
            {
                "id": call.id,
                "type": "function",
                "function": {
                    "name": call.function.name,
                    "arguments": call.function.arguments,
                },
            }
            for call in message.tool_calls
        ],
    }


def _synthesis_prompt(retrieval: Retrieval) -> str:
    """综合阶段的 system prompt。"""
    if not retrieval.used_tools:
        # 没检索过(闲聊或模型判定不需要查)。此时不能提"来源",
        # 否则模型会为了凑格式编出来源。
        return SYSTEM_PROMPT

    lines = [
        "你是一个游戏攻略助手。下面是刚刚检索到的资料,请**只依据这些资料**回答用户的问题。",
        "",
        "要求:",
        "- 用到某条来源的信息时,在句末标注编号,如 [1]、[2]",
        "- 资料里没有的内容,直接说明「检索到的资料里没有提到」,不要补充记忆里的信息",
        "- 不要自己写「来源」小节,系统会自动附上",
    ]
    if retrieval.hit_limit:
        lines.append("- 注意:检索轮次已达上限,资料可能不完整,回答时请说明这一点")

    lines.extend(["", "可引用的来源:"])
    for source in retrieval.sources:
        lines.append(f"[{source.index}] {source.title} — {source.url}")

    lines.extend(["", "检索到的资料:", retrieval.context])
    return "\n".join(lines)


def _sources_section(sources: list[Source]) -> str:
    """确定性拼装的来源小节。模型碰不到这段文本,所以编不了 URL。"""
    if not sources:
        return ""
    lines = ["\n\n---\n来源："]
    lines.extend(f"[{s.index}] {s.title}\n{s.url}" for s in sources)
    return "\n".join(lines)


async def answer_stream(question: str) -> AsyncIterator[AgentEvent]:
    """完整的两段式回答,产出结构化事件。

    刻意**不产出 SSE 文本帧**:传输协议属于接口层。这里只说"进入了检索阶段"
    和"多了一段文本",由 api/services 层决定序列化成 SSE、WebSocket 还是别的。
    """
    retrieval: Retrieval | None = None
    messages: list[dict[str, Any]] = []

    async for item in _retrieve_events(question):
        if isinstance(item, StageEvent):
            yield item
        else:
            retrieval, messages = item

    assert retrieval is not None  # _retrieve_events 保证最后一项是结果

    yield StageEvent(stage="answering")

    synthesis_messages = [
        {"role": "system", "content": _synthesis_prompt(retrieval)},
        {"role": "user", "content": question},
    ]
    async for delta in llm.stream_completion(synthesis_messages):
        yield DeltaEvent(text=delta)

    section = _sources_section(retrieval.sources)
    if section:
        yield DeltaEvent(text=section)


async def answer(question: str) -> str:
    """非流式版本,给 /api/chat 的非流式分支用。"""
    parts: list[str] = []
    async for event in answer_stream(question):
        if isinstance(event, DeltaEvent):
            parts.append(event.text)
    return "".join(parts)
