@echo off
setlocal
cd /d "%~dp0"
set PYTHONUTF8=1
if not exist ".venv\Scripts\python.exe" (
  echo Bitte zuerst INSTALLIEREN.bat ausfuehren.
  if not "%EVENT_STUDY_NO_PAUSE%"=="1" pause
  exit /b 1
)
".venv\Scripts\python.exe" -B -u analyse.py
set "ergebnis=%errorlevel%"
if not "%ergebnis%"=="0" echo FEHLER: Die Reproduktion wurde nicht erfolgreich abgeschlossen.
if not "%EVENT_STUDY_NO_PAUSE%"=="1" pause
exit /b %ergebnis%
