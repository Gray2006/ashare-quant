@echo off
setlocal EnableExtensions
chcp 65001 >nul
cd /d "%~dp0"
title 量化工作台 - 安装

echo.
echo  ============================================
echo   量化工作台 - 一键安装
echo   (公开行情 + 技术分析 + 选股 + 回测)
echo  ============================================
echo.

rem ---------- 1. 找 Python ----------
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
echo [1/4] 找到 Python
%PY% -c "import sys; print('      ' + sys.version.split()[0] + '  ' + sys.executable)"

rem ---------- 2. 建虚拟环境 ----------
if exist ".venv\Scripts\python.exe" (
    echo [2/4] 已有 .venv,跳过
) else (
    echo [2/4] 创建虚拟环境 .venv ...
    %PY% -m venv .venv
    if errorlevel 1 (
        echo [X] 创建虚拟环境失败。确认磁盘空间和权限后重试。
        pause
        exit /b 1
    )
)
set "VPY=.venv\Scripts\python.exe"

rem ---------- 3. 装依赖 ----------
if "%GRAY_SKIP_PIP%"=="1" (
    echo [3/4] 已设置 GRAY_SKIP_PIP=1,跳过依赖安装
    goto after_pip
)
echo [3/4] 安装依赖(第一次会下载 PySide6 等,约 1~3 分钟)...
"%VPY%" -m pip install --upgrade pip --quiet
"%VPY%" -m pip install -r requirements.txt
if errorlevel 1 (
    echo.
    echo [X] 依赖安装失败。常见原因:
    echo     1. 网络问题:重试一次,或换手机热点
    echo     2. 公司电脑限制:用管理员身份运行
    echo.
    pause
    exit /b 1
)
:after_pip

rem ---------- 4. 桌面快捷方式 ----------
echo [4/4] 创建桌面快捷方式 ...
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\make_shortcut.ps1" >nul 2>nul
if errorlevel 1 (
    echo      快捷方式没建成,不影响使用;以后双击 start.bat 即可。
) else (
    echo       桌面已生成「量化工作台」
)

echo.
echo  ============================================
echo   安装完成
echo   以后双击桌面「量化工作台」,或 start.bat
echo  ============================================
echo.

if "%GRAY_NO_LAUNCH%"=="1" (
    echo 已设置 GRAY_NO_LAUNCH=1,不自动启动
    exit /b 0
)
start "" ".venv\Scripts\pythonw.exe" run.py
exit /b 0
