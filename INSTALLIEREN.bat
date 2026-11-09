@echo off
setlocal
cd /d "%~dp0"
set PYTHONUTF8=1
if exist ".venv\Scripts\python.exe" goto pakete
where py >nul 2>nul
if errorlevel 1 (
  echo Bitte Python 3.12 oder 3.14 in 64 Bit mit Python Launcher installieren.
  goto fehler
)
set "PYTHON_VERSION="
py -3.14 --version >nul 2>nul
if not errorlevel 1 set "PYTHON_VERSION=3.14"
if not defined PYTHON_VERSION (
  py -3.12 --version >nul 2>nul
  if not errorlevel 1 set "PYTHON_VERSION=3.12"
)
if not defined PYTHON_VERSION (
  echo Python 3.12 oder 3.14 wurde nicht gefunden.
  goto fehler
)
py -%PYTHON_VERSION% -m venv .venv
if errorlevel 1 goto fehler
:pakete
".venv\Scripts\python.exe" -c "import sys,struct; assert sys.version_info[:2] in ((3,12),(3,14)) and struct.calcsize('P') == 8, 'Python 3.12 oder 3.14 in 64 Bit erforderlich'"
if errorlevel 1 goto fehler
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 goto fehler
echo Installation abgeschlossen. Jetzt REPRODUZIEREN.bat starten.
if not "%EVENT_STUDY_NO_PAUSE%"=="1" pause
exit /b 0
:fehler
echo Installation nicht abgeschlossen.
if not "%EVENT_STUDY_NO_PAUSE%"=="1" pause
exit /b 1
