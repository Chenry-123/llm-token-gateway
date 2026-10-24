@echo off
chcp 65001 >nul
cd /d "%~dp0"
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8

if not exist ".venv\Scripts\python.exe" (
  echo 还没有运行环境，请先双击一次 run.bat 把它建好。
  pause
  exit /b 1
)

".venv\Scripts\python.exe" selftest.py
echo.
echo ===== 自验结束，按任意键关闭 =====
pause >nul
