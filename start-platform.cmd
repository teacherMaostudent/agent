@echo off
setlocal
cd /d "%~dp0"
title Agent Platform Launcher

if not exist "%~dp0scripts\start-platform.ps1" (
  echo ERROR: scripts\start-platform.ps1 was not found.
  echo Launcher directory: %~dp0
  goto :failed
)

echo Repository: %CD%
echo Starting the seven-service Agent Platform...
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\start-platform.ps1" -Action Start -WithIdentity
set "EXIT_CODE=%ERRORLEVEL%"
echo.

if not "%EXIT_CODE%"=="0" (
  echo Startup failed with exit code %EXIT_CODE%.
  goto :failed
)

echo Startup and health verification completed.
echo Press any key to close this window.
pause >nul
exit /b 0

:failed
echo Review the error above. This window will remain open until you press a key.
pause >nul
exit /b 1
