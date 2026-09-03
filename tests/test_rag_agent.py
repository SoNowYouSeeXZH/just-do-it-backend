"""ReAct 循环编排的单测(脚本化假 LLM,不打真网)。

Agent 的 bug 大多不是"某个函数算错了",而是"轮次没停下来"、"observation 没接
回去"、"来源编号对不上"这类编排错误——它们只在多轮交互中出现,单独测每个
函数都是绿的。所以这里全部按"排好剧本、断言编排结果"的方式写。
"""

import asyncio

import pytest

from app.services import llm
from app.services.rag import agent, tools
from app.services.rag.search_provider import SearchHit


class _FakeToolCall:
    def __init__(self, name: str, arguments: str, call_id: str = "call_1") -> None:
        self.id = call_id
        self.function = type("_Fn", (), {"name": name, "arguments": arguments})()


class _FakeMessage:
    def __init__(self, content: str = "", tool_calls=None) -> None:
        self.content = content
        self.tool_calls = tool_calls


def _script(monkeypatch: pytest.MonkeyPatch, turns: list[_FakeMessage]) -> list[list]:
    seen: list[list] = []
    queue = list(turns)

    async def fake_complete(messages, *, tools=None):
        seen.append([dict(m) for m in messages])
        return queue.pop(0) if queue else _FakeMessage(content="兜底文本")

    monkeypatch.setattr(llm, "complete", fake_complete)
    return seen


def _stream(monkeypatch: pytest.MonkeyPatch, text: str) -> list[list]:
    seen: list[list] = []

    async def fake_stream(messages):
        seen.append([dict(m) for m in messages])
        for char in text:
            yield char

    monkeypatch.setattr(llm, "stream_completion", fake_stream)
    return seen


def _fake_execute(monkeypatch: pytest.MonkeyPatch, results: dict):
    calls: list[str] = []

    async def fake_execute(name: str, raw_arguments):
        calls.append(name)
        return results.get(name, tools.ToolResult(text=f"{name} 无预设"))

    monkeypatch.setattr(tools, "execute", fake_execute)
    return calls


def _collect(question: str = "纳塔有哪些神像") -> list:
    async def run():
        return [event async for event in agent.answer_stream(question)]

    return asyncio.run(run())


def _text_of(events: list) -> str:
    return "".join(e.text for e in events if isinstance(e, agent.DeltaEvent))


# ---------------------------------------------------------------------------
# 基本编排:调工具 → observation 接回 → 综合生成
# ---------------------------------------------------------------------------


def test_tool_call_then_final_text(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _script(
        monkeypatch,
        [
            _FakeMessage(tool_calls=[_FakeToolCall("search_guides", '{"query":"纳塔"}')]),
            _FakeMessage(content="资料够了"),
        ],
    )
    _stream(monkeypatch, "纳塔有七座神像[1]")
    calls = _fake_execute(
        monkeypatch,
        {
            "search_guides": tools.ToolResult(
                text="搜索结果:纳塔",
                candidates=[("纳塔", "https://w/ys/纳塔")],
            )
        },
    )

    events = _collect()

    assert calls == ["search_guides"], "工具必须被真的执行"
    # 第二轮的 messages 里要能看到 observation 已经接回去了
    second_turn = seen[1]
    assert any(m["role"] == "tool" and "搜索结果" in m["content"] for m in second_turn)
    assert "纳塔有七座神像[1]" in _text_of(events)


def test_assistant_tool_calls_are_paired_with_tool_results(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """OpenAI 协议要求 assistant.tool_calls 和 role=tool 结果按 id 严格配对。

    不配对上游会直接拒绝整个请求,而且报错信息很难看出是配对问题。
    """
    seen = _script(
        monkeypatch,
        [
            _FakeMessage(
                tool_calls=[_FakeToolCall("search_guides", "{}", call_id="call_abc")]
            ),
            _FakeMessage(content="好了"),
        ],
    )
    _stream(monkeypatch, "答案")
    _fake_execute(monkeypatch, {"search_guides": tools.ToolResult(text="obs")})

    _collect()

    second_turn = seen[1]
    assistant = next(m for m in second_turn if m["role"] == "assistant")
    tool_msg = next(m for m in second_turn if m["role"] == "tool")
    assert assistant["tool_calls"][0]["id"] == "call_abc"
    assert tool_msg["tool_call_id"] == "call_abc"


def test_stage_events_precede_deltas(monkeypatch: pytest.MonkeyPatch) -> None:
    """进度事件要在正文之前发,否则前端的"正在搜索"提示毫无意义。"""
    _script(
        monkeypatch,
        [
            _FakeMessage(
                tool_calls=[_FakeToolCall("search_guides", '{"query":"纳塔"}')]
            ),
            _FakeMessage(content="ok"),
        ],
    )
    _stream(monkeypatch, "答案")
    _fake_execute(monkeypatch, {"search_guides": tools.ToolResult(text="obs")})

    events = _collect()
    kinds = [type(e).__name__ for e in events]

    assert kinds[0] == "StageEvent"
    assert kinds.index("DeltaEvent") > kinds.index("StageEvent")
    # 检索阶段的提示要带上关键词,用户才知道在搜什么
    assert "纳塔" in events[0].detail
    # 检索完进入作答阶段,前端可以据此把"正在搜索"换掉
    assert any(
        isinstance(e, agent.StageEvent) and e.stage == "answering" for e in events
    )


# ---------------------------------------------------------------------------
# 来源溯源:服务端确定性组装
# ---------------------------------------------------------------------------


def test_sources_section_is_appended_deterministically(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _script(
        monkeypatch,
        [
            _FakeMessage(tool_calls=[_FakeToolCall("search_guides", "{}")]),
            _FakeMessage(content="ok"),
        ],
    )
    _stream(monkeypatch, "神像在这些位置[1]")
    _fake_execute(
        monkeypatch,
        {
            "search_guides": tools.ToolResult(
                text="obs",
                candidates=[("纳塔", "https://w/ys/纳塔"), ("神像", "https://w/ys/神像")],
            )
        },
    )

    text = _text_of(_collect())

    assert "来源" in text
    assert "https://w/ys/纳塔" in text
    assert "[1] 纳塔" in text and "[2] 神像" in text


def test_fetched_sources_rank_before_search_only_ones(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """被抓过正文的页面内容真的进了上下文,它对答案的贡献是实的;
    只出现在搜索结果里的只贡献了一句摘要。编号 [1] 应该给前者。"""
    _script(
        monkeypatch,
        [
            _FakeMessage(tool_calls=[_FakeToolCall("search_guides", "{}")]),
            _FakeMessage(tool_calls=[_FakeToolCall("fetch_page", "{}", "call_2")]),
            _FakeMessage(content="ok"),
        ],
    )
    _stream(monkeypatch, "答案")
    _fake_execute(
        monkeypatch,
        {
            "search_guides": tools.ToolResult(
                text="obs1",
                candidates=[("A", "https://w/a"), ("B", "https://w/b")],
            ),
            "fetch_page": tools.ToolResult(text="obs2", fetched_url="https://w/b"),
        },
    )

    text = _text_of(_collect())

    assert "[1] B" in text, "抓过正文的应该排在前面"
    assert "[2] A" in text


def test_model_cannot_forge_source_urls(monkeypatch: pytest.MonkeyPatch) -> None:
    """来源小节由代码拼装,模型碰不到这段文本。

    让模型自己输出来源列表时它会编造 URL——这是 LLM 最稳定的失败模式之一。
    这条用例断言:即使模型在正文里编了一个假地址,真来源小节仍然只有真 URL。
    """
    _script(
        monkeypatch,
        [
            _FakeMessage(tool_calls=[_FakeToolCall("search_guides", "{}")]),
            _FakeMessage(content="ok"),
        ],
    )
    _stream(monkeypatch, "参考 https://fake.example.com/我编的页面")
    _fake_execute(
        monkeypatch,
        {
            "search_guides": tools.ToolResult(
                text="obs", candidates=[("真页面", "https://w/ys/real")]
            )
        },
    )

    text = _text_of(_collect())
    sources_part = text.split("来源：", 1)[1]

    assert "https://w/ys/real" in sources_part
    assert "fake.example.com" not in sources_part


def test_fetching_unlisted_url_is_still_credited(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """模型可能抓一个没出现在搜索结果里的 URL(它自己拼的)。

    仍然要记进来源:它的内容确实进了上下文,溯源就该体现出来——
    藏起来反而让"回答依据什么"变得不可查。
    """
    _script(
        monkeypatch,
        [
            _FakeMessage(tool_calls=[_FakeToolCall("fetch_page", "{}")]),
            _FakeMessage(content="ok"),
        ],
    )
    _stream(monkeypatch, "答案")
    _fake_execute(
        monkeypatch,
        {"fetch_page": tools.ToolResult(text="obs", fetched_url="https://w/unlisted")},
    )

    assert "https://w/unlisted" in _text_of(_collect())


# ---------------------------------------------------------------------------
# 边界:不检索、迭代上限、工具失败
# ---------------------------------------------------------------------------


def test_no_tool_call_means_no_sources_section(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """闲聊问题模型不调工具。此时不能出现"来源"小节——
    提了来源模型就会为了凑格式编出来源。"""
    _script(monkeypatch, [_FakeMessage(content="你好")])
    synthesis = _stream(monkeypatch, "你好,有什么可以帮你")

    text = _text_of(_collect("你好"))

    assert "来源" not in text
    # 综合阶段的 system prompt 应该是原始人设,不含"只依据这些资料"
    assert "只依据这些资料" not in synthesis[0][0]["content"]


def test_hitting_iteration_limit_stops_and_warns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """模型可能反复调工具却始终不给结论。没有硬上限就是会烧钱、卡住请求的死循环。"""
    monkeypatch.setattr(agent.settings, "rag_max_iterations", 3)
    always_tool = [
        _FakeMessage(tool_calls=[_FakeToolCall("search_guides", "{}")]) for _ in range(10)
    ]
    seen = _script(monkeypatch, always_tool)
    synthesis = _stream(monkeypatch, "尽力回答")
    calls = _fake_execute(monkeypatch, {"search_guides": tools.ToolResult(text="obs")})

    _collect()

    assert len(seen) == 3, "必须停在配置的迭代上限"
    assert len(calls) == 3
    # 达到上限时要告诉综合阶段"资料可能不完整",否则模型会当作检索充分
    assert "资料可能不完整" in synthesis[0][0]["content"]


def test_tool_failure_does_not_abort_the_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """工具失败只是一条 observation,模型仍然要能给出回答。"""
    _script(
        monkeypatch,
        [
            _FakeMessage(tool_calls=[_FakeToolCall("search_guides", "{}")]),
            _FakeMessage(content="检索失败了,我说明一下"),
        ],
    )
    _stream(monkeypatch, "没查到相关资料")
    _fake_execute(
        monkeypatch,
        {"search_guides": tools.ToolResult(text="工具执行失败:搜索超时(20.0s)")},
    )

    text = _text_of(_collect())

    assert "没查到相关资料" in text
    # 一条来源都没有时不应该硬拼一个空的来源小节
    assert "来源：" not in text


def test_multiple_tool_calls_in_one_turn_all_execute(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """模型可以在一轮里并行发起多个工具调用,每个都要执行并各自接回结果。"""
    seen = _script(
        monkeypatch,
        [
            _FakeMessage(
                tool_calls=[
                    _FakeToolCall("search_guides", '{"query":"A"}', "c1"),
                    _FakeToolCall("search_guides", '{"query":"B"}', "c2"),
                ]
            ),
            _FakeMessage(content="ok"),
        ],
    )
    _stream(monkeypatch, "答案")
    calls = _fake_execute(monkeypatch, {"search_guides": tools.ToolResult(text="obs")})

    _collect()

    assert len(calls) == 2
    tool_msgs = [m for m in seen[1] if m["role"] == "tool"]
    assert {m["tool_call_id"] for m in tool_msgs} == {"c1", "c2"}


# ---------------------------------------------------------------------------
# 非流式入口
# ---------------------------------------------------------------------------


def test_answer_returns_same_text_as_stream(monkeypatch: pytest.MonkeyPatch) -> None:
    """非流式和流式必须是同一条管线,否则两个入口会慢慢长出不同行为。"""
    _script(
        monkeypatch,
        [
            _FakeMessage(tool_calls=[_FakeToolCall("search_guides", "{}")]),
            _FakeMessage(content="ok"),
        ],
    )
    _stream(monkeypatch, "正文内容")
    _fake_execute(
        monkeypatch,
        {
            "search_guides": tools.ToolResult(
                text="obs", candidates=[("页面", "https://w/p")]
            )
        },
    )

    text = asyncio.run(agent.answer("问题"))

    assert "正文内容" in text
    assert "https://w/p" in text


def test_agent_emits_structured_events_not_sse(monkeypatch: pytest.MonkeyPatch) -> None:
    """传输协议属于接口层。Agent 只说"进入检索阶段"和"多了一段文本",
    由上层决定序列化成 SSE、WebSocket 还是别的——所以事件里不能有 data: 前缀。"""
    _script(monkeypatch, [_FakeMessage(content="ok")])
    _stream(monkeypatch, "答案")

    events = _collect("你好")

    for event in events:
        payload = getattr(event, "text", "") + getattr(event, "detail", "")
        assert "data:" not in payload
        assert "[DONE]" not in payload
