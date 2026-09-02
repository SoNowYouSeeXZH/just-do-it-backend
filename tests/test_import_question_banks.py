"""开源题库导入管道的解析器单测。

解析器是纯函数,用固定的 markdown 样例验证「四要素抽取」
(题干/选项/答案/解析)——不依赖网络与数据库。
样例结构与 lydiahallie/javascript-questions 中文版真实格式一一对应,
含边角情况:带锚点的标题、代码块、行内 HTML、全角冒号。
"""

from app.import_question_banks import parse_javascript_questions

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
