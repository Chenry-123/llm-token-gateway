@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8

if not exist ".venv\Scripts\python.exe" (
  echo [1/2] 第一次运行，正在建独立环境 ^(只做一次，约 1 分钟^) ...
  py -3 -m venv .venv 2>nul || python -m venv .venv
  if not exist ".venv\Scripts\python.exe" (
    echo [x] 建环境失败：没找到可用的 Python。
    pause
    exit /b 1
  )
  echo [2/2] 正在装依赖 fastapi uvicorn httpx fastmcp ...
  ".venv\Scripts\python.exe" -m pip install --upgrade pip --quiet
  ".venv\Scripts\python.exe" -m pip install fastapi uvicorn httpx fastmcp --quiet
)

if /i "%~1"=="test" (
  ".venv\Scripts\python.exe" selftest.py
  echo.
  echo ===== 自验结束，按任意键关闭 =====
  pause >nul
  exit /b 0
)

if /i "%~1"=="dashboard" (
  ".venv\Scripts\python.exe" dashboard.py
  echo.
  echo ===== 看板已生成 dashboard.html，双击即可打开 =====
  pause >nul
  exit /b 0
)

echo.
echo   网关已启动： http://127.0.0.1:8317
echo   Qoder 里 Base URL 填：  http://127.0.0.1:8317/v1
echo   Claude Code 里填：      ANTHROPIC_BASE_URL=http://127.0.0.1:8317
echo.
echo   关闭窗口即停止（或按 Ctrl+C）。
echo.
".venv\Scripts\python.exe" -m uvicorn server:app --host 127.0.0.1 --port 8317
pause
