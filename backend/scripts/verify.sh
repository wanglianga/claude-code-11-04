#!/usr/bin/env bash
# 一键验证：pytest 验收套件 + 端到端种子流程（含账本守恒与只追加保护断言）
set -euo pipefail
cd "$(dirname "$0")/.."

# 解释器：优先显式 PYTHON，其次 PATH 中的 python，最后回退 python3
PYTHON_BIN="${PYTHON:-$(command -v python || command -v python3)}"
export RECON_DATABASE_URL="${RECON_DATABASE_URL:-postgresql+asyncpg://recon:recon@localhost:5432/recon}"
export PYTHONPATH="$(pwd)"

echo "=========== 1/2 pytest 验收套件（27 用例） ==========="
"$PYTHON_BIN" -m pytest tests/ -q

echo "=========== 2/2 端到端场景种子（重置 schema 后装载） ==========="
"$PYTHON_BIN" scripts/seed.py --reset

echo
echo "VERIFY_DONE: 全部验收通过"
