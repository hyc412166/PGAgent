@echo off
REM Windows 启动包装器：把控制权交给 PowerShell 主脚本并在失败时暂停窗口。
setlocal
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\start.ps1"
if errorlevel 1 pause

