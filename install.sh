#!/usr/bin/env bash
# macOS / Linux：建立虛擬環境、安裝依賴、準備 .env。
set -euo pipefail
cd "$(dirname "$0")"
python3 -m venv .venv
# shellcheck disable=SC1091
source .venv/bin/activate
python -m pip install -U pip
python -m pip install -r requirements.txt
python -m pip install -e . --no-deps
if [ ! -f .env ]; then
  cp .env.example .env
  echo "已建立 .env。金鑰留空也可以先看模擬盤。要實盤再自己填，不要提交。"
fi
echo "安裝完成。下一步：./start.sh"
echo "儀表臺預設 http://127.0.0.1:8787/"
