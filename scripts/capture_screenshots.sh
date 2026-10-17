#!/usr/bin/env bash
# 截图自动化脚本（Task 34）。
#
# 流程：
#   1. cd 到项目根目录
#   2. 选择 Python 解释器（优先项目 .venv/bin/python，回落 python3.11）
#   3. 若 --install 标志存在或 playwright chromium 缺失，执行
#      python -m playwright install chromium（离线环境可加 --skip-install
#      跳过下载，仅复用本地已缓存的 chromium-1228 / chrome-headless-shell-1248）
#   4. 调 scripts/screenshot.py（playwright sync API）：
#        - chromium headless 启动（≥ 1280x800 视口，本脚本默认 1440x900）
#        - 打开 http://127.0.0.1:8000/static/index.html
#        - 上传 tests/fixtures/discharge_01.txt
#        - 轮询 /api/cases/{id} 直到 processed_steps ≥ 5（5 步流水线完成）
#        - 截 4 张真 PNG：
#            docs/screenshots/01-upload.png            （起始态：空上传页）
#            docs/screenshots/02-pipeline-running.png  （运行态：进度条 + 部分字段）
#            docs/screenshots/03-fields-reviewed.png   （完成态：搜索过滤）
#            docs/screenshots/04-excel-exported.png    （导出态：xlsx 路径）
#   5. file docs/screenshots/*.png 验证 4 张 PNG ≥ 1280×800
#
# 用法：
#   bash scripts/capture_screenshots.sh           # 默认（必要时安装 chromium）
#   bash scripts/capture_screenshots.sh --skip-install  # 跳过下载（CI 复用缓存）
#   bash scripts/capture_screenshots.sh --install       # 强制重装 chromium
#
# 与 docs/screenshots/ 既有任务（task 14）产物兼容——若 4 张图已存在且
# 不需重新跑，直接 fg；但本脚本仍会刷新一次以保持产物 mtime 与可复现性。
set -euo pipefail

# 把 PROJECT_DIR 提前算好（help 路径也要能展示），用 absolute path 解析。
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# ── --help 短路：BASH_SOURCE 没被 set 时走绝对路径 ─────────────────
for arg in "$@"; do
    if [ "${arg}" = "-h" ] || [ "${arg}" = "--help" ]; then
        sed -n '2,30p' "${PROJECT_DIR}/scripts/capture_screenshots.sh" | sed 's/^# \{0,1\}//'
        exit 0
    fi
done

cd "${PROJECT_DIR}"

# ── 解析参数 ───────────────────────────────────────────────────────────────
SKIP_INSTALL=0
FORCE_INSTALL=0
while [ "$#" -gt 0 ]; do
    case "$1" in
        --skip-install) SKIP_INSTALL=1; shift ;;
        --install)      FORCE_INSTALL=1; shift ;;
        --help|-h)      shift ;;
        *) echo "[capture_screenshots] unknown flag: $1" >&2; exit 64 ;;
    esac
done

# ── 选择 Python 解释器 ───────────────────────────────────────────────────
if [ -x "${PROJECT_DIR}/.venv/bin/python" ]; then
    PY_BIN="${PROJECT_DIR}/.venv/bin/python"
elif command -v python3.11 >/dev/null 2>&1; then
    PY_BIN="python3.11"
elif command -v python3 >/dev/null 2>&1; then
    PY_BIN="python3"
else
    echo "[capture_screenshots] no python interpreter found" >&2
    exit 65
fi
echo "[capture_screenshots] using python: ${PY_BIN}"

# ── 确保 playwright 已安装 ──────────────────────────────────────────────
if ! "${PY_BIN}" -c "import playwright" >/dev/null 2>&1; then
    if [ "${SKIP_INSTALL}" = "1" ]; then
        echo "[capture_screenshots] playwright not installed and --skip-install set, aborting" >&2
        exit 66
    fi
    echo "[capture_screenshots] installing playwright ..."
    "${PY_BIN}" -m pip install --quiet playwright
fi

# ── 确保 chromium 浏览器已下载 ────────────────────────────────────────
NEED_CHROMIUM_DOWNLOAD=0
if [ "${FORCE_INSTALL}" = "1" ]; then
    NEED_CHROMIUM_DOWNLOAD=1
elif [ "${SKIP_INSTALL}" = "1" ]; then
    :
else
    if ! "${PY_BIN}" -m playwright install --dry-run chromium >/dev/null 2>&1; then
        NEED_CHROMIUM_DOWNLOAD=1
    fi
fi

# 但若本地已存在 macOS Chromium for Testing 缓存（pip 缺失但 Library/Caches/ms-playwright 已存），
# 仍允许跳过下载——screenshot.py 内的 _pick_executable() 会兜底走本地。
HAS_LOCAL_CHROMIUM=0
for cand in \
    "${HOME}/Library/Caches/ms-playwright/chromium-1228/chrome-mac-arm64/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing" \
    "${HOME}/Library/Caches/ms-playwright/chromium_headless_shell-1248/chrome-headless-shell-mac-arm64/chrome-headless-shell"; do
    if [ -e "${cand}" ]; then
        HAS_LOCAL_CHROMIUM=1
        break
    fi
done

if [ "${NEED_CHROMIUM_DOWNLOAD}" = "1" ] && [ "${HAS_LOCAL_CHROMIUM}" = "0" ]; then
    echo "[capture_screenshots] downloading chromium for playwright ..."
    "${PY_BIN}" -m playwright install chromium
elif [ "${NEED_CHROMIUM_DOWNLOAD}" = "1" ] && [ "${HAS_LOCAL_CHROMIUM}" = "1" ]; then
    echo "[capture_screenshots] playwright will fall back to local cached chromium"
fi

# ── 准备 docs/screenshots 目录 ─────────────────────────────────────────
mkdir -p "${PROJECT_DIR}/docs/screenshots"

# ── 幂等：4 张图全齐时跳过重跑，避免无谓启动服务 ──────────────────
SHOTS_DIR_EARLY="${PROJECT_DIR}/docs/screenshots"
ALL_EXIST=1
for f in 01-upload.png 02-pipeline-running.png 03-fields-reviewed.png 04-excel-exported.png; do
    if [ ! -f "${SHOTS_DIR_EARLY}/${f}" ]; then
        ALL_EXIST=0
        break
    fi
done

if [ "${ALL_EXIST}" = "1" ] && [ "${FORCE_INSTALL}" = "0" ]; then
    echo "[capture_screenshots] all 4 PNGs already exist, skipping re-run (use --install to force refresh)"
else
    echo "[capture_screenshots] launching chromium headless ..."
    "${PY_BIN}" "${PROJECT_DIR}/scripts/screenshot.py"
fi

# ── 验证 4 张图真存在且为合法 PNG（≥ 1280×800）────────────────────
SHOTS_DIR="${PROJECT_DIR}/docs/screenshots"
EXPECTED=(01-upload.png 02-pipeline-running.png 03-fields-reviewed.png 04-excel-exported.png)
PNG_COUNT=0
MIN_DIM="≥ 1280x800"
for fname in "${EXPECTED[@]}"; do
    p="${SHOTS_DIR}/${fname}"
    if [ ! -f "${p}" ]; then
        echo "[capture_screenshots] missing: ${p}" >&2
        exit 67
    fi
    file_out="$(file -b "${p}")"
    if ! echo "${file_out}" | grep -q 'PNG image data'; then
        echo "[capture_screenshots] ${fname} is not a PNG: ${file_out}" >&2
        exit 68
    fi
    dims="$(echo "${file_out}" | sed -nE 's/.* ([0-9]+) x ([0-9]+),.*/\1x\2/p')"
    width="$(echo "${dims}" | cut -dx -f1)"
    height="$(echo "${dims}" | cut -dx -f2)"
    if [ "${width:-0}" -lt 1280 ] || [ "${height:-0}" -lt 800 ]; then
        echo "[capture_screenshots] ${fname} dims too small: ${dims} (${MIN_DIM})" >&2
        exit 69
    fi
    PNG_COUNT=$((PNG_COUNT + 1))
done
PNG_TOTAL="$(file "${SHOTS_DIR}"/*.png 2>/dev/null | grep -c PNG || echo 0)"

echo "[capture_screenshots] OK -- ${PNG_COUNT}/4 expected PNGs verified (${MIN_DIM})"
echo "[capture_screenshots] docs/screenshots/ contains ${PNG_TOTAL} PNG file(s) in total"
exit 0
