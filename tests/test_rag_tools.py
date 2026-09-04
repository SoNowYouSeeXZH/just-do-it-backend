"""ReAct 循环与工具的单测。

核心手法是**脚本化假 LLM**:预先排好"第一轮返回 tool_calls、第二轮返回纯文本",
从而在不打真网、不花 token 的前提下断言循环的每一步。

为什么这类测试值得写:Agent 的 bug 大多不是"某个函数算错了",而是
"轮次没停下来"、"observation 没接回去"、"来源编号对不上"这类**编排错误**。
它们只在多轮交互中出现,单独测每个函数都是绿的。
"""

import asyncio
from typing import Any

import pytest

from app.core.exceptions import ConfigurationError
from app.services import llm
from app.services.rag import agent, tools


# ---------------------------------------------------------------------------
# 假 LLM 脚手架
# ---------------------------------------------------------------------------


class _FakeToolCall:
    def __init__(self, name: str, arguments: str, call_id: str = "call_1") -> None:
        self.id = call_id
        self.function = type("_Fn", (), {"name": name, "arguments": arguments})()


class _FakeMessage:
    def __init__(
        self, content: str = "", tool_calls: list[_FakeToolCall] | None = None
    ) -> None:
        self.content = content
        self.tool_calls = tool_calls


def _script(monkeypatch: pytest.MonkeyPatch, turns: list[_FakeMessage]) -> list[list]:
    """按顺序返回预设的每一轮响应,并记录每次收到的 messages。"""
    seen: list[list] = []
    queue = list(turns)

    async def fake_complete(messages, *, tools=None):
        seen.append([dict(m) for m in messages])
        return queue.pop(0) if queue else _FakeMessage(content="兜底")

    monkeypatch.setattr(llm, "complete", fake_complete)
    return seen


def _stream(monkeypatch: pytest.MonkeyPatch, text: str) -> list[list]:
    """假的流式综合调用,把文本一个字一个字吐出来。"""
    seen: list[list] = []

    async def fake_stream(messages):
        seen.append([dict(m) for m in messages])
        for char in text:
            yield char

    monkeypatch.setattr(llm, "stream_completion", fake_stream)
    return seen


def _fake_tools(monkeypatch: pytest.MonkeyPatch, results: dict[str, tools.ToolResult]):
    """按工具名返回预设结果,并记录调用顺序。"""
    calls: list[tuple[str, str | None]] = []

    async def fake_execute(name: str, raw_arguments: str | None) -> tools.ToolResult:
        calls.append((name, raw_arguments))
        return results.get(name, tools.ToolResult(text=f"{name} 没有预设结果"))

    monkeypatch.setattr(tools, "execute", fake_execute)
    return calls


# ---------------------------------------------------------------------------
# 工具注册表:契约一致性
# ---------------------------------------------------------------------------


def test_schema_and_executor_names_match() -> None:
    """function calling 的坑几乎都出在两边不一致上。

    schema 里声明了工具却没有执行器,模型调到它就是运行时 KeyError;
    反过来执行器存在但没声明,模型永远不会用。这条断言让不一致在 CI 就红。
    """
    schema_names = {s["function"]["name"] for s in tools.tool_schemas()}
    assert schema_names == set(tools.TOOLS)


def test_every_schema_declares_required_params() -> None:
    """漏了 required,模型会给出空参数调用,然后在执行器里才发现缺东西。"""
    for schema in tools.tool_schemas():
        params = schema["function"]["parameters"]
        assert params["required"], schema["function"]["name"]
        for name in params["required"]:
            assert name in params["properties"]


# ---------------------------------------------------------------------------
# 工具执行:永不抛异常
# ---------------------------------------------------------------------------


def test_unknown_tool_returns_hint_not_exception() -> None:
    """模型幻觉出不存在的工具名是真实会发生的,要告诉它有哪些可用。"""
    result = asyncio.run(tools.execute("summon_dragon", "{}"))

    assert "没有名为 summon_dragon 的工具" in result.text
    assert "search_guides" in result.text


@pytest.mark.parametrize("raw", ["", None, "{不是合法 JSON", "[1,2]", '"字符串"'])
def test_malformed_arguments_fall_back_to_empty(raw: Any) -> None:
    """参数 JSON 非法时不能抛 JSONDecodeError 让整个请求挂掉。"""
    assert tools.parse_arguments(raw) == {}


def test_search_error_becomes_observation(monkeypatch: pytest.MonkeyPatch) -> None:
    """搜索失败要降级成一条模型读得懂的话,而不是 5xx。

    这是 Agent 相比传统调用链的真实优势:模型可以换关键词重试。
    """

    async def boom(query: str, max_results: int | None = None):
        raise tools.search_provider.SearchError("搜索失败(TimeoutException)")

    monkeypatch.setattr(tools.search_provider, "search_guides", boom)

    result = asyncio.run(tools.execute("search_guides", '{"query": "纳塔"}'))

    assert "执行失败" in result.text
    assert "换个关键词" in result.text


def test_unexpected_exception_leaks_only_type_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """未预期异常的消息里可能有内部路径、SQL 片段,而 observation 会进上下文、
    最终可能被模型复述给用户。所以只留类型名。"""

    async def boom(query: str, max_results: int | None = None):
        raise RuntimeError("/Users/secret/path 数据库连接串 postgresql://u:p@h/db")

    monkeypatch.setattr(tools.search_provider, "search_guides", boom)

    result = asyncio.run(tools.execute("search_guides", '{"query": "x"}'))

    assert "RuntimeError" in result.text
    assert "postgresql://" not in result.text
    assert "/Users/secret" not in result.text


def test_missing_query_gives_actionable_message() -> None:
    result = asyncio.run(tools.execute("search_guides", "{}"))
    assert "缺少 query 参数" in result.text


def test_empty_search_result_suggests_shorter_keywords(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """MediaWiki 搜索对多词偏 AND,提示换更短的词比只说"没结果"有用。"""

    async def empty(query: str, max_results: int | None = None):
        return []

    monkeypatch.setattr(tools.search_provider, "search_guides", empty)

    result = asyncio.run(tools.execute("search_guides", '{"query": "纳塔 火神 突破材料"}'))

    assert "更短" in result.text
    assert result.candidates == []


def test_search_result_carries_structured_candidates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """结构给代码、文本给模型。这样 Agent 不必去解析自己刚生成的格式。"""
    from app.services.rag.search_provider import SearchHit

    async def hits(query: str, max_results: int | None = None):
        return [SearchHit(title="纳塔", url="https://w/ys/纳塔", snippet="国度")]

    monkeypatch.setattr(tools.search_provider, "search_guides", hits)

    result = asyncio.run(tools.execute("search_guides", '{"query": "纳塔"}'))

    assert result.candidates == [("纳塔", "https://w/ys/纳塔")]
    assert "https://w/ys/纳塔" in result.text


def test_observation_is_truncated(monkeypatch: pytest.MonkeyPatch) -> None:
    """工具结果会参与后续每一轮调用,不截断几次 fetch 就能顶满上下文。"""

    async def long_page(url: str, *, max_chars: int | None = None):
        return "神庙" * 20000

    monkeypatch.setattr(tools.page_fetcher, "fetch_page", long_page)

    result = asyncio.run(tools.execute("fetch_page", '{"url": "https://e.com/a"}'))

    assert "已截断" in result.text
    assert len(result.text) < 9000
    assert result.fetched_url == "https://e.com/a"


# ===== 本地语料检索工具 =====
#
# 这些用例都把嵌入和数据库替换成假实现:真实现依赖计费的嵌入 API 和 PG
# 向量算符,都不适合进单测。这里验证的是"工具契约"——参数处理、
# 来源标记、依赖不可用时的降级。


def _fake_corpus(monkeypatch: pytest.MonkeyPatch, rows: list[tuple[str, str, str]]):
    """替换掉 _run_corpus_search 里延迟导入的两个依赖。"""

    async def fake_embed(texts: list[str]) -> list[list[float]]:
        return [[0.1] * 4 for _ in texts]

    captured: dict = {}

    def fake_search(session, *, query_embedding, game_slug=None, top_k=5):
        captured["game_slug"] = game_slug
        captured["top_k"] = top_k

        class Row:
            def __init__(self, title: str, url: str, text: str) -> None:
                self.title = title
                self.source_url = url
                self.chunk_text = text

        return [Row(*row) for row in rows]

    monkeypatch.setattr("app.services.embedding.embed_texts", fake_embed)
    monkeypatch.setattr(
        "app.repositories.guide_chunk.search_similar", fake_search
    )
    return captured


def test_corpus_search_marks_chunks_as_fetched(monkeypatch: pytest.MonkeyPatch) -> None:
    """语料块的正文真的进了上下文,所以按「抓过」计——来源排序才准。"""
    _fake_corpus(
        monkeypatch,
        [("纳塔", "https://w/ys/纳塔", "纳塔是提瓦特的火之国。")],
    )

    result = asyncio.run(
        tools.execute("search_local_corpus", '{"query": "纳塔在哪"}')
    )

    assert result.candidates == [("纳塔", "https://w/ys/纳塔")]
    assert result.fetched_urls == ["https://w/ys/纳塔"]
    assert "纳塔是提瓦特的火之国" in result.text


def test_corpus_search_passes_game_filter(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = _fake_corpus(monkeypatch, [("刻晴", "https://w/ys/刻晴", "雷元素")])

    asyncio.run(
        tools.execute("search_local_corpus", '{"query": "刻晴", "game_slug": "ys"}')
    )

    assert captured["game_slug"] == "ys"


def test_corpus_search_empty_suggests_online_search(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """语料是快照,没覆盖到很正常;必须把模型引导到在线检索,而不是让它放弃。"""
    _fake_corpus(monkeypatch, [])

    result = asyncio.run(tools.execute("search_local_corpus", '{"query": "冷门内容"}'))

    assert "search_guides" in result.text


def test_corpus_search_missing_query_is_actionable() -> None:
    result = asyncio.run(tools.execute("search_local_corpus", "{}"))

    assert "query" in result.text


def test_corpus_search_degrades_when_embedding_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """没配嵌入 Key 时语料检索不可用,但不能让整个对话挂掉。"""

    async def no_key(texts: list[str]) -> list[list[float]]:
        raise ConfigurationError("未配置嵌入模型 API Key，语料向量检索暂不可用")

    monkeypatch.setattr("app.services.embedding.embed_texts", no_key)

    result = asyncio.run(tools.execute("search_local_corpus", '{"query": "纳塔"}'))

    assert "不可用" in result.text
    assert "其他检索工具" in result.text
