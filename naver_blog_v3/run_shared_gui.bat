@echo off
setlocal
set "NBA_PROFILE=public-share"
set "PYTHONUTF8=1"
set "PYTHONPATH=%~dp0"
cd /d "%~dp0"
if not exist "%~dp0..\profiles\public-share" (
  echo Run py tools\bootstrap_shared.py from the repository root first.
  exit /b 1
)
if exist "%~dp0..\.venv\Scripts\python.exe" (
  "%~dp0..\.venv\Scripts\python.exe" gui_app.py
) else (
  py gui_app.py
)
endlocal
