@echo off
setlocal EnableExtensions
chcp 65001 >nul
cd /d "%~dp0"
title 量化工作台

rem VPY 不带 .exe,后面拼 w.exe / .exe
if exist ".venv\Scripts\python.exe" (
    set "VPY=.venv\Scripts\python"
) else (
    set "VPY=python"
)

if "%1"=="--console" (
    "%VPY%.exe" run.py
) else (
    start "" "%VPY%w.exe" run.py
)
exit /b 0
