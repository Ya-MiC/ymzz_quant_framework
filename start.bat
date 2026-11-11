@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo 請先在 PowerShell 執行 .\install.ps1
  exit /b 1
)
if exist ".env" (
  for /f "usebackq eol=# tokens=1,* delims==" %%A in (".env") do (
    if not "%%A"=="" set "%%A=%%B"
  )
)
if "%YMZZ_DASHBOARD_HOST%"=="" set "YMZZ_DASHBOARD_HOST=127.0.0.1"
if "%YMZZ_DASHBOARD_PORT%"=="" set "YMZZ_DASHBOARD_PORT=8787"
echo 儀表臺 http://%YMZZ_DASHBOARD_HOST%:%YMZZ_DASHBOARD_PORT%/
start "ymzz-dashboard" ".venv\Scripts\python.exe" -m ymzz.cli dashboard --host %YMZZ_DASHBOARD_HOST% --port %YMZZ_DASHBOARD_PORT%
".venv\Scripts\python.exe" -m ymzz.cli run --mode paper
