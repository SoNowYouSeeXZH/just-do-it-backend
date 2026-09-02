"""开源题库导入管道。

用法:
    python -m app.import_question_banks --job frontend            # 前端:直导 lydiahallie
    python -m app.import_question_banks --job backend             # 后端:LLM 改编 JavaGuide
    python -m app.import_question_banks --job backend --dry-run   # 只解析改编校验,不写库
    python -m app.import_question_banks --job backend --repo-dir /tmp/javaguide  # 用本地克隆
    python -m app.import_question_banks --job backend --limit 40  # 只取前 40 个问答对

设计(见 .comate/specs/game-guide-rag-agent/doc.md 的 Track C):
- 前端:lydiahallie/javascript-questions(MIT 协议)中文版,本身就是
  「题干+选项+答案+解析」的 MCQ 结构,纯文本解析,零 LLM 参与;
  每题 source_url 填仓库地址,溯源与版权归属一眼可查。
- 后端:JavaGuide 的问答体八股(开源仓库,Apache-2.0)由 LLM 做**有锚点改编**
  ——题干与正确答案来自原文,LLM 只负责压缩题干、生成干扰项、浓缩解析。
  「LLM 当改编者而不是出题者」是整个管道的质量底线:答案有出处,
  幻觉风险被压到「干扰项可能不够好」这个量级,而不是「答案可能是错的」。
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
import asyncio
import dataclasses
import json
import re
import subprocess
import sys
import tempfile
from collections.abc import Iterator
from pathlib import Path

from openai import AsyncOpenAI
from sqlmodel import Session

from app.config import settings
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


# ===== 后端轨道:JavaGuide 问答 → LLM 有锚点改编 =====

JAVAGUIDE_REPO_URL = "https://github.com/Snailclimb/JavaGuide"
# 圈定通用章节的问答集(MySQL/Redis/网络——「后端工程师」定位下的通用主题,
# 不含 Java 语言特有内容)。文件路径相对仓库根。
JAVAGUIDE_QA_FILES = [
    "docs/database/mysql/mysql-questions-01.md",
    "docs/database/redis/redis-questions-01.md",
    "docs/database/redis/redis-questions-02.md",
    "docs/cs-basics/network/other-network-questions.md",
    "docs/cs-basics/network/other-network-questions2.md",
]
# 一次 LLM 调用塞多少个问答对:太多会稀释注意力,太少浪费调用次数
ADAPT_BATCH_SIZE = 8
# 喂给 LLM 的参考答案上限:原文动辄数千字,截断后足够改编一道选择题
ANSWER_MAX_CHARS = 3500

# 问答标题行:### 什么是 Redis?(⭐️ 等装饰去掉,标题以问号结尾才算问答)
_QA_HEADING_RE = re.compile(r"^###\s+(?P<title>.+?[？?])\s*$")
# 答案清洗:图片行整个删掉,超链接保留文字,行内 HTML 去标签
_IMAGE_RE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_LINK_RE = re.compile(r"\[([^\]]*)\]\([^)]*\)")


@dataclasses.dataclass
class QAItem:
    """一组待改编的问答对,source_url 指向锚点文档。"""

    question: str
    answer: str
    source_url: str


def extract_qa_pairs(markdown: str, source_url: str) -> list[QAItem]:
    """从 JavaGuide 问答集 markdown 里抽取「问题 + 参考答案」对。

    只认以问号结尾的 ### 标题——「常见网络协议」这类话题型标题不是问题,
    混进来只会产出陈述句式的伪选择题。答案取到下一个同级或更高级标题为止。
    """
    items: list[QAItem] = []
    title: str | None = None
    body: list[str] = []

    def _flush() -> None:
        nonlocal title, body
        if title is not None:
            answer = "\n".join(body).strip()
            answer = _IMAGE_RE.sub("", answer)
            answer = _LINK_RE.sub(r"\1", answer)
            answer = _HTML_TAG_RE.sub("", answer)
            # 去掉连续空行(图片删除后常留大片空白)
            answer = re.sub(r"\n{3,}", "\n\n", answer).strip()
            if answer:
                items.append(
                    QAItem(
                        question=title,
                        answer=answer[:ANSWER_MAX_CHARS],
                        source_url=source_url,
                    )
                )
        title, body = None, []

    for line in markdown.splitlines():
        heading = _QA_HEADING_RE.match(line)
        if heading:
            _flush()
            title = heading.group("title").strip("⭐️ ").strip()
            continue
        # 遇到更高级标题(## 一级分类),当前问答结束
        if title is not None and re.match(r"^#{1,2}\s", line):
            _flush()
            continue
        if title is not None:
            body.append(line)
    _flush()
    return items


_ADAPT_SYSTEM_PROMPT = """你是资深后端面试官兼题库编辑。用户会给你若干组「面试问题 + 参考答案」,
请把每一组改编成一道四选一的单选题。严格遵守:

1. prompt:基于原问题改写为独立完整的题干,不超过 120 字;
2. options:恰好 4 个选项。正确选项的内容必须来自参考答案,不许自行发挥;
   3 个干扰项要有迷惑性但明确错误(概念混淆、张冠李戴是好干扰项,
   「以上都对」这类选项禁止出现);
3. answer_index:正确选项的下标(0-3),在四个位置间随机分布;
4. explanation:不超过 300 字,说清正确项为什么对,并点出干扰项错在哪;
5. 如果该问题本质是开放设计题或需要写代码,不适合改编为选择题,该项返回 null。

输出必须是 JSON 数组,与输入顺序一一对应,每项形如
{"prompt": "...", "options": ["...", "...", "...", "..."], "answer_index": 0, "explanation": "..."}
不适合改编的项为 null。不要输出 JSON 以外的任何内容。"""

_llm_client: AsyncOpenAI | None = None


def _get_llm_client() -> AsyncOpenAI:
    """脚本自用的 LLM 客户端(与 app.services.llm 全局客户端同配置)。

    独立持有而不是复用,是因为导入脚本的调用模式(低频、大 prompt、
    长超时)和在线聊天不同,不想互相干扰连接池参数。测试里 monkeypatch
    这个函数注入假客户端,不打真网。
    """
    global _llm_client
    if _llm_client is None:
        _llm_client = AsyncOpenAI(
            api_key=settings.llm_api_key,
            base_url=settings.llm_base_url,
            timeout=120,
        )
    return _llm_client


def _parse_adapt_response(content: str) -> list[dict | None]:
    """解析 LLM 的改编输出:容忍代码围栏与前后缀噪声,切出 JSON 数组。"""
    text = content.strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    start, end = text.find("["), text.rfind("]")
    if start == -1 or end == -1 or end <= start:
        raise ValueError(f"LLM 输出中找不到 JSON 数组: {content[:200]}")
    parsed = json.loads(text[start : end + 1])
    if not isinstance(parsed, list):
        raise ValueError("LLM 输出的顶层不是 JSON 数组")
    return parsed


async def adapt_qa_batch(batch: list[QAItem]) -> list[dict | None]:
    """把一批问答对交给 LLM 改编,返回与输入等长的结果列表(null=不适合)。"""
    payload = [
        {"question": item.question, "reference_answer": item.answer}
        for item in batch
    ]
    user_prompt = json.dumps(payload, ensure_ascii=False)
    last_error: Exception | None = None
    for attempt in range(2):  # 失败重试一次:网关偶发抖动不值得整批放弃
        try:
            response = await _get_llm_client().chat.completions.create(
                model=settings.llm_model,
                messages=[
                    {"role": "system", "content": _ADAPT_SYSTEM_PROMPT},
                    {"role": "user", "content": user_prompt},
                ],
            )
            parsed = _parse_adapt_response(response.choices[0].message.content or "")
            if len(parsed) != len(batch):
                raise ValueError(f"LLM 返回 {len(parsed)} 项,期望 {len(batch)} 项")
            return parsed
        except Exception as exc:  # noqa: BLE001 —— 重试后仍失败则整批置 null
            last_error = exc
            if attempt == 0:
                await asyncio.sleep(2)
    print(f"  ⚠️ 一批 {len(batch)} 个问答改编失败({last_error}),已跳过")
    return [None] * len(batch)


def build_backend_questions(
    pairs: list[tuple[QAItem, dict | None]]
) -> tuple[list[QuestionInput], list[str]]:
    """把 LLM 改编产物组装成 QuestionInput,并做与批量接口同规则的校验。"""
    questions: list[QuestionInput] = []
    skips: list[str] = []
    for qa, item in pairs:
        if item is None:
            skips.append(f"[{qa.question[:30]}] LLM 判定不适合改编为选择题")
            continue
        try:
            question = QuestionInput(
                qtype="single",
                prompt=str(item["prompt"]),
                options=[str(option) for option in item["options"]],
                answer_indices=[int(item["answer_index"])],
                explanation=str(item["explanation"]),
                source_url=qa.source_url,
            )
        except (KeyError, TypeError, ValueError) as exc:
            skips.append(f"[{qa.question[:30]}] 改编产物字段缺失/类型错误: {exc}")
            continue
        invalid = validate_question(question)
        if invalid:
            skips.append(f"[{qa.question[:30]}] {invalid}")
            continue
        questions.append(question)
    return questions, skips


async def import_backend(
    session: Session, *, dry_run: bool, repo_dir: str | None, limit: int | None
) -> None:
    # ---- 取数 ----
    base = Path(repo_dir) if repo_dir else None
    tmp_ctx = tempfile.TemporaryDirectory()
    with tmp_ctx as tmp:
        if base is None:
            subprocess.run(
                ["git", "clone", "--depth", "1", JAVAGUIDE_REPO_URL, str(Path(tmp) / "repo")],
                check=True,
                capture_output=True,
            )
            base = Path(tmp) / "repo"
        qa_items: list[QAItem] = []
        for rel_path in JAVAGUIDE_QA_FILES:
            markdown = (base / rel_path).read_text(encoding="utf-8")
            source_url = f"{JAVAGUIDE_REPO_URL}/blob/main/{rel_path}"
            qa_items.extend(extract_qa_pairs(markdown, source_url))
    if limit:
        qa_items = qa_items[:limit]

    print(f"=== 后端题库改编({JAVAGUIDE_REPO_URL}) ===")
    print(f"抽取问答对: {len(qa_items)} 组")

    # ---- 改编 ----
    pairs: list[tuple[QAItem, dict | None]] = []
    for start in range(0, len(qa_items), ADAPT_BATCH_SIZE):
        batch = qa_items[start : start + ADAPT_BATCH_SIZE]
        pairs.extend(zip(batch, await adapt_qa_batch(batch)))
        print(f"  改编进度: {min(start + ADAPT_BATCH_SIZE, len(qa_items))}/{len(qa_items)}")

    questions, skips = build_backend_questions(pairs)
    print(f"改编通过: {len(questions)} 道;跳过: {len(skips)} 道")
    for skip in skips[:10]:
        print(f"  - {skip}")
    if len(skips) > 10:
        print(f"  ... 其余 {len(skips) - 10} 条略")
    if dry_run:
        print("(--dry-run:未写库)")
        return

    # ---- 入库 ----
    created = skipped = failed = 0
    for start in range(0, len(questions), BATCH_SIZE):
        chunk = questions[start : start + BATCH_SIZE]
        response = batch_create_questions(
            session,
            BatchQuestionsRequest(job_id="backend", questions=chunk),
        )
        created += response.created
        skipped += response.skipped
        failed += response.failed
        for result in response.results:
            if result.status == "failed":
                print(f"  - 批内第 {result.index} 题失败: {result.reason}")
    print(f"导入完成:写入 {created} / 重复跳过 {skipped} / 失败 {failed}")


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
    parser.add_argument("--job", choices=["frontend", "backend"], required=True)
    parser.add_argument("--dry-run", action="store_true", help="只解析校验,不写库")
    parser.add_argument("--source", help="前端:本地 markdown 路径(默认浅克隆仓库)")
    parser.add_argument("--repo-dir", help="后端:本地 JavaGuide 克隆目录(默认浅克隆)")
    parser.add_argument("--limit", type=int, help="后端:只取前 N 个问答对(调试用)")
    args = parser.parse_args()

    if args.job == "frontend":
        with Session(engine) as session:
            import_frontend(session, dry_run=args.dry_run, source=args.source)
    else:
        with Session(engine) as session:
            asyncio.run(
                import_backend(
                    session,
                    dry_run=args.dry_run,
                    repo_dir=args.repo_dir,
                    limit=args.limit,
                )
            )


if __name__ == "__main__":
    main()
