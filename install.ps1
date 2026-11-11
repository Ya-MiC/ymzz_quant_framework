# Windows：建立虛擬環境、安裝依賴、準備 .env。
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
if (Get-Command py -ErrorAction SilentlyContinue) {
  py -3 -m venv .venv
} else {
  python -m venv .venv
}
$python = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
& $python -m pip install -U pip
& $python -m pip install -r requirements.txt
& $python -m pip install -e . --no-deps
if (-not (Test-Path .env)) {
  Copy-Item .env.example .env
  Write-Host "已建立 .env。金鑰留空也可以先看模擬盤。不要把 .env 提交到 Git。"
}
Write-Host "安裝完成。下一步：.\start.bat"
Write-Host "儀表臺預設 http://127.0.0.1:8787/"
