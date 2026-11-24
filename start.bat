@echo off
setlocal EnableExtensions
chcp 65001 >nul
cd /d "%~dp0"
title 量化工作台

if exist ".venv\Scripts\python.exe" (
    set "VPY=.venv\Scripts\python.exe"
) else (
    set "VPY=python"
)

if "%1"=="--console" (
    "%VPY%" run.py
) else (
    start "" "%VPY%w.exe" run.py
)
exit /b 0
