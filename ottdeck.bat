@echo off
REM OttDeck command line.
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Not set up yet - run setup.bat first.
  pause
  exit /b 1
)
".venv\Scripts\python.exe" -m ottdeck %*
endlocal
