"""语料清洗与分块(wikitext -> 嵌入用文本)。

为什么单独一个纯函数模块:清洗/分块规则一定会反复调(嵌坏语料很难事后发现),
必须能离线单测;而爬取是 IO,两者混在一起就没法只测逻辑。

分块策略:先按 wikitext 的章节标题(== 二级标题 ==)切节,
每节内部再按段落聚合成不超过 max_chars 的块,块前缀「页面标题 · 小节标题」。
前缀不是装饰:嵌入没有"上下文"概念,一块写着"生命值 1200,攻击力 34"的文本,
不带标题根本不知道说的是哪个角色。
"""

from __future__ import annotations

import re

# 章节标题:行首 2~6 个等号包裹,如 "== 配队推荐 =="。一级(=)和超过六级的忽略:
# 一级一般是页面自己的标题,七级以上基本是乱写。
_HEADING = re.compile(r"^(={2,6})\s*(.+?)\s*\1\s*$", re.MULTILINE)

# HTML 注释与 <ref> 引用:对攻略语义没贡献,还常塞着超长 URL。
_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)
_REF = re.compile(r"<ref[^>]*/>|<ref[^>]*>.*?</ref>", re.DOTALL | re.IGNORECASE)
# 其余 HTML 标签(<div> <br> <span>…)。wiki 页里嵌 HTML 很常见,
# 留着就是一堆尖括号噪声;必须在 <ref> 之后删,避免把 ref 的内容漏出来。
_HTML_TAG = re.compile(r"<[^>]+>")

# 表格。攻略页里的表格多是数据表(伤害/掉落),转成纯文本会变成一堆竖线噪声,
# 直接丢弃——语义信息由表格外的正文承载。
_TABLE = re.compile(r"\{\|.*?\|\}", re.DOTALL)

# 内链 [[目标|显示]] / [[目标]],取"显示"(更接近人读到的文字)。
# 命名空间前缀(如 [[分类:xx]])整条丢弃。
_LINK = re.compile(r"\[\[(?:[^|\]]*\|)?([^\]]+)\]\]")

# 外链 [https://... 标签] -> 标签;裸 URL 丢弃(占 token 且无语义)。
_EXT_LINK = re.compile(r"\[https?://\S+\s+([^\]]+)\]")
_BARE_URL = re.compile(r"https?://\S+")

# 强调与排版标记。
_BOLD_ITALIC = re.compile(r"'{2,5}")
# 模板 {{...}}。嵌套时一次正则删不干净(外层会先匹配),迭代删到稳定为止。
_TEMPLATE = re.compile(r"\{\{[^{}]*\}\}")

# MediaWiki 魔法变量/标签残余。
_MAGIC = re.compile(r"__\w+__|<nowiki\s*/>|</?nowiki>", re.IGNORECASE)

# 图片/文件引用残渣:`thumb|240px|阿贝多官方故事图` 这类。
# 内链正则会把 [[文件:x.png|thumb|240px|说明]] 的最后一段留下来,
# 剩下的排版参数就成了噪声——语义为零,还会把"官方故事图"带进检索。
_IMAGE_RESIDUE = re.compile(
    r"^\s*(?:thumb|left|right|center|\d+px)\b.*$", re.MULTILINE | re.IGNORECASE
)

# HTML 实体空白。wiki 用 &nbsp; 做首行缩进,量很大;不还原成普通空格,
# 它们会被当成正文字符,把"汉字占比"这类判据算歪。
_NBSP_ENTITY = re.compile(r"&nbsp;|&#160;|&emsp;|&ensp;|&thinsp;", re.IGNORECASE)


def _strip_templates(text: str) -> str:
    for _ in range(10):  # 上限防病态嵌套死循环
        cleaned = _TEMPLATE.sub("", text)
        if cleaned == text:
            break
        text = cleaned
    return text


def clean_wikitext(text: str) -> str:
    """去掉 wiki 标记,保留可读正文。顺序有讲究:先删注释/ref(里面可能
    含模板),再删表格,最后删模板——表格的花括号和模板的定界符同源,
    先删模板会把表格拆成碎渣。
    """
    text = _COMMENT.sub("", text)
    text = _REF.sub("", text)
    text = _HTML_TAG.sub("", text)
    text = _TABLE.sub("", text)
    text = _strip_templates(text)
    text = _LINK.sub(r"\1", text)
    text = _EXT_LINK.sub(r"\1", text)
    text = _BARE_URL.sub("", text)
    text = _BOLD_ITALIC.sub("", text)
    text = _MAGIC.sub("", text)
    text = _NBSP_ENTITY.sub(" ", text)
    # 列表标记(: * #)保留文字去掉符号;连续空行压成一行。
    text = re.sub(r"^[:*#]+\s*", "", text, flags=re.MULTILINE)
    # 图片参数残渣要在列表标记之后删:它常常跟在 * 后面。
    text = _IMAGE_RESIDUE.sub("", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def split_sections(wikitext: str) -> list[tuple[str, str]]:
    """按章节标题切分,返回 [(小节标题, 该节正文)]。

    第一个元组是导语(标题为空串)——wiki 页开头没有标题的那段摘要,
    往往是全页信息密度最高的部分,不能丢。
    """
    matches = list(_HEADING.finditer(wikitext))
    if not matches:
        return [("", wikitext)]

    sections: list[tuple[str, str]] = []
    if matches[0].start() > 0:
        sections.append(("", wikitext[: matches[0].start()]))
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(wikitext)
        sections.append((match.group(2).strip(), wikitext[match.end() : end]))
    return sections


# 单块目标长度。太长(>1000)语义被稀释,太短(<200)上下文不够;
# 600 是 embedding 模型输入和检索粒度的折中,实测值,不是理论值。
CHUNK_MAX_CHARS = 600
# 比这还短的块直接丢:碎片不但检索不上,还会把 top_k 名额挤掉。
CHUNK_MIN_CHARS = 50

# 维护类分类:这些页面是 wiki 的内部事务,不是攻略内容。
# 命中任一即整页跳过——它们的正文常常是模板占位或半成品。
JUNK_CATEGORIES = frozenset(
    {"待完善", "待审核", "待审核攻略", "待补充", "施工中", "模板", "含有受损文件链接的页面"}
)

# 判定"这段文本不是给人读的攻略正文"的特征。
# 实测触发案例:一个伤害计算器页面,正文是 MATLAB 风格的公式和变量赋值,
# 嵌入进去只会污染检索结果——它匹配不到任何自然语言提问。
_CODE_MARKERS = re.compile(r"[=;%]{1}")
_CJK = re.compile(r"[\u4e00-\u9fff]")


def looks_like_junk(text: str) -> bool:
    """粗筛非自然语言正文(代码、数据表残渣、纯符号)。

    两个信号:汉字占比过低(<25%),或代码符号密度过高(>8%)。
    阈值刻意保守——宁可漏放几段噪声,也不要把正常攻略误杀。
    """
    if not text:
        return True
    cjk_ratio = len(_CJK.findall(text)) / len(text)
    symbol_ratio = len(_CODE_MARKERS.findall(text)) / len(text)
    return cjk_ratio < 0.25 or symbol_ratio > 0.08


def chunk_page(page_title: str, wikitext: str, *, max_chars: int = CHUNK_MAX_CHARS) -> list[str]:
    """一页 wikitext -> 若干带标题前缀的文本块。"""
    chunks: list[str] = []
    for section_title, body in split_sections(wikitext):
        text = clean_wikitext(body)
        if not text:
            continue
        header = f"{page_title} · {section_title}" if section_title else page_title

        # 节内按段落聚合:段落是天然的语义边界,比硬按字数切好得多。
        buffer: list[str] = []
        buffered = 0
        for paragraph in text.split("\n"):
            paragraph = paragraph.strip()
            if not paragraph:
                continue
            # 单段就超长的(极少见,多为复制粘贴的纯文本),按长度硬切。
            while len(paragraph) > max_chars:
                if buffer:
                    chunks.append(header + "\n" + "\n".join(buffer))
                    buffer, buffered = [], 0
                chunks.append(header + "\n" + paragraph[:max_chars])
                paragraph = paragraph[max_chars:]
            if buffered + len(paragraph) > max_chars and buffer:
                chunks.append(header + "\n" + "\n".join(buffer))
                buffer, buffered = [], 0
            buffer.append(paragraph)
            buffered += len(paragraph)
        if buffer:
            chunks.append(header + "\n" + "\n".join(buffer))

    return [
        chunk
        for chunk in chunks
        if len(chunk) >= CHUNK_MIN_CHARS and not looks_like_junk(chunk)
    ]
