@echo off
setlocal
python "%~dp0codex_email_notify.py" setup
if errorlevel 1 (
  echo.
  echo Configuration failed. See the error above.
  pause
  exit /b 1
)
echo.
pause
