@echo off
setlocal
chcp 65001 >nul
set "PYTHONUTF8=1"
set "ROOT=%~dp0"
cd /d "%ROOT%"

if not exist "%ROOT%py\Scripts\python.exe" (
  echo Python environment not found.
  echo Run: powershell -ExecutionPolicy Bypass -File setup_env.ps1
  pause
  exit /b 1
)

"%ROOT%py\Scripts\python.exe" "%ROOT%main.py" %*
pause
