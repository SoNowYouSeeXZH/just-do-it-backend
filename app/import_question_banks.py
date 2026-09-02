"""开源题库导入管道。

用法:
    python -m app.import_question_banks --job frontend            # 导入前端题库
    python -m app.import_question_banks --job frontend --dry-run  # 只解析校验,不写库
    python -m app.import_question_banks --job frontend --source <md路径>  # 解析本地文件

设计(见 .comate/specs/game-guide-rag-agent/doc.md 的 Track C):
- 前端:lydiahallie/javascript-questions(MIT 协议)中文版,本身就是
  「题干+选项+答案+解析」的 MCQ 结构,纯文本解析,零 LLM 参与;
  每题 source_url 填仓库地址,溯源与版权归属一眼可查。
- 写库直接调 services.question_bank.batch_create_questions——与
  POST /api/admin/questions/batch 是同一段代码(逐题校验 + content_hash
  去重 + SAVEPOINT 部分成功),只省去起服务的一跳。
- 取数用浅克隆而不是 raw 下载:本机网络对 raw.githubusercontent.com
  不通、对 github.com 主站可达,git clone 走主站域名。

为什么解析器是纯函数:导入正确性的关键在「四要素抽取」(题干/选项/
答案/解析),纯函数意味着可以用固定的 markdown 样例做单测,不依赖网络
与数据库。
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
import tempfile
from collections.abc import Iterator
from pathlib import Path

from sqlmodel import Session

from app.db import engine
from app.schemas.question_bank import BatchQuestionsRequest, QuestionInput
from app.services.question_bank import batch_create_questions, validate_question

JSQ_REPO_URL = "https://github.com/lydiahallie/javascript-questions"
JSQ_ZH_PATH = "zh-CN/README-zh_CN.md"
# 批量接口单批上限(BatchQuestionsRequest.max_length=100),脚本自动分批
BATCH_SIZE = 100

# 题目标题行:###### 12. 输出是什么?(部分标题带 <a name=xxx></a> 锚点前缀)
_HEADING_RE = re.compile(r"^######\s+(?:<a[^>]*>\s*</a>)?\d+\.\s*(?P<title>.*)$")
# 选项行:- A: xxx(兼容全角冒号)
_OPTION_RE = re.compile(r"^- (?P<letter>[A-F])[：:]\s*(?P<text>.*)$")
# 答案行:#### 答案：B(全角冒号为主;字母可多个 → 多选题)
_ANSWER_RE = re.compile(
    r"^####\s*答案[：:]\s*(?P<letters>[A-F](?:\s*[,，、]\s*[A-F])*)\s*$"
)
# 解析里的行内 HTML 标签(仓库正文用 <i>/<code> 等做强调)
_HTML_TAG_RE = re.compile(r"</?(?:i|b|em|strong|code|pre|sub|sup)>")


def _split_blocks(markdown: str) -> Iterator[list[str]]:
    """按 ###### 标题切块。首个标题之前的内容(TOC/简介)自动丢弃。"""
    current: list[str] = []
    for line in markdown.splitlines():
        if line.startswith("###### "):
            if current:
                yield current
            current = [line]
        elif current:
            current.append(line)
    if current:
        yield current


def _parse_block(
    lines: list[str], source_url: str
) -> tuple[QuestionInput | None, str | None]:
    """解析单个题目块,返回 (题目, 跳过原因)——二者互斥。

    结构约定(与仓库真实格式一一对应):
      ###### N. 题干
      ```javascript 可选代码块```
      - A: 选项 ... - D: 选项
      <details><summary>答案</summary><p>
      #### 答案：X
      解析文本(可含代码块)
      </p></details>
    """
    heading = _HEADING_RE.match(lines[0])
    if heading is None or not heading.group("title").strip():
        return None, "题干缺失或为空"

    title_lines: list[str] = [heading.group("title").strip()]
    code_lines: list[str] = []
    options: dict[str, str] = {}
    answer_letters: list[str] | None = None
    explanation_lines: list[str] = []
    in_code = False
    in_explanation = False

    for line in lines[1:]:
        stripped = line.strip()

        # 答案行:进入解析区
        if answer_letters is None and not in_code:
            match = _ANSWER_RE.match(stripped)
            if match:
                answer_letters = re.findall(r"[A-F]", match.group("letters"))
                in_explanation = True
                continue

        if in_explanation:
            if stripped in ("</p>", "</details>"):
                in_explanation = False  # 解析区结束,后面的分隔线等忽略
                continue
            explanation_lines.append(line)
            continue

        # 答案之前:代码块围栏 / 代码内容 / 选项 / 题干续行,按此优先级判断。
        # 顺序不能乱:in_code 的判定必须先于「题干续行」分支,
        # 否则代码行会被当成题干(选项还没出现)。
        if stripped.startswith("```"):
            in_code = not in_code
            code_lines.append(stripped)
            continue
        if in_code:
            code_lines.append(line)
            continue
        option = _OPTION_RE.match(stripped)
        if option:
            options[option.group("letter")] = option.group("text").strip()
            continue
        if not stripped:
            continue
        if not options:
            title_lines.append(stripped)
        # 选项之后、答案之前的非空行不属于任何区(罕见),忽略

    if not options or len(options) < 2:
        return None, "选项不足 2 个"
    if not answer_letters:
        return None, "答案行缺失"
    letters = sorted(set(answer_letters))
    if any(letter not in options for letter in letters):
        return None, f"答案字母 {letters} 超出选项范围 {sorted(options)}"

    prompt_parts: list[str] = []
    if code_lines:
        prompt_parts.append("\n".join(title_lines))
        prompt_parts.append("\n".join(code_lines))
    else:
        prompt_parts.append("\n".join(title_lines))
    explanation = _HTML_TAG_RE.sub("", " ".join(explanation_lines)).strip()

    return (
        QuestionInput(
            qtype="multi" if len(letters) > 1 else "single",
            prompt="\n\n".join(prompt_parts),
            options=[options[k] for k in sorted(options)],
            answer_indices=[sorted(options).index(k) for k in letters],
            explanation=explanation,
            source_url=source_url,
        ),
        None,
    )


def parse_javascript_questions(
    markdown: str, source_url: str = JSQ_REPO_URL
) -> tuple[list[QuestionInput], list[str]]:
    """解析 lydiahallie 中文版 README,返回 (题目列表, 跳过原因列表)。"""
    questions: list[QuestionInput] = []
    skips: list[str] = []
    for index, block in enumerate(_split_blocks(markdown), start=1):
        question, reason = _parse_block(block, source_url)
        if question is None:
            title = block[0].lstrip("# ").strip()
            skips.append(f"第 {index} 题[{title[:30]}]: {reason}")
        else:
            # 结构解析成功 ≠ 业务校验通过:超长(题干>512/解析>1024)在这里拦下
            invalid = validate_question(question)
            if invalid:
                title = block[0].lstrip("# ").strip()
                skips.append(f"第 {index} 题[{title[:30]}]: {invalid}")
            else:
                questions.append(question)
    return questions, skips


def _fetch_markdown(source: str | None) -> str:
    """取题库原文:--source 指定本地文件,否则浅克隆仓库。"""
    if source:
        return Path(source).read_text(encoding="utf-8")
    with tempfile.TemporaryDirectory() as tmp:
        target = str(Path(tmp) / "repo")
        subprocess.run(
            ["git", "clone", "--depth", "1", JSQ_REPO_URL, target],
            check=True,
            capture_output=True,
        )
        return (Path(target) / JSQ_ZH_PATH).read_text(encoding="utf-8")


def import_frontend(session: Session, *, dry_run: bool, source: str | None) -> None:
    markdown = _fetch_markdown(source)
    questions, skips = parse_javascript_questions(markdown)

    print(f"=== 前端题库解析结果({JSQ_REPO_URL}) ===")
    print(f"解析成功: {len(questions)} 道;跳过: {len(skips)} 道")
    if skips:
        for skip in skips[:10]:
            print(f"  - {skip}")
        if len(skips) > 10:
            print(f"  ... 其余 {len(skips) - 10} 条略")
    if dry_run:
        print("(--dry-run:未写库)")
        return

    created = skipped = failed = 0
    for start in range(0, len(questions), BATCH_SIZE):
        chunk = questions[start : start + BATCH_SIZE]
        response = batch_create_questions(
            session,
            BatchQuestionsRequest(job_id="frontend", questions=chunk),
        )
        created += response.created
        skipped += response.skipped
        failed += response.failed
        for result in response.results:
            if result.status == "failed":
                print(f"  - 批内第 {result.index} 题失败: {result.reason}")
    print(f"导入完成:写入 {created} / 重复跳过 {skipped} / 失败 {failed}")


def main() -> None:
    parser = argparse.ArgumentParser(description="开源题库导入管道")
    parser.add_argument("--job", choices=["frontend"], required=True)
    parser.add_argument("--dry-run", action="store_true", help="只解析校验,不写库")
    parser.add_argument("--source", help="本地 markdown 路径(默认浅克隆仓库)")
    args = parser.parse_args()

    with Session(engine) as session:
        if args.job == "frontend":
            import_frontend(session, dry_run=args.dry_run, source=args.source)

    return None


if __name__ == "__main__":
    main()
