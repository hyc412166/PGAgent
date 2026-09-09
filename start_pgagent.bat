@echo off
REM Windows launcher: open Windows Terminal and use independent PowerShell 7.6.5.
setlocal
set "PWSH=%USERPROFILE%\Tools\PowerShell\7.6.5\pwsh.exe"
set "WT=%LOCALAPPDATA%\Microsoft\Windows Terminal\wt.exe"
if not exist "%PWSH%" (
    echo PowerShell 7.6.5 was not found: %PWSH%
    pause
    exit /b 1
)
if not exist "%WT%" (
    echo Windows Terminal was not found: %WT%
    pause
    exit /b 1
)
"%WT%" -w new --title "PGAgent Backend" "%PWSH%" -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\start.ps1"
if errorlevel 1 pause

