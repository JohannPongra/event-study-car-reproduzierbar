@echo off
setlocal
cd /d "%~dp0"
set PYTHONUTF8=1
if not exist "..\.venv\Scripts\python.exe" (
  echo Bitte zuerst INSTALLIEREN.bat im Projektordner ausfuehren.
  if not "%EVENT_STUDY_NO_PAUSE%"=="1" pause
  exit /b 1
)
"..\.venv\Scripts\python.exe" -B erstellen.py %*
set "ergebnis=%errorlevel%"
if not "%EVENT_STUDY_NO_PAUSE%"=="1" pause
exit /b %ergebnis%
