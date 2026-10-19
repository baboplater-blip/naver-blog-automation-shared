@echo off
setlocal
set "NBA_PROFILE=public-share"
set "PYTHONUTF8=1"
set "PYTHONPATH=%~dp0"
cd /d "%~dp0"
if exist "%~dp0..\.venv\Scripts\python.exe" (
  "%~dp0..\.venv\Scripts\python.exe" run_cli.py %*
) else (
  py run_cli.py %*
)
endlocal
