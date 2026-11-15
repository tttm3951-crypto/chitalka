@echo off
rem Сборка «Chitalka-Setup.exe» (нужен Python 3.12). Результат — в папке dist.
cd /d "%~dp0"
if not exist ".build-venv\Scripts\python.exe" py -3.12 -m venv .build-venv
".build-venv\Scripts\python.exe" -m pip install -q pyinstaller
".build-venv\Scripts\pyinstaller.exe" --noconfirm --onefile --windowed --name Chitalka-Setup ^
  --icon chitalka.ico --add-data "components.json;." --add-data "chitalka.ico;." installer.py
