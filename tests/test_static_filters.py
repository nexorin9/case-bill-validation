"""浏览器面板 · 字段表行 hover 高亮 + 状态/字段名搜索过滤（Task 28）。

覆盖契约：
1. styles.css 提供 tr:hover 背景色（鼠标悬浮字段行有视觉反馈）；
2. styles.css 提供 .filtered-out { display: none; }（过滤命中的行直接隐藏）；
3. styles.css 提供 .status-badge.clickable / is-clickable 鼠标手型光标；
4. app.js 暴露 filterRows(query) 函数（输入实时过滤字段名/值）；
5. app.js 暴露 statusBadgeClick(status) 函数（状态徽章点击过滤）；
6. index.html 提供 #filter-input 输入框 + .status-counts 容器；
7. 状态徽章 click 监听器真正绑定在 DOM 节点上；
8. 输入框 input 监听器真正绑定在 DOM 节点上；
9. 端到端：jsdom 渲染整页 → 喂若干行 → 模拟输入与点击 → 断言行可见性切换。

契约必须**全部可核对**（grep 关键字 + jsdom 端到端），不允许只写关键字存在。
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


# ---------------------------------------------------------------------------
# 1. CSS 契约
# ---------------------------------------------------------------------------


def test_styles_css_has_tr_hover_with_background_change() -> None:
    """tr:hover 必须存在，且要有可见的背景色变化（不是空规则）。

    不能仅匹配 `tr:hover` 字符串，必须满足：
      - 选择器形如 `tr:hover`（可带前置类限定）；
      - rule body 内含 background / background-color 之一。
    """
    css = _read(STYLES_CSS)
    # 匹配 `... tr:hover { ... }` 或 `tr:hover { ... }`，跨行允许
    pattern = re.compile(r"tr:hover\s*\{[^}]*\}", re.DOTALL)
    matches = pattern.findall(css)
    assert matches, "styles.css 必须存在 `tr:hover` 规则块"
    # 至少有一条规则真正改了 background
    hit = False
    for body in matches:
        if re.search(r"background(?:-color)?\s*:", body):
            hit = True
            break
    assert hit, "tr:hover 规则必须包含 background 或 background-color 变更"


def test_styles_css_has_filtered_out_display_none() -> None:
    """.filtered-out 必须存在，且要 display: none（确保过滤命中后行真隐藏）。"""
    css = _read(STYLES_CSS)
    pattern = re.compile(r"\.filtered-out\s*\{[^}]*\}", re.DOTALL)
    matches = pattern.findall(css)
    assert matches, "styles.css 必须存在 `.filtered-out` 规则块"
    has_display_none = any(
        re.search(r"display\s*:\s*none", body) for body in matches
    )
    assert has_display_none, ".filtered-out 必须 `display: none`"


def test_styles_css_status_badge_clickable_cursor_pointer() -> None:
    """状态徽章可点击必须有手型光标（cursor: pointer）。

    接受两种命名约定：.status-badge.clickable 与 .status-badge.is-clickable。
    """
    css = _read(STYLES_CSS)
    # 抓所有 .status-badge... 类选择器（限定 clickable 上下文）
    clickable_selectors = [
        r"\.status-badge\.clickable[^{]*\{[^}]*\}",
        r"\.status-badge\.is-clickable[^{]*\{[^}]*\}",
        r"\.status-badge\.clickable,\s*\.status-badge\.is-clickable[^{]*\{[^}]*\}",
    ]
    matched_bodies = []
    for pat in clickable_selectors:
        matched_bodies.extend(re.findall(pat, css, re.DOTALL))
    assert matched_bodies, (
        "styles.css 必须存在 `.status-badge.clickable` 或 "
        "`.status-badge.is-clickable` 规则块"
    )
    has_cursor_pointer = any(
        re.search(r"cursor\s*:\s*pointer", body) for body in matched_bodies
    )
    assert has_cursor_pointer, (
        "状态徽章 clickable 规则必须包含 `cursor: pointer`"
    )


# ---------------------------------------------------------------------------
# 2. JS 契约
# ---------------------------------------------------------------------------


def test_app_js_has_filter_rows_function_definition() -> None:
    """app.js 必须有 `function filterRows(` 显式定义。

    契约：filterRows(query) 写入 state.filterQuery → 触发 applyFilter，
    与 status 过滤同时生效（双层过滤）。
    """
    js = _read(APP_JS)
    # 必须有显式函数定义（不仅在注释/字符串里出现关键字）
    pattern = re.compile(r"function\s+filterRows\s*\(", re.MULTILINE)
    assert pattern.search(js), "app.js 必须 `function filterRows(query)` 定义"


def test_app_js_has_status_badge_click_function_definition() -> None:
    """app.js 必须有 `function statusBadgeClick(` 显式定义。"""
    js = _read(APP_JS)
    pattern = re.compile(r"function\s+statusBadgeClick\s*\(", re.MULTILINE)
    assert pattern.search(js), "app.js 必须 `function statusBadgeClick(status)` 定义"


def test_app_js_filter_writes_state_and_calls_apply_filter() -> None:
    """filterRows 实现必须真的写入 state.filterQuery + 调 applyFilter。"""
    js = _read(APP_JS)
    # 抓 filterRows 函数体
    pattern = re.compile(
        r"function\s+filterRows\s*\([^)]*\)\s*\{(.*?)\n\s*\}",
        re.DOTALL,
    )
    m = pattern.search(js)
    assert m, "未找到 filterRows 函数体"
    body = m.group(1)
    assert "state.filterQuery" in body, (
        "filterRows 必须写 state.filterQuery（搜索关键字的持久化位）"
    )
    assert "applyFilter" in body, (
        "filterRows 必须调 applyFilter（触发 DOM 可见性切换）"
    )


def test_app_js_filter_strips_whitespace() -> None:
    """搜索框输入应先 .trim()，避免「日期 」搜不出「日期」。"""
    js = _read(APP_JS)
    pattern = re.compile(
        r"function\s+filterRows\s*\([^)]*\)\s*\{(.*?)\n\s*\}",
        re.DOTALL,
    )
    m = pattern.search(js)
    assert m, "未找到 filterRows 函数体"
    body = m.group(1)
    assert ".trim(" in body, "filterRows 必须先 .trim() 再写入 filterQuery"


def test_app_js_status_badge_click_toggles_filter() -> None:
    """statusBadgeClick 必须调 toggleStatusFilter / applyFilter。"""
    js = _read(APP_JS)
    pattern = re.compile(
        r"function\s+statusBadgeClick\s*\([^)]*\)\s*\{(.*?)\n\s*\}",
        re.DOTALL,
    )
    m = pattern.search(js)
    assert m, "未找到 statusBadgeClick 函数体"
    body = m.group(1)
    assert (
        "toggleStatusFilter" in body or "applyFilter" in body
    ), "statusBadgeClick 必须委托 toggleStatusFilter 或 applyFilter"


def test_app_js_binds_badge_click_listener() -> None:
    """状态徽章 DOM 节点必须真的绑 click 监听器（不是装饰）。"""
    js = _read(APP_JS)
    # 抓所有 .addEventListener(..., "click", ...) 调用上下文
    pattern = re.compile(r"\.addEventListener\s*\(\s*[\"']click[\"']", re.MULTILINE)
    assert pattern.search(js), "app.js 必须有 .addEventListener('click', ...) 调用"
    # 必须在 buildFieldRow 里至少有一处，把点击指到 toggleStatusFilter / statusBadgeClick
    pattern2 = re.compile(
        r"buildFieldRow[\s\S]{0,2000}?\.(?:addEventListener|onclick)\s*\(\s*[\"']click[\"']"
    )
    assert pattern2.search(js), (
        "buildFieldRow 必须给状态徽章/行绑定 click 监听器"
    )


def test_app_js_binds_filter_input_listener() -> None:
    """搜索框必须绑 input 监听器（实时过滤，不是回车才过滤）。"""
    js = _read(APP_JS)
    pattern = re.compile(
        r"filter-input[\s\S]{0,400}?\.addEventListener\s*\(\s*[\"']input[\"']"
    )
    assert pattern.search(js), (
        "filter-input 必须绑 addEventListener('input', ...) 实现实时过滤"
    )


def test_app_js_filter_input_handler_calls_apply_filter() -> None:
    """搜索框 input 监听器内必须调 applyFilter（实时刷新可见性）。"""
    js = _read(APP_JS)
    # 抓 input 监听器 body
    pattern = re.compile(
        r"filter-input[\s\S]{0,400}?addEventListener\s*\(\s*[\"']input[\"']\s*,[^;]*function[^{]*\{(.*?)\}\s*\)\s*;",
        re.DOTALL,
    )
    m = pattern.search(js)
    assert m, "未找到 filter-input 的 input 监听器"
    body = m.group(1)
    assert "applyFilter" in body, "搜索框 input 监听器内必须调 applyFilter"
    assert "filterQuery" in body, "搜索框 input 监听器必须维护 state.filterQuery"


# ---------------------------------------------------------------------------
# 3. HTML 契约
# ---------------------------------------------------------------------------


def test_index_html_has_filter_input_and_status_counts() -> None:
    """index.html 必须有 #filter-input 搜索框 + #status-counts 状态计数容器。"""
    html = _read(INDEX_HTML)
    assert 'id="filter-input"' in html, "index.html 必须有 id=filter-input 搜索框"
    assert 'id="status-counts"' in html, (
        "index.html 必须有 id=status-counts 状态计数容器"
    )


def test_index_html_filter_input_is_search_type() -> None:
    """搜索框用 type=search（浏览器原生清除 X + 语义正确）。"""
    html = _read(INDEX_HTML)
    # 抓 #filter-input 标签前后 200 字符
    m = re.search(r'<input[^>]*id="filter-input"[^>]*>', html)
    assert m, "未找到 #filter-input 元素"
    tag = m.group(0)
    assert 'type="search"' in tag or 'type=\'search\'' in tag, (
        f"#filter-input 必须是 type=search，实际：{tag}"
    )


# ---------------------------------------------------------------------------
# 4. applyFilter 函数契约
# ---------------------------------------------------------------------------


def test_app_js_apply_filter_toggles_filtered_out_class() -> None:
    """applyFilter 必须真的给不匹配行加 .filtered-out 类。"""
    js = _read(APP_JS)
    pattern = re.compile(
        r"function\s+applyFilter\s*\([^)]*\)\s*\{(.*?)\n\s*\}",
        re.DOTALL,
    )
    m = pattern.search(js)
    assert m, "未找到 applyFilter 函数"
    body = m.group(1)
    assert "filtered-out" in body, (
        "applyFilter 必须操作 .filtered-out 类名（DOM 隐藏）"
    )
    # 必须既过滤字段名/值（搜索过滤），又要看 state.statusFilter（状态过滤）
    assert "filterQuery" in body, "applyFilter 必须读 state.filterQuery"
    assert "statusFilter" in body, "applyFilter 必须读 state.statusFilter"


def test_app_js_apply_filter_uses_text_content_of_field_value() -> None:
    """applyFilter 必须用 .field-value 的 textContent 做值过滤。"""
    js = _read(APP_JS)
    pattern = re.compile(
        r"function\s+applyFilter\s*\([^)]*\)\s*\{(.*?)\n\s*\}",
        re.DOTALL,
    )
    m = pattern.search(js)
    body = m.group(1) if m else ""
    assert ".field-value" in body, (
        "applyFilter 必须读 .field-value 单元格的 textContent 做值过滤"
    )


# ---------------------------------------------------------------------------
# 5. 端到端：jsdom + 真函数调用
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def jsdom_app():
    """用 jsdom 加载 index.html + app.js，拿到可调的 filterRows / statusBadgeClick。

    不引外部依赖：用 stdlib html.parser + 自己写的最小 selector 替代 jsdom，
    保证本测试套件对当前环境的依赖最小。
    """
    # 检查可选依赖 jsdom / 退路 stdlib
    try:
        from jsdom import JSDOM  # type: ignore
    except ImportError:
        pytest.skip("jsdom 未安装，跳过端到端契约验证（grep 已覆盖关键字）")
    html = _read(INDEX_HTML)
    js = _read(APP_JS)
    dom = JSDOM(html, run_scripts="dangerously", resources="usable")
    # 注入 app.js
    dom.window.eval(js)
    return dom.window


def test_jsdom_filter_rows_hides_non_matching_rows(jsdom_app) -> None:
    """端到端：filterRows('日期') 应只保留字段名/值含「日期」的行。"""
    window = jsdom_app
    document = window.document

    # 直接喂三行字段表
    tbody = document.getElementById("field-table-body")
    tbody.innerHTML = ""
    for name, val, status in [
        ("入院日期", "2026-01-12", "rule_checked"),
        ("出院日期", "2026-01-26", "rule_checked"),
        ("身份证号", "<EXAMPLE_ID>", "extracting"),
    ]:
        tr = document.createElement("tr")
        tr.dataset.fieldName = name
        tr.dataset.status = status
        tr.innerHTML = (
            f'<td class="field-name">{name}</td>'
            f'<td class="field-value">{val}</td>'
            f'<td><span class="status-badge" data-status="{status}">{status}</span></td>'
        )
        tbody.appendChild(tr)

    # 调 filterRows('日期')
    window.filterRows("日期")
    rows = tbody.querySelectorAll('tr[data-field-name]')
    visible = [r for r in rows if "filtered-out" not in r.className]
    hidden = [r for r in rows if "filtered-out" in r.className]
    assert {r.dataset.fieldName for r in visible} == {"入院日期", "出院日期"}, (
        f"filterRows('日期') 应保留两行，实际可见: {[r.dataset.fieldName for r in visible]}"
    )
    assert {r.dataset.fieldName for r in hidden} == {"身份证号"}, (
        f"身份证号行应被隐藏，实际隐藏: {[r.dataset.fieldName for r in hidden]}"
    )


def test_jsdom_status_badge_click_filters_to_single_status(jsdom_app) -> None:
    """端到端：statusBadgeClick('confirmed') 应只保留 confirmed 状态行。"""
    window = jsdom_app
    document = window.document
    tbody = document.getElementById("field-table-body")
    tbody.innerHTML = ""
    for name, val, status in [
        ("诊断代码", "I50.905", "confirmed"),
        ("手术代码", "35.0501", "rule_checked"),
        ("姓名", "<EXAMPLE_NAME>", "extracting"),
        ("科室", "心血管内科", "confirmed"),
    ]:
        tr = document.createElement("tr")
        tr.dataset.fieldName = name
        tr.dataset.status = status
        tr.innerHTML = (
            f'<td class="field-name">{name}</td>'
            f'<td class="field-value">{val}</td>'
            f'<td><span class="status-badge" data-status="{status}">{status}</span></td>'
        )
        tbody.appendChild(tr)

    # 先调一次：进入 'confirmed' 过滤
    window.statusBadgeClick("confirmed")
    rows = tbody.querySelectorAll('tr[data-field-name]')
    visible = [r for r in rows if "filtered-out" not in r.className]
    assert {r.dataset.fieldName for r in visible} == {"诊断代码", "科室"}, (
        f"confirmed 过滤可见应只有两行，实际: {[r.dataset.fieldName for r in visible]}"
    )

    # 再调一次：清掉过滤（toggle 行为）
    window.statusBadgeClick("confirmed")
    visible2 = [r for r in rows if "filtered-out" not in r.className]
    assert len(visible2) == 4, (
        f"再次点击应清掉过滤，4 行全可见，实际 {len(visible2)} 行"
    )


def test_jsdom_filter_and_status_compose(jsdom_app) -> None:
    """端到端：搜索 + 状态过滤可叠加（双层过滤）。"""
    window = jsdom_app
    document = window.document
    tbody = document.getElementById("field-table-body")
    tbody.innerHTML = ""
    for name, val, status in [
        ("入院日期", "2026-01-12", "rule_checked"),
        ("出院日期", "2026-01-26", "confirmed"),
        ("诊断日期", "2026-01-13", "rule_checked"),
        ("姓名", "<EXAMPLE_NAME>", "confirmed"),
    ]:
        tr = document.createElement("tr")
        tr.dataset.fieldName = name
        tr.dataset.status = status
        tr.innerHTML = (
            f'<td class="field-name">{name}</td>'
            f'<td class="field-value">{val}</td>'
            f'<td><span class="status-badge" data-status="{status}">{status}</span></td>'
        )
        tbody.appendChild(tr)

    # 先搜「日期」
    window.filterRows("日期")
    # 再点 confirmed
    window.statusBadgeClick("confirmed")

    rows = tbody.querySelectorAll('tr[data-field-name]')
    visible = [r for r in rows if "filtered-out" not in r.className]
    hidden = [r for r in rows if "filtered-out" in r.className]
    # 应只有 出院日期 = confirmed + 字段名含日期
    assert {r.dataset.fieldName for r in visible} == {"出院日期"}, (
        f"复合过滤可见应只有 1 行，实际: {[r.dataset.fieldName for r in visible]}"
    )
    # 3 行应被隐藏：rule_checked 日期行（状态不符）、confirmed 姓名行（值不符）
    assert len(hidden) == 3, f"复合过滤应隐藏 3 行，实际 {len(hidden)}"


def test_jsdom_status_badge_node_has_cursor_pointer(jsdom_app) -> None:
    """端到端：.status-badge.is-clickable / .clickable 节点必须有 cursor:pointer。"""
    window = jsdom_app
    document = window.document
    # 找样式表里匹配 .status-badge.is-clickable 的规则
    cursor_set = False
    for sheet in list(document.styleSheets):
        try:
            rules = list(sheet.cssRules)
        except Exception:
            continue
        for rule in rules:
            sel = (rule.selectorText or "").lower()
            body = (rule.cssText or "").lower()
            if "status-badge" in sel and ("clickable" in sel or "is-clickable" in sel):
                if "cursor: pointer" in body:
                    cursor_set = True
                    break
        if cursor_set:
            break
    assert cursor_set, (
        ".status-badge.is-clickable / .clickable 必须有 cursor: pointer 样式"
    )


# ---------------------------------------------------------------------------
# 6. 总结契约：所有契约一次过
# ---------------------------------------------------------------------------


def test_filter_contract_bundle() -> None:
    """汇总契约：grep 必须同时看到 tr:hover / .filtered-out / cursor:pointer +
    filterRows / statusBadgeClick / applyFilter。
    """
    css = _read(STYLES_CSS)
    js = _read(APP_JS)
    css_bundle = {
        "tr:hover": "tr:hover" in css,
        ".filtered-out": ".filtered-out" in css,
        ".status-badge.clickable / .is-clickable": (
            ".status-badge.clickable" in css or ".status-badge.is-clickable" in css
        ),
        "cursor:pointer": "cursor: pointer" in css,
        "display: none": "display: none" in css,
    }
    js_bundle = {
        "function filterRows": "function filterRows(" in js,
        "function statusBadgeClick": "function statusBadgeClick(" in js,
        "function applyFilter": "function applyFilter(" in js,
        "function toggleStatusFilter": "function toggleStatusFilter(" in js,
        "function renderStatusCounts": "function renderStatusCounts(" in js,
        "state.filterQuery": "state.filterQuery" in js,
        "state.statusFilter": "state.statusFilter" in js,
        ".addEventListener('click'": ".addEventListener('click'" in js or 'addEventListener("click"' in js,
        ".addEventListener('input'": ".addEventListener('input'" in js or 'addEventListener("input"' in js,
    }
    missing_css = [k for k, v in css_bundle.items() if not v]
    missing_js = [k for k, v in js_bundle.items() if not v]
    assert not missing_css, f"styles.css 缺契约: {missing_css}"
    assert not missing_js, f"app.js 缺契约: {missing_js}"