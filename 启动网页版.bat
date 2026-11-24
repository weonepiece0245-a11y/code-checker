@echo off
chcp 65001 >nul
cd /d "%~dp0"
title 代码检查助手

rem 自动更新下载了新的启动器时，先替换自己再重新启动
if exist "%~f0.new" (move /y "%~f0.new" "%~f0" >nul & start "" "%~f0" & exit /b)

set "PY=python"
python --version >nul 2>nul
if errorlevel 1 (
  set "PY=py"
  py --version >nul 2>nul
  if errorlevel 1 (
    echo 没有找到可用的 Python。
    echo 请到 https://www.python.org/downloads/ 下载安装，
    echo 安装时勾选 "Add python.exe to PATH"，装好后再双击本文件。
    pause
    exit /b 1
  )
)

rem 在桌面创建或刷新快捷方式
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0create_shortcut.ps1" >nul 2>nul

rem 检查更新
%PY% -m codecheck.updater
if errorlevel 10 (start "" "%~f0" & exit /b)

if not exist ".deps_ok" (
  echo 正在安装需要的组件，请稍等……
  %PY% -m pip install -r requirements.txt
  if errorlevel 1 (
    echo 安装失败，请把上面的错误信息截图发给 Claude。
    pause
    exit /b 1
  )
  echo ok> .deps_ok
)

%PY% -m codecheck.web.server
if errorlevel 10 (
  echo 正在用新版本重新启动……
  set "CODECHECK_NO_BROWSER=1"
  start "" "%~f0"
  exit /b
)
echo.
echo 服务已停止。如果上面有错误信息，请截图发给 Claude。
pause
