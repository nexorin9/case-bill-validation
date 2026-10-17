"""浏览器面板 · 原文高亮 mark 渲染 + span 区间对齐（Task 29）。

覆盖契约：
1. styles.css 提供 mark.field-highlight 规则块，含黄色背景 (#fef08a) + 黄色边框 (#facc15) + padding；
2. styles.css 提供 fade / fading 规则块，含 opacity 下降（0.3 上下）+ transition 让淡出平滑；
3. app.js 暴露 scrollToSpan(rawText, startChar, endChar) 函数；
4. scrollToSpan 实现里创建 <mark class='field-highlight'> 元素并 append 到原文区；
5. scrollToSpan 实现里用 setTimeout(..., 3000) 在 3 秒后追加 fading class；
6. scrollToSpan 实现里先 clear 旧的 mark.field-highlight，再渲染新 mark（不同字段高亮互不污染）。

契约必须**全部可核对**（grep 关键字 + 函数体断言），不允许只写关键字存在。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
STATIC_DIR = PROJECT_ROOT / "case_bill_validation" / "static"

INDEX_HTML = STATIC_DIR / "index.html"
APP_JS = STATIC_DIR / "app.js"
STYLES_CSS = STATIC_DIR / "styles.css"


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def _extract_function_body(source: str, fn_name: str) -> str:
    """抓取 function <name>(...) { <body> } 的函数体，使用平衡大括号扫描。

    支持嵌套的 if / while / object literal 等带额外 { ... } 块。
    """
    pattern = re.compile(
        r"function\s+" + re.escape(fn_name) + r"\s*\([^)]*\)\s*\{",
        re.MULTILINE,
    )
    m = pattern.search(source)
    if not m:
        return ""
    start = m.end()
    depth = 1
    i = start
    in_str = None  # None / "'" / '"'
    while i < len(source):
        c = source[i]
        if in_str is not None:
            if c == "\\":
                i += 2
                continue
            if c == in_str:
                in_str = None
            i += 1
            continue
        if c in ("'", '"'):
            in_str = c
            i += 1
            continue
        if c == "/" and i + 1 < len(source) and source[i + 1] == "/":
            # 行注释：跳到下一行
            j = source.find("\n", i)
            i = len(source) if j == -1 else j + 1
            continue
        if c == "/" and i + 1 < len(source) and source[i + 1] == "*":
            j = source.find("*/", i + 2)
            i = len(source) if j == -1 else j + 2
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return source[start:i]
        i += 1
    return ""


# ---------------------------------------------------------------------------
# 1. CSS 契约
# ---------------------------------------------------------------------------


def test_styles_css_has_mark_field_highlight_rule() -> None:
    """mark.field-highlight 必须存在，含黄色背景 + 黄色边框 + padding。

    不能仅匹配关键字字符串，必须满足：
      - 选择器形如 `mark.field-highlight`；
      - rule body 内含 background + border + padding 三件套。
    """
    css = _read(STYLES_CSS)
    pattern = re.compile(
        r"mark\.field-highlight\s*\{[^}]*\}", re.DOTALL
    )
    matches = pattern.findall(css)
    assert matches, "styles.css 必须存在 `mark.field-highlight` 规则块"
    # 至少有一条规则同时含 background + border + padding 三件套
    hit = False
    for body in matches:
        if (
            re.search(r"background(?:-color)?\s*:", body)
            and re.search(r"border(?:-[a-z]+)?\s*:", body)
            and re.search(r"padding\s*:", body)
        ):
            hit = True
            break
    assert hit, "mark.field-highlight 规则必须同时含 background + border + padding"


def test_styles_css_highlight_uses_yellow_color_palette() -> None:
    """黄底（#fef08a）与黄边（#facc15）必须真的出现在 mark.field-highlight 规则体内。

    这是高亮可见性的核心色板，不能改成纯灰或其他色系。
    """
    css = _read(STYLES_CSS)
    pattern = re.compile(
        r"mark\.field-highlight\s*\{[^}]*\}", re.DOTALL
    )
    matches = pattern.findall(css)
    assert matches, "styles.css 必须存在 `mark.field-highlight` 规则块"
    hit_bg = any("#fef08a" in body for body in matches)
    hit_border = any("#facc15" in body for body in matches)
    assert hit_bg, "mark.field-highlight 规则必须含黄色背景 #fef08a"
    assert hit_border, "mark.field-highlight 规则必须含黄色边框 #facc15"


def test_styles_css_has_fading_opacity_rule() -> None:
    """fade / fading 规则必须存在，含 opacity 下降与 transition 让淡出平滑。

    允许两种命名：
      - `mark.field-highlight.fading`（精确匹配 + 共同选择器）
      - `mark.fading`（仅 .fading 在 mark 上）

    opacity 阈值：≤ 0.5 算 fade（视觉上明显淡化即可，0.3 / 0.35 都可）。
    transition 阈值：≥ 0.3s（平滑过渡，不闪变）。
    """
    css = _read(STYLES_CSS)
    patterns = [
        r"mark\.field-highlight\.fading\s*\{[^}]*\}",
        r"mark\.fading\s*\{[^}]*\}",
        r"\.field-highlight\.fading\s*\{[^}]*\}",
    ]
    matched_bodies = []
    for pat in patterns:
        matched_bodies.extend(re.findall(pat, css, re.DOTALL))
    assert matched_bodies, (
        "styles.css 必须存在 `.fading` 规则块（如 "
        "`mark.field-highlight.fading` 或 `mark.fading`）"
    )
    # 抽 opacity 数值
    opacity_re = re.compile(r"opacity\s*:\s*([0-9.]+)")
    found_opacity = None
    for body in matched_bodies:
        m = opacity_re.search(body)
        if m:
            found_opacity = float(m.group(1))
            break
    assert found_opacity is not None, "fading 规则必须声明 opacity"
    assert 0.0 < found_opacity < 0.5, (
        f"fading opacity 必须 < 0.5 且 > 0（淡出可见），实际={found_opacity}"
    )
    # transition 至少出现一处（含 opacity 或 background 等可淡出的属性）
    has_transition = any(
        re.search(r"transition\s*:", body) for body in matched_bodies
    )
    if not has_transition:
        # 允许 transition 放在 mark.field-highlight 块内（fading 类继承 transition）
        main_bodies = re.findall(
            r"mark\.field-highlight\s*\{[^}]*\}", css, re.DOTALL
        )
        has_transition = any(
            re.search(r"transition\s*:[^;]*opacity", body)
            for body in main_bodies
        )
    assert has_transition, "必须声明 transition（opacity/background）让淡出平滑"


# ---------------------------------------------------------------------------
# 2. JS 契约
# ---------------------------------------------------------------------------


def test_app_js_has_scroll_to_span_function_definition() -> None:
    """app.js 必须 `function scrollToSpan(` 显式定义。

    签名可以是 (rawText, start, end) 或 (rawText, startChar, endChar)，但
    必须带 rawText + start + end 三个参数。
    """
    js = _read(APP_JS)
    pattern = re.compile(
        r"function\s+scrollToSpan\s*\(([^)]*)\)", re.MULTILINE
    )
    m = pattern.search(js)
    assert m, "app.js 必须 `function scrollToSpan(...)` 定义"
    params = m.group(1)
    assert "rawText" in params, (
        "scrollToSpan 必须接受 rawText 参数（原文）"
    )
    # start/end 至少有任一参数名
    assert any(name in params for name in ("start", "startChar")), (
        "scrollToSpan 必须接受 start 参数（偏移起点）"
    )
    assert any(name in params for name in ("end", "endChar")), (
        "scrollToSpan 必须接受 end 参数（偏移终点）"
    )


def test_app_js_scroll_to_span_creates_mark_field_highlight_element() -> None:
    """scrollToSpan 实现必须真的创建 <mark class='field-highlight'>。

    不能只在注释里出现关键字，必须能找到：
      - document.createElement("mark") 或 createElement('mark')
      - mark.className 含 'field-highlight'
      - appendChild(mark) 把 mark 插入到原文区。
    """
    js = _read(APP_JS)
    body = _extract_function_body(js, "scrollToSpan")
    assert body, "未找到 scrollToSpan 函数体"
    assert re.search(
        r"createElement\s*\(\s*[\"']mark[\"']\s*\)", body
    ), "scrollToSpan 必须 document.createElement('mark')"
    assert re.search(
        r"className\s*=[\s\S]*?field-highlight", body
    ), "scrollToSpan 必须给 mark 设 className 含 'field-highlight'"
    assert re.search(
        r"appendChild\s*\(\s*(?:the)?mark\b", body
    ), "scrollToSpan 必须 appendChild(mark) 把高亮插入到原文区"


def test_app_js_scroll_to_span_has_three_second_fade_timeout() -> None:
    """scrollToSpan 实现里必须 setTimeout(..., 3000) 在 3 秒后添加 fading class。

    验收：
      - setTimeout 存在，且第二个参数 = 3000（或 3000±500 毫秒）；
      - setTimeout 的回调函数体内有 classList.add('fading') 或 .add("fading")。
    """
    js = _read(APP_JS)
    body = _extract_function_body(js, "scrollToSpan")
    assert body, "未找到 scrollToSpan 函数体"
    timeout_pattern = re.compile(
        r"setTimeout\s*\(\s*(?:function|\(\s*\)\s*=>|\(\s*\w+\s*\)\s*=>)[^,]*,\s*(\d+)\s*\)",
        re.DOTALL,
    )
    m = timeout_pattern.search(body)
    assert m, (
        "scrollToSpan 必须有 setTimeout(..., <ms>) 形式定时器"
    )
    ms = int(m.group(1))
    assert 2500 <= ms <= 3500, (
        f"setTimeout 延迟必须在 2500–3500 ms 范围（3 秒附近），实际={ms}"
    )
    # 至少一段回调里有 classList.add('fading')
    adds_fading = re.search(
        r"classList\.add\s*\(\s*[\"']fading[\"']\s*\)", body
    )
    assert adds_fading, (
        "scrollToSpan 的 setTimeout 回调必须 classList.add('fading')"
    )


def test_app_js_scroll_to_span_clears_old_highlights_before_render() -> None:
    """scrollToSpan 在追加新 mark 前必须先清掉旧的 mark.field-highlight，
    否则「点不同字段高亮不同 mark」就无法生效。
    """
    js = _read(APP_JS)
    body = _extract_function_body(js, "scrollToSpan")
    assert body, "未找到 scrollToSpan 函数体"
    # 必须能找到 querySelectorAll('mark.field-highlight') 或类似选择器
    clear_pattern = re.compile(
        r"querySelectorAll\s*\(\s*[\"']mark\.field-highlight[\"']\s*\)"
    )
    assert clear_pattern.search(body), (
        "scrollToSpan 必须 querySelectorAll('mark.field-highlight') 清旧高亮"
    )
    # 必须存在 forEach / removeChild / unmount 等清除动作之一
    clear_actions = re.search(
        r"forEach\s*\(|removeChild\s*\(|replaceChildren\s*\(", body
    )
    assert clear_actions, (
        "scrollToSpan 必须对旧 mark 执行实际清除动作（forEach/removeChild/...）"
    )


def test_app_js_scroll_to_span_writes_target_text_into_mark() -> None:
    """scrollToSpan 必须把 slice(start, end) 的 target 文本写到 mark.textContent。"""
    js = _read(APP_JS)
    body = _extract_function_body(js, "scrollToSpan")
    assert body, "未找到 scrollToSpan 函数体"
    # 应有 slice(start, end) 或 slice(startChar, endChar) 形式
    slice_call = re.search(r"\.slice\s*\([^)]*\)", body)
    assert slice_call, "scrollToSpan 必须 .slice(start, end) 取目标片段"
    # mark.textContent = target 形式
    mark_text = re.search(
        r"mark\.textContent\s*=", body
    )
    assert mark_text, "scrollToSpan 必须 `mark.textContent = <变量>` 写入目标文本"


# ---------------------------------------------------------------------------
# 3. 跨文件契约 & HTML 关联
# ---------------------------------------------------------------------------


def test_app_js_scroll_to_span_smooth_scrolls_into_view() -> None:
    """scrollToSpan 必须在 mark 元素上调用 scrollIntoView（让高亮滚到视野）。"""
    js = _read(APP_JS)
    body = _extract_function_body(js, "scrollToSpan")
    assert body, "未找到 scrollToSpan 函数体"
    assert re.search(r"scrollIntoView\s*\(", body), (
        "scrollToSpan 必须调 scrollIntoView 让 mark 元素滚到视野"
    )


def test_scroll_to_span_invoked_from_show_evidence_handler() -> None:
    """onShowEvidence 处理器必须真的调 scrollToSpan（"看依据"按钮才有用）。

    不能只让 scrollToSpan 存在却没人调它。
    """
    js = _read(APP_JS)
    body = _extract_function_body(js, "onShowEvidence")
    assert body, "未找到 onShowEvidence 函数体"
    assert "scrollToSpan" in body, (
        "onShowEvidence 必须调 scrollToSpan（看依据按钮的实际行为）"
    )
    # 必须传入 start_char / end_char 之类参数
    assert "start_char" in body and "end_char" in body, (
        "onShowEvidence 必须从 mark.span.start_char / end_char 取 span 区间传入"
    )


def test_index_html_has_source_text_container() -> None:
    """index.html 必须有 #source-text 容器（scrollToSpan 的写入目标）。"""
    html = _read(INDEX_HTML)
    assert re.search(r'id\s*=\s*["\']source-text["\']', html), (
        "index.html 必须有 id='source-text' 的元素作为原文高亮区"
    )
