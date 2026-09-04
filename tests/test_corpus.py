"""语料清洗与分块的纯函数测试。"""

from app.services.corpus import (
    CHUNK_MIN_CHARS,
    chunk_page,
    clean_wikitext,
    looks_like_junk,
    split_sections,
)


def test_clean_strips_templates_nested_and_flat() -> None:
    raw = "{{信息框|角色}}{{嵌套{{内部}}外层}}正文内容"

    assert "{{" not in clean_wikitext(raw)
    assert "正文内容" in clean_wikitext(raw)


def test_clean_strips_refs_and_comments() -> None:
    raw = "结论<ref>https://example.com/source</ref>正文<!-- 隐藏注释 -->"

    cleaned = clean_wikitext(raw)
    assert "ref" not in cleaned
    assert "隐藏" not in cleaned
    assert "结论" in cleaned and "正文" in cleaned


def test_clean_unwraps_links_keeps_display_text() -> None:
    raw = "使用[[提瓦特|提瓦特大陆]]的[[传送锚点]]。"

    cleaned = clean_wikitext(raw)
    assert "提瓦特大陆" in cleaned
    assert "传送锚点" in cleaned
    assert "[[" not in cleaned


def test_clean_drops_tables_and_urls() -> None:
    raw = "{| class=wikitable\n| 1200 || 34\n|}\n前往[https://example.com 官网]看更多 https://spam.example"

    cleaned = clean_wikitext(raw)
    assert "1200" not in cleaned
    assert "官网" in cleaned
    assert "spam" not in cleaned


def test_clean_strips_stray_html_tags() -> None:
    raw = "<div>第一行</div><br>第二行<span style=\"x\">第三行</span>"

    cleaned = clean_wikitext(raw)
    assert "<" not in cleaned
    assert "第一行" in cleaned and "第三行" in cleaned


def test_split_sections_keeps_lead_and_orders_titles() -> None:
    raw = "导语内容\n== 攻略 ==\n正文A\n=== 细节 ===\n正文B\n== 配队 ==\n正文C"

    sections = split_sections(raw)
    assert [title for title, _ in sections] == ["", "攻略", "细节", "配队"]
    assert "导语内容" in sections[0][1]
    assert "正文B" in sections[2][1]


def test_chunk_page_prefixes_title_and_respects_max_chars() -> None:
    raw = (
        "== 配队推荐 ==\n"
        + ("推荐使用火系主C配队，输出稳定。" * 40)  # 单段约 560 字
        + "\n\n"
        + ("另一段关于充能的说明文字。" * 40)
    )

    chunks = chunk_page("纳塔攻略", raw)
    assert len(chunks) >= 2
    assert all(chunk.startswith("纳塔攻略 · 配队推荐") for chunk in chunks)
    assert all(len(chunk) <= 600 + len("纳塔攻略 · 配队推荐\n") for chunk in chunks)


def test_chunk_page_drops_tiny_fragments() -> None:
    chunks = chunk_page("页面", "== 小节 ==\n就一句话")

    assert all(len(chunk) >= CHUNK_MIN_CHARS for chunk in chunks)


def test_chunk_page_without_sections_uses_page_title() -> None:
    chunks = chunk_page("刻晴", "雷元素单手剑角色，" + "普攻五段斩击，重击消耗体力。" * 30)

    assert chunks and chunks[0].startswith("刻晴\n")


def test_clean_drops_image_layout_residue() -> None:
    """[[文件:x.png|thumb|240px|说明]] 被内链正则处理后会剩下排版参数行。"""
    raw = "thumb|240px|阿贝多官方故事图\n这门技术历史悠久。"

    cleaned = clean_wikitext(raw)
    assert "240px" not in cleaned
    assert "官方故事图" not in cleaned
    assert "这门技术历史悠久" in cleaned


def test_clean_normalizes_nbsp_entities() -> None:
    """wiki 用 &nbsp; 做缩进,不还原会把汉字占比算歪。"""
    raw = "&nbsp;&nbsp;&nbsp;&nbsp;炼金术只够用来拼接物件碎块。"

    cleaned = clean_wikitext(raw)
    assert "nbsp" not in cleaned
    assert "炼金术" in cleaned


def test_looks_like_junk_rejects_formula_dump() -> None:
    """实测案例:一个伤害计算器页面,正文是 MATLAB 风格的变量赋值。"""
    formula = "gjbz=321+608; gjxs=46.6; gdgj=311+18*2; bs=169; jt3=247; ss3=46.6+15;"

    assert looks_like_junk(formula)


def test_looks_like_junk_keeps_normal_guide_text() -> None:
    text = "纳塔是提瓦特大陆的火之国，居民以龙为伴，探索时需要注意龙息机制。"

    assert not looks_like_junk(text)


def test_chunk_page_filters_junk_chunks() -> None:
    """整页里混着公式段和正常段时,只保留正常段。"""
    raw = (
        "== 伤害计算 ==\n"
        + "gjbz=321;gjxs=46.6;gdgj=311;bs=169;jt3=247;ss3=61;dmg=1234;" * 6
        + "\n== 配队 ==\n"
        + "推荐火系主C搭配水系副C，打蒸发反应输出稳定，注意元素充能效率。" * 4
    )

    chunks = chunk_page("测试页", raw)

    assert chunks, "正常段落不该被一起丢掉"
    assert all("gjbz" not in chunk for chunk in chunks)
    assert any("蒸发反应" in chunk for chunk in chunks)
