@echo off
setlocal EnableExtensions
chcp 65001 >nul
title 量化工作台 - 一键在线安装

echo.
echo  ============================================
echo   量化工作台 - 一键在线安装
echo   自动从 GitHub 拉最新版,装到 %USERPROFILE%\ashare-quant
echo  ============================================
echo.

rem ---------- 找 Python(没有就直接说清楚) ----------
set "PY="
py -3 -c "import sys; sys.exit(0 if sys.version_info>=(3,10) else 1)" >nul 2>nul
if not errorlevel 1 set "PY=py -3"
if not defined PY (
    python -c "import sys; sys.exit(0 if sys.version_info>=(3,10) else 1)" >nul 2>nul
    if not errorlevel 1 set "PY=python"
)
if not defined PY (
    echo [X] 没找到 Python 3.10 以上版本。
    echo.
    echo     请先装 Python:https://www.python.org/downloads/
    echo     安装时勾选 "Add python.exe to PATH",然后重新双击本文件。
    echo.
    pause
    exit /b 1
)

rem ---------- 下载并执行安装脚本 ----------
set "PS1=%TEMP%\ashare-quant-install-online.ps1"
del "%PS1%" >nul 2>nul
echo [1/2] 下载安装脚本 ...
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "Invoke-WebRequest -Uri 'https://raw.githubusercontent.com/Gray2006/ashare-quant/main/scripts/install_online.ps1' -OutFile '%PS1%' -UseBasicParsing -TimeoutSec 60"
if errorlevel 1 (
    echo.
    echo [X] 下载安装脚本失败。检查网络后重试,或改用 README 里的「下载 ZIP 手动安装」。
    pause
    exit /b 1
)

echo [2/2] 开始安装 ...
powershell -NoProfile -ExecutionPolicy Bypass -File "%PS1%"
set "RC=%ERRORLEVEL%"
del "%PS1%" >nul 2>nul

if "%RC%"=="2" (
    pause
    exit /b 1
)
if not "%RC%"=="0" (
    echo.
    echo [X] 安装没有完成,按上面的提示排查。
    pause
    exit /b 1
)

echo.
echo  ============================================
echo   安装完成,桌面已生成「量化工作台」
echo  ============================================
echo.
pause
exit /b 0
