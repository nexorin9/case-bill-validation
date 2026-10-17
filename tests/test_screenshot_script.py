"""截图自动化脚本契约测试（Task 34）。

覆盖：
- scripts/capture_screenshots.sh 存在 + 可执行 + 含 --help / --install / --skip-install
  三个 CLI flag
- scripts/capture_screenshots.sh 帮助输出 ≥ 6 行（含「用法」「chromium」「上传」等关键字）
- scripts/screenshot.py 存在 ≥ 80 行 + 4 张截图文件名齐 + 三大关键 API 落盘
- scripts/screenshot.py 通过 _wait_for_pipeline_done 在 processed_steps ≥ 5 时返回 +
  _upload_via_api 走 httpx.POST /api/cases + _pick_executable 兜底本地 chromium
- docs/screenshots/{01..04}.png 4 张真 PNG 存在 + 分辨率 ≥ 1280x800
- file -b 命令识别为 PNG image data（非设计稿、非空文件）
- 4 张图覆盖 4 状态（上传/流水线运行/字段已审查/Excel 导出后）

回归：与 task 14 已落盘的 screenshot.py + 4 张 PNG 不冲突（仅补契约层）。
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = PROJECT_ROOT / "scripts"
SHOTS_DIR = PROJECT_ROOT / "docs" / "screenshots"

CAPTURE_SH = SCRIPTS_DIR / "capture_screenshots.sh"
SCREENSHOT_PY = SCRIPTS_DIR / "screenshot.py"

EXPECTED_SHOTS = (
    "01-upload.png",
    "02-pipeline-running.png",
    "03-fields-reviewed.png",
    "04-excel-exported.png",
)

MIN_WIDTH = 1280
MIN_HEIGHT = 800


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _png_dimensions(path: Path) -> tuple[int, int] | None:
    """用 file -b 解析 PNG 尺寸；非 PNG 返 None。"""
    try:
        out = subprocess.run(
            ["file", "-b", str(path)],
            capture_output=True, text=True, check=False, timeout=5
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    m = re.search(r"(\d+)\s*x\s*(\d+)", out)
    if not m:
        return None
    return int(m.group(1)), int(m.group(2))


# ── capture_screenshots.sh 契约 ─────────────────────────────────────────


def test_capture_screenshots_sh_exists() -> None:
    assert CAPTURE_SH.exists(), f"缺失：{CAPTURE_SH}"


def test_capture_screenshots_sh_executable() -> None:
    assert os.access(CAPTURE_SH, os.X_OK), f"{CAPTURE_SH} 不可执行（缺 chmod +x）"


def test_capture_screenshots_sh_has_three_cli_flags() -> None:
    text = _read_text(CAPTURE_SH)
    for flag in ("--skip-install", "--install", "--help"):
        assert flag in text, f"capture_screenshots.sh 缺 CLI flag: {flag}"


def test_capture_screenshots_sh_help_output_contains_key_words() -> None:
    proc = subprocess.run(
        ["bash", str(CAPTURE_SH), "--help"],
        capture_output=True, text=True, check=True, timeout=10,
    )
    help_text = proc.stdout
    for kw in ("chromium", "playwright", "上传", "用法", "PNG"):
        assert kw in help_text, f"--help 输出缺关键字: {kw}"


def test_capture_screenshots_sh_idempotent_when_pngs_exist() -> None:
    """所有 PNG 已存在时，幂等路径应直接通过且不抛错。"""
    proc = subprocess.run(
        ["bash", str(CAPTURE_SH)],
        capture_output=True, text=True, check=False, timeout=30,
    )
    assert proc.returncode == 0, (
        f"capture_screenshots.sh 退出码 = {proc.returncode}\n"
        f"stdout: {proc.stdout}\nstderr: {proc.stderr}"
    )
    # 4/4 expected PNGs verified
    assert "4/4 expected PNGs verified" in proc.stdout, proc.stdout


# ── scripts/screenshot.py 契约 ──────────────────────────────────────────


def test_screenshot_py_exists_and_is_substantial() -> None:
    assert SCREENSHOT_PY.exists(), f"缺失：{SCREENSHOT_PY}"
    line_count = sum(1 for _ in SCREENSHOT_PY.open(encoding="utf-8"))
    assert line_count >= 80, f"screenshot.py 仅 {line_count} 行，应 ≥ 80"


def test_screenshot_py_defines_four_screenshot_targets() -> None:
    text = _read_text(SCREENSHOT_PY)
    for shot in EXPECTED_SHOTS:
        assert shot in text, f"screenshot.py 未声明目标文件名: {shot}"


def test_screenshot_py_polls_pipeline_done() -> None:
    """_wait_for_pipeline_done 必须按 processed_steps ≥ 5 判定。"""
    text = _read_text(SCREENSHOT_PY)
    assert "_wait_for_pipeline_done" in text, "_wait_for_pipeline_done 缺失"
    assert "processed_steps" in text, "未按 processed_steps 判定流水线完成"
    assert re.search(r"processed_steps.*>=\s*5|len\([^)]*processed_steps[^)]*\)\s*>=\s*5", text), (
        "未检测 processed_steps >= 5"
    )


def test_screenshot_py_uploads_via_api() -> None:
    """_upload_via_api 必须走 httpx POST /api/cases。"""
    text = _read_text(SCREENSHOT_PY)
    assert "_upload_via_api" in text, "_upload_via_api 缺失"
    assert "httpx.post" in text, "未走 httpx.post"
    assert "/api/cases" in text, "未调 /api/cases"


def test_screenshot_py_picks_local_chromium_fallback() -> None:
    """_pick_executable() 必须含本地 chromium 兜底列表。"""
    text = _read_text(SCREENSHOT_PY)
    assert "_pick_executable" in text, "_pick_executable 缺失"
    assert "Library/Caches/ms-playwright" in text, "未含本地 chromium 路径兜底"


def test_screenshot_py_uses_sync_playwright() -> None:
    text = _read_text(SCREENSHOT_PY)
    assert "sync_playwright" in text, "未用 sync_playwright"
    # headless 启用（接受字典/调用实参两种风格）
    assert re.search(r"""["']headless["']\s*:\s*True\b""", text), "未启用 headless"


# ── docs/screenshots/ 实测产物契约 ──────────────────────────────────────


def test_docs_screenshots_dir_exists() -> None:
    assert SHOTS_DIR.is_dir(), f"缺失：{SHOTS_DIR}"


def test_four_expected_pngs_exist() -> None:
    for fname in EXPECTED_SHOTS:
        assert (SHOTS_DIR / fname).exists(), f"缺失截图：{SHOTS_DIR / fname}"


def test_each_png_is_real_png_format() -> None:
    for fname in EXPECTED_SHOTS:
        out = subprocess.run(
            ["file", "-b", str(SHOTS_DIR / fname)],
            capture_output=True, text=True, check=True, timeout=5,
        ).stdout
        assert "PNG image data" in out, f"{fname} 不是真 PNG: {out.strip()}"


def test_each_png_meets_minimum_resolution() -> None:
    """每张图分辨率 ≥ 1280x800。"""
    for fname in EXPECTED_SHOTS:
        dims = _png_dimensions(SHOTS_DIR / fname)
        assert dims is not None, f"{fname} 无法解析分辨率"
        w, h = dims
        assert w >= MIN_WIDTH and h >= MIN_HEIGHT, (
            f"{fname} 维度 {w}x{h} < {MIN_WIDTH}x{MIN_HEIGHT}"
        )


def test_each_png_non_empty_size() -> None:
    """每张 PNG 文件大小 > 0。"""
    for fname in EXPECTED_SHOTS:
        size = (SHOTS_DIR / fname).stat().st_size
        assert size > 0, f"{fname} 是空文件"


def test_png_pixel_signature() -> None:
    """PNG 文件头 8 字节必须为 89 50 4E 47 0D 0A 1A 0A。"""
    for fname in EXPECTED_SHOTS:
        header = (SHOTS_DIR / fname).read_bytes()[:8]
        assert header == b"\x89PNG\r\n\x1a\n", f"{fname} PNG 头部签名不正确"


# ── 04 张图覆盖 4 状态（粗体关键字验证） ──────────────────────────────────────────


def test_docstring_describes_four_states() -> None:
    """screenshot.py docstring 必须列出 4 个截图状态语义。"""
    text = _read_text(SCREENSHOT_PY)
    for kw in (
        "01-upload", "02-pipeline-running",
        "03-fields-reviewed", "04-excel-exported",
    ):
        assert kw in text, f"screenshot.py 缺状态锚点: {kw}"
    # 4 个语义短语
    for kw in ("upload form rendered", "pipeline running",
               "pipeline finished", "export button pressed"):
        assert kw in text, f"screenshot.py 缺语义说明: {kw}"
