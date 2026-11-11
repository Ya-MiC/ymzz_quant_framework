#!/usr/bin/env bash
# 同時啟動儀表臺與紙上 runner。關掉這個終端會一併停掉儀表臺。
set -euo pipefail
cd "$(dirname "$0")"
if [ ! -x .venv/bin/python ]; then
  echo "請先執行 ./install.sh"
  exit 1
fi
# shellcheck disable=SC1091
source .venv/bin/activate
set -a
# shellcheck disable=SC1091
[ -f .env ] && source .env
set +a
export PYTHONPATH="${PYTHONPATH:-src}"
HOST="${YMZZ_DASHBOARD_HOST:-127.0.0.1}"
PORT="${YMZZ_DASHBOARD_PORT:-8787}"
python -m ymzz.cli dashboard --host "$HOST" --port "$PORT" &
dash_pid=$!
cleanup() { kill "$dash_pid" 2>/dev/null || true; }
trap cleanup EXIT INT TERM
echo "儀表臺 http://${HOST}:${PORT}/"
echo "紙上 runner 啟動。沒有 kraken 執行檔時，成交用公開報價在純 Python 裡模擬。"
python -m ymzz.cli run --mode paper
