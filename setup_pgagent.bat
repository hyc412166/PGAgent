@echo off
REM Windows 安装包装器：调用统一的 PowerShell 安装流程。
setlocal
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\setup.ps1"
if errorlevel 1 pause

