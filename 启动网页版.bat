@echo off
chcp 65001 >nul
cd /d "%~dp0"
title 代码检查助手

rem 自动更新下载了新的启动器时，先替换自己再重新启动
if exist "%~f0.new" (move /y "%~f0.new" "%~f0" >nul & start "" "%~f0" %* & exit /b)

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

echo 正在检查更新……
%PY% -m codecheck.updater
if errorlevel 10 (start "" "%~f0" %* & exit /b)

%PY% -m codecheck.deps
if errorlevel 1 (
  pause
  exit /b 1
)

rem 加参数 web 可以用浏览器版：启动网页版.bat web
if /i "%~1"=="web" goto web

echo 正在打开代码检查助手……
%PY% -m codecheck.app --detach
if errorlevel 2 (
  echo.
  echo 桌面窗口打不开，改用浏览器打开。请把上面的信息截图发给 Claude。
  goto web
)
exit /b 0

:web
%PY% -m codecheck.web.server
if errorlevel 10 (
  echo 正在用新版本重新启动……
  set "CODECHECK_NO_BROWSER=1"
  start "" "%~f0" web
  exit /b
)
echo.
echo 服务已停止。如果上面有错误信息，请截图发给 Claude。
pause
