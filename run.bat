@echo off
REM Launch OttDeck.
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\pythonw.exe" (
  echo Not set up yet - run setup.bat first.
  pause
  exit /b 1
)
start "" ".venv\Scripts\pythonw.exe" "%~dp0app.py"
endlocal
