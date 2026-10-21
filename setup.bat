@echo off
REM OttDeck one-click setup - double-click this file.
setlocal
cd /d "%~dp0"
title OttDeck setup
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\setup.ps1" %*
if errorlevel 1 (
  echo.
  echo   Setup did not finish. The error is above.
  pause
)
endlocal
