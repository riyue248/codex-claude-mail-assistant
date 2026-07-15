@echo off
setlocal
python "%~dp0codex_email_notify.py" test
if errorlevel 1 pause
