@echo off
REM Windows setup wrapper: use the independent PowerShell 7.6.5 installation.
setlocal
set "PWSH=%USERPROFILE%\Tools\PowerShell\7.6.5\pwsh.exe"
if not exist "%PWSH%" (
    echo PowerShell 7.6.5 was not found: %PWSH%
    pause
    exit /b 1
)
"%PWSH%" -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\setup.ps1"
if errorlevel 1 pause

