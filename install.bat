@echo off
rem Chitalka installer. Messages in Russian are printed by install_helper.py
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1
set "PY="
py -3.12 -c "1" >nul 2>nul && set "PY=py -3.12"
if not defined PY py -3.13 -c "1" >nul 2>nul && set "PY=py -3.13"
if not defined PY py -3.11 -c "1" >nul 2>nul && set "PY=py -3.11"
if not defined PY py -3.10 -c "1" >nul 2>nul && set "PY=py -3.10"
if not defined PY python -c "import sys; assert (3,10) <= sys.version_info < (3,14)" >nul 2>nul && set "PY=python"
if not defined PY (
  echo Python 3.12 not found - installing it with winget...
  winget install -e --id Python.Python.3.12 --accept-package-agreements --accept-source-agreements
  set "PY=py -3.12"
)
if not exist ".venv\Scripts\python.exe" (
  echo Creating environment...
  %PY% -m venv .venv
)
if not exist ".venv\Scripts\python.exe" (
  echo ERROR: could not create Python environment.
  pause
  exit /b 1
)
".venv\Scripts\python.exe" install_helper.py --setup %*
pause
