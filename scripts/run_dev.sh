#!/usr/bin/env bash
# 本地开发一键启动脚本
# - 创建 venv（如不存在）
# - 安装依赖（pip install -e .）
# - 启动 uvicorn (python -m case_bill_validation)
# - sleep 后 curl /api/health 自检
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${PROJECT_DIR}"

VENV_DIR="${PROJECT_DIR}/.venv"
PYTHON_BIN="${VENV_DIR}/bin/python"

if [ ! -x "${PYTHON_BIN}" ]; then
    echo "[run_dev] creating venv ..."
    python3 -m venv "${VENV_DIR}"
fi

echo "[run_dev] installing dependencies (editable) ..."
"${PYTHON_BIN}" -m pip install --upgrade pip >/dev/null
"${PYTHON_BIN}" -m pip install -e ".[dev]" >/dev/null

if [ ! -f "${PROJECT_DIR}/.env" ] && [ -f "${PROJECT_DIR}/.env.example" ]; then
    echo "[run_dev] .env 不存在，从 .env.example 复制（请按需修改）"
    cp "${PROJECT_DIR}/.env.example" "${PROJECT_DIR}/.env"
fi

echo "[run_dev] starting uvicorn ..."
"${PYTHON_BIN}" -m case_bill_validation &
UVICORN_PID=$!
trap 'kill ${UVICORN_PID} 2>/dev/null || true' EXIT

sleep 4

echo "[run_dev] probing /api/health ..."
curl -fsS "http://127.0.0.1:${APP_PORT:-8000}/api/health" || {
    echo "[run_dev] health check failed"
    exit 1
}

echo "[run_dev] OK -- press Ctrl-C to stop uvicorn"
wait "${UVICORN_PID}"