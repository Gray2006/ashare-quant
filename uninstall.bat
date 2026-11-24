@echo off
setlocal EnableExtensions
chcp 65001 >nul
cd /d "%~dp0"
title 量化工作台 - 卸载

echo.
echo 这会删除:
echo   - .venv(程序依赖)
echo   - 桌面快捷方式
echo 不会删除:
echo   - config.json(自选股/设置)
echo   - data(行情缓存/回测记录)
echo   - feedback(待上传的回流样本)
echo.
choice /c YN /m "继续吗"
if errorlevel 2 exit /b 0

if exist ".venv" rmdir /s /q ".venv"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\remove_shortcut.ps1" >nul 2>nul
echo 已卸载。想彻底清空,再手动删 config.json、data、feedback 目录。
pause
exit /b 0
