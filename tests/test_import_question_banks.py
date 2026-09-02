"""开源题库导入管道的解析器单测。

解析器是纯函数,用固定的 markdown 样例验证「四要素抽取」
(题干/选项/答案/解析)——不依赖网络与数据库。
样例结构与 lydiahallie/javascript-questions 中文版真实格式一一对应,
含边角情况:带锚点的标题、代码块、行内 HTML、全角冒号。

后端轨道(JavaGuide → LLM 改编)同样只测纯逻辑:问答抽取、
改编产物组装校验、LLM 输出解析——LLM 调用本身不打真网。
"""

from types import SimpleNamespace
from typing import Any

import asyncio

import pytest

from app import import_question_banks as pipeline
from app.import_question_banks import (
    QAItem,
    build_backend_questions,
    extract_qa_pairs,
    parse_javascript_questions,
)

SOURCE = "https://example.com/repo"

FIXTURE = """# 标题与目录(应被丢弃)

- [1. xxx](#1)

---

###### 1. 输出是什么？

```javascript
console.log(1)
```

- A: `1`
- B: `2`

<details><summary><b>答案</b></summary>
<p>

#### 答案：A

console.log 直接打印字面量 `1`。

</p>
</details>

---

###### <a name=20190629></a>2. 下列哪个说法正确？

- A: 甲
- B: 乙
- C: 丙

<details><summary><b>答案</b></summary>
<p>

#### 答案：C

<i>丙</i> 是正确的。行内 HTML 标签应被清掉。

</p>
</details>

---

###### 3. 多选题样例？

- A: 甲
- B: 乙
- C: 丙

<details><summary><b>答案</b></summary>
<p>

#### 答案：A,C

两个都对。

</p>
</details>

---

###### 4. 缺答案的坏题？

- A: 甲
- B: 乙

<details><summary><b>答案</b></summary>
<p>

这里没有答案行。

</p>
</details>

---
"""


def test_parses_standard_question_with_code() -> None:
    questions, skips = parse_javascript_questions(FIXTURE, SOURCE)
    assert not any("第 1 题" in s for s in skips)
    first = questions[0]
    # 题干 = 标题 + 代码块,围栏顺序正确(标题在前、围栏成对)
    assert first.prompt.startswith("输出是什么？\n\n```javascript")
    assert first.prompt.rstrip().endswith("```")
    assert "console.log(1)" in first.prompt
    assert first.options == ["`1`", "`2`"]
    assert first.answer_indices == [0]
    assert first.qtype == "single"
    assert "字面量" in first.explanation
    assert first.source_url == SOURCE


def test_anchor_prefixed_heading_and_html_stripped() -> None:
    questions, _ = parse_javascript_questions(FIXTURE, SOURCE)
    second = questions[1]
    assert second.prompt == "下列哪个说法正确？"
    assert second.options == ["甲", "乙", "丙"]
    assert second.answer_indices == [2]
    # 行内 HTML 标签(<i>)应被清理,内容保留
    assert "<i>" not in second.explanation
    assert "丙" in second.explanation


def test_multi_letter_answer_becomes_multi_choice() -> None:
    questions, _ = parse_javascript_questions(FIXTURE, SOURCE)
    third = [q for q in questions if q.prompt.startswith("多选题")][0]
    assert third.qtype == "multi"
    assert third.answer_indices == [0, 2]


def test_block_without_answer_is_skipped_with_reason() -> None:
    _, skips = parse_javascript_questions(FIXTURE, SOURCE)
    assert any("第 4 题" in s and "答案行缺失" in s for s in skips)


def test_overlong_explanation_is_rejected_by_validation() -> None:
    # 解析后的业务校验(解析>1024)应把超长题拦在导入前
    overlong = FIXTURE.replace(
        "console.log 直接打印字面量 `1`。",
        "超长解析。" * 300,
    )
    _, skips = parse_javascript_questions(overlong, SOURCE)
    assert any("第 1 题" in s for s in skips)


# ===== 后端轨道:JavaGuide 问答抽取与 LLM 改编产物组装 =====

JAVAGUIDE_FIXTURE = """---
title: 问答集
---

## Redis 基础

### 什么是 Redis？

Redis 是一个基于内存的键值数据库。参考 [官方文档](https://redis.io/docs)。

![架构图](https://example.com/arch.png)

### ⭐️Redis 为什么这么快？

内存存储 + 单线程避免锁竞争 + IO 多路复用。

### 常见网络协议

这一节是话题型标题,不是问题,不应被抽取。

## HTTP

### GET 和 POST 的区别是什么？

GET 幂等且参数在 URL,POST 非幂等且参数在请求体。
"""


def test_extract_qa_pairs_only_takes_question_headings() -> None:
    items = extract_qa_pairs(JAVAGUIDE_FIXTURE, "https://example.com/qa.md")
    # 话题型标题「常见网络协议」和 frontmatter 不算问答
    assert [item.question for item in items] == [
        "什么是 Redis？",
        "Redis 为什么这么快？",  # ⭐️ 装饰被剥掉
        "GET 和 POST 的区别是什么？",
    ]
    # 答案清洗:超链接保留文字、图片删除、⭐️ 不残留
    assert "官方文档" in items[0].answer
    assert "example.com" not in items[0].answer
    assert "内存的键值数据库" in items[0].answer
    assert "IO 多路复用" in items[1].answer
    assert items[0].source_url == "https://example.com/qa.md"


def test_build_backend_questions_handles_null_and_invalid() -> None:
    qa = QAItem(
        question="什么是 Redis？",
        answer="内存键值数据库",
        source_url="https://example.com/qa.md",
    )
    good = {
        "prompt": "Redis 是什么类型的数据库？",
        "options": ["内存键值", "磁盘文档", "图数据库", "时序数据库"],
        "answer_index": 0,
        "explanation": "Redis 基于内存,以键值形式存储。",
    }
    bad_index = dict(good, answer_index=9)  # 越界
    missing = {"prompt": "缺字段的题"}  # 缺 options/explanation

    questions, skips = build_backend_questions(
        [(qa, good), (qa, None), (qa, bad_index), (qa, missing)]
    )
    assert len(questions) == 1
    assert questions[0].prompt == "Redis 是什么类型的数据库？"
    assert questions[0].answer_indices == [0]
    assert questions[0].source_url == qa.source_url
    assert len(skips) == 3
    assert any("不适合" in s for s in skips)
    assert any("越界" in s for s in skips)
    assert any("字段缺失" in s for s in skips)


def _fake_client(payload: str) -> Any:
    """伪造 AsyncOpenAI 客户端:只实现本管道用到的 create 调用。"""

    class _Completions:
        @staticmethod
        async def create(**_: Any) -> Any:
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content=payload))]
            )

    return SimpleNamespace(chat=SimpleNamespace(completions=_Completions()))


def test_adapt_qa_batch_parses_fenced_json(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = """```json
[{"prompt": "题干", "options": ["甲", "乙", "丙", "丁"], "answer_index": 2, "explanation": "解析"}, null]
```"""
    monkeypatch.setattr(pipeline, "_get_llm_client", lambda: _fake_client(payload))
    batch = [
        QAItem("问一？", "答一", "https://e.com/1"),
        QAItem("问二？", "答二", "https://e.com/2"),
    ]
    results = asyncio.run(pipeline.adapt_qa_batch(batch))
    assert results[0] is not None and results[0]["answer_index"] == 2
    assert results[1] is None


def test_adapt_qa_batch_falls_back_to_null_after_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _BrokenCompletions:
        @staticmethod
        async def create(**_: Any) -> Any:
            raise RuntimeError("网关抖动")

    monkeypatch.setattr(
        pipeline,
        "_get_llm_client",
        lambda: SimpleNamespace(chat=SimpleNamespace(completions=_BrokenCompletions)),
    )
    batch = [QAItem("问？", "答", "https://e.com")]
    assert asyncio.run(pipeline.adapt_qa_batch(batch)) == [None]
