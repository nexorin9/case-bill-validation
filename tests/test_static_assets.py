"""浏览器面板静态资源回归测试（Task 10）。

覆盖：
- static/index.html / app.js / styles.css 都存在；
- 每个文件 ≥ 30 行；
- 关键字存在（EventSource / POST / scrollToSpan / 状态色 CSS 变量等）；
- 状态色变量定义齐全（7 态）；
- 上传 / 进度 / 字段表 / 原文 / 导出五大区在 index.html 中各有一个 anchor。
"""

from __future__ import annotations

from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
STATIC_DIR = PROJECT_ROOT / "case_bill_validation" / "static"

INDEX_HTML = STATIC_DIR / "index.html"
APP_JS = STATIC_DIR / "app.js"
STYLES_CSS = STATIC_DIR / "styles.css"


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def test_static_files_exist() -> None:
    """三个静态资源文件必须存在。"""
    assert INDEX_HTML.exists(), f"缺失：{INDEX_HTML}"
    assert APP_JS.exists(), f"缺失：{APP_JS}"
    assert STYLES_CSS.exists(), f"缺失：{STYLES_CSS}"


def test_static_files_min_line_count() -> None:
    """每个文件 ≥ 30 行（避免占位 stub）。"""
    for fp in (INDEX_HTML, APP_JS, STYLES_CSS):
        lines = fp.read_text(encoding="utf-8").splitlines()
        assert len(lines) >= 30, f"{fp.name} 行数 {len(lines)} < 30"


def test_index_html_has_required_sections() -> None:
    """index.html 必须包含上传 / 进度 / 字段表 / 原文 / 导出五区。"""
    html = _read(INDEX_HTML)
    required = [
        'id="upload-form"',          # 上传表单
        'id="pipeline-progress"',    # 进度条
        'id="field-table"',          # 字段表
        'id="source-text"',          # 原文高亮区
        'id="export-btn"',           # 导出按钮
    ]
    for token in required:
        assert token in html, f"index.html 缺少 {token}"

    # 引用 styles.css + app.js
    assert '/static/styles.css' in html
    assert '/static/app.js' in html


def test_app_js_has_required_keywords() -> None:
    """app.js 必须含 EventSource / POST / scrollToSpan / fetch 调用。"""
    js = _read(APP_JS)
    required = [
        "EventSource",        # SSE 订阅
        "scrollToSpan",       # 跳转到原文 span
        "method: \"POST\"",   # 确认 / 驳回 / 导出调用
        "fetch(",             # 上传 / 拉取 / 操作
        "/api/cases",         # 至少一处 API 调用
    ]
    for token in required:
        assert token in js, f"app.js 缺少关键字 {token}"

    # 必须定义 7 态状态标签
    statuses = ["pending", "extracting", "rule_checked", "semantic_checked",
                "confirmed", "rejected", "flagged"]
    for s in statuses:
        assert s in js, f"app.js 缺少状态 {s}"


def test_styles_css_has_status_color_tokens() -> None:
    """styles.css 必须定义 7 态状态色 + 必要交互色变量。"""
    css = _read(STYLES_CSS)
    color_vars = [
        "--status-pending",
        "--status-extracting",
        "--status-rule_checked",
        "--status-semantic_checked",
        "--status-confirmed",
        "--status-rejected",
        "--status-flagged",
        "--c-primary",
        "--c-success",
        "--c-danger",
    ]
    for v in color_vars:
        assert v in css, f"styles.css 缺少 CSS 变量 {v}"

    # 关键交互样式：tr:hover + mark.field-highlight + fading
    for token in ["tr:hover", "mark.field-highlight", "fading", ".status-badge"]:
        assert token in css, f"styles.css 缺少样式 {token}"
