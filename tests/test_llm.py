"""大模型客户端的单元测试。

这个模块此前几乎没被测到(覆盖率 44%),原因是客户端在 import 期就被
构造成模块级单例——测试想换成假客户端,时机上根本插不进去。
改成惰性 `get_client()` 之后才有了这些用例。

顺带补上一条容易忽略的事实:`AsyncOpenAI(api_key="")` 的构造函数**不校验 key**,
所以"没配 key 也能 import 成功"是偶然而非保障,问题会被推迟到发请求时
以 401 的形式出现,看起来像上游故障。get_client() 显式挡住这种情况。
"""

import asyncio
from typing import Any

import pytest

from app.core.exceptions import ConfigurationError
from app.services import llm


@pytest.fixture(autouse=True)
def _clean_client():
    """每个用例前后都清掉模块级单例,避免用例之间互相污染。"""
    llm.reset_client_for_tests()
    yield
    llm.reset_client_for_tests()


class _FakeCompletions:
    """只实现被用到的那一个方法,不去模仿整个 SDK。"""

    def __init__(self, result: Any) -> None:
        self._result = result
        self.calls: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        return self._result


def _fake_client(result: Any) -> Any:
    completions = _FakeCompletions(result)
    chat = type("_Chat", (), {"completions": completions})()
    return type("_Client", (), {"chat": chat, "completions": completions})()


def _message(content: str | None) -> Any:
    """拼出 completion.choices[0].message.content 这条访问链。"""
    message = type("_Msg", (), {"content": content})()
    choice = type("_Choice", (), {"message": message})()
    return type("_Completion", (), {"choices": [choice]})()


def test_get_client_is_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    """连接池要复用:同一进程内只应创建一次客户端。"""
    monkeypatch.setattr(llm.settings, "llm_api_key", "sk-test")
    created: list[dict[str, Any]] = []

    def fake_ctor(**kwargs: Any) -> object:
        created.append(kwargs)
        return object()

    monkeypatch.setattr(llm, "AsyncOpenAI", fake_ctor)

    first = llm.get_client()
    second = llm.get_client()

    assert first is second
    assert len(created) == 1, "客户端被重复创建会白白重建 HTTP 连接池"


def test_get_client_raises_when_key_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    """缺配置必须显式失败,而不是留到发请求时报 401。"""
    monkeypatch.setattr(llm.settings, "llm_api_key", "")

    with pytest.raises(ConfigurationError) as excinfo:
        llm.get_client()

    # 503 的语义是"环境/配置问题",运维看到就知道去查部署配置。
    assert excinfo.value.status_code == 503
    # 给用户看的文案里不能出现 key、base_url 之类的内部信息。
    assert "sk-" not in excinfo.value.message


def test_ask_llm_returns_text_and_sends_system_prompt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _fake_client(_message("你好"))
    monkeypatch.setattr(llm, "get_client", lambda: fake)

    assert asyncio.run(llm.ask_llm("在吗")) == "你好"

    sent = fake.completions.calls[0]["messages"]
    assert sent[0]["role"] == "system"
    assert sent[1] == {"role": "user", "content": "在吗"}


def test_ask_llm_tolerates_null_content(monkeypatch: pytest.MonkeyPatch) -> None:
    """content 偶尔会是 None,返回值必须仍然是 str,否则上层拼接会炸。"""
    monkeypatch.setattr(llm, "get_client", lambda: _fake_client(_message(None)))

    assert asyncio.run(llm.ask_llm("在吗")) == ""


def _chunk(delta: str | None, *, empty_choices: bool = False) -> Any:
    if empty_choices:
        return type("_Chunk", (), {"choices": []})()
    inner = type("_Delta", (), {"content": delta})()
    choice = type("_Choice", (), {"delta": inner})()
    return type("_Chunk", (), {"choices": [choice]})()


class _FakeStream:
    def __init__(self, chunks: list[Any]) -> None:
        self._chunks = chunks

    def __aiter__(self) -> "_FakeStream":
        self._it = iter(self._chunks)
        return self

    async def __anext__(self) -> Any:
        try:
            return next(self._it)
        except StopIteration:
            raise StopAsyncIteration from None


def test_ask_llm_stream_skips_empty_frames(monkeypatch: pytest.MonkeyPatch) -> None:
    """流的首尾会出现 choices 为空、或 delta 为 None 的帧。

    不跳过 choices 为空的帧会直接 IndexError,这是真实踩过的坑;
    delta 为 None 的帧(只带角色或思考内容)也不该往外发空串。
    """
    chunks = [
        _chunk(None, empty_choices=True),
        _chunk(None),
        _chunk("你"),
        _chunk(""),
        _chunk("好"),
        _chunk(None, empty_choices=True),
    ]
    monkeypatch.setattr(llm, "get_client", lambda: _fake_client(_FakeStream(chunks)))

    async def collect() -> list[str]:
        return [delta async for delta in llm.ask_llm_stream("在吗")]

    assert asyncio.run(collect()) == ["你", "好"]


def test_ask_llm_stream_requests_streaming(monkeypatch: pytest.MonkeyPatch) -> None:
    """漏了 stream=True 就退化成一次性返回,前端拿不到逐字效果。"""
    fake = _fake_client(_FakeStream([_chunk("x")]))
    monkeypatch.setattr(llm, "get_client", lambda: fake)

    async def drain() -> None:
        async for _ in llm.ask_llm_stream("在吗"):
            pass

    asyncio.run(drain())

    assert fake.completions.calls[0]["stream"] is True


# ---------------------------------------------------------------------------
# 给 Agent 用的低层入口
# ---------------------------------------------------------------------------


def test_complete_passes_messages_verbatim(monkeypatch: pytest.MonkeyPatch) -> None:
    """Agent 需要自己掌控完整 messages(要往里追加 tool_calls 和 observation),
    所以这层不能偷偷改内容——比如不能自作主张插入 SYSTEM_PROMPT。"""
    fake = _fake_client(_message("回复"))
    monkeypatch.setattr(llm, "get_client", lambda: fake)
    messages = [
        {"role": "system", "content": "自定义人设"},
        {"role": "user", "content": "问题"},
    ]

    asyncio.run(llm.complete(messages))

    assert fake.completions.calls[0]["messages"] == messages
    assert "tools" not in fake.completions.calls[0]


def test_complete_with_tools_uses_auto_choice(monkeypatch: pytest.MonkeyPatch) -> None:
    """tool_choice 必须是 auto 而不是强制调工具。

    闲聊问题被强制走一遍检索,既慢又会答得很怪——是否需要检索该由模型判断。
    """
    fake = _fake_client(_message("回复"))
    monkeypatch.setattr(llm, "get_client", lambda: fake)
    schemas = [{"type": "function", "function": {"name": "t"}}]

    asyncio.run(llm.complete([{"role": "user", "content": "x"}], tools=schemas))

    call = fake.completions.calls[0]
    assert call["tools"] == schemas
    assert call["tool_choice"] == "auto"


def test_complete_returns_raw_message_object(monkeypatch: pytest.MonkeyPatch) -> None:
    """返回 SDK 原对象而不是自己的类型:Agent 要把这条 assistant 消息原样
    塞回 messages(协议要求 tool_calls 与 tool 结果严格配对)。"""
    expected = _message("回复")
    monkeypatch.setattr(llm, "get_client", lambda: _fake_client(expected))

    result = asyncio.run(llm.complete([{"role": "user", "content": "x"}]))

    assert result is expected.choices[0].message


def test_stream_completion_yields_text(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _fake_client(_FakeStream([_chunk("你"), _chunk(None), _chunk("好")]))
    monkeypatch.setattr(llm, "get_client", lambda: fake)

    async def collect() -> list[str]:
        return [d async for d in llm.stream_completion([{"role": "user", "content": "x"}])]

    assert asyncio.run(collect()) == ["你", "好"]
    assert fake.completions.calls[0]["stream"] is True
