@echo off
setlocal
cd /d "%~dp0"
title Agent Platform Manager

if not exist "%~dp0scripts\start-platform.ps1" (
  echo ERROR: scripts\start-platform.ps1 was not found.
  pause
  exit /b 1
)

:menu
cls
echo ========================================
echo          Agent Platform Manager
echo ========================================
echo Repository: %CD%
echo.
echo [1] Start seven services
echo [2] Show status
echo [3] Follow logs
echo [4] Restart services
echo [5] Stop services, keep data volumes
echo [6] Start services with Model Lab and Agent Lab
echo [0] Exit
echo.
set "ACTION_ARGS="
set /p "CHOICE=Select an action: "

if "%CHOICE%"=="1" set "ACTION_ARGS=-Action Start"
if "%CHOICE%"=="2" set "ACTION_ARGS=-Action Status"
if "%CHOICE%"=="3" set "ACTION_ARGS=-Action Logs"
if "%CHOICE%"=="4" set "ACTION_ARGS=-Action Restart"
if "%CHOICE%"=="5" set "ACTION_ARGS=-Action Stop"
if "%CHOICE%"=="6" set "ACTION_ARGS=-Action Start -WithLabs"
if "%CHOICE%"=="0" exit /b 0
if not defined ACTION_ARGS goto :invalid

powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\start-platform.ps1" %ACTION_ARGS%
set "EXIT_CODE=%ERRORLEVEL%"
echo.
echo Command finished with exit code %EXIT_CODE%.
echo Press any key to return to the menu.
pause >nul
goto :menu

:invalid
echo Invalid selection. Press any key to try again.
pause >nul
goto :menu
