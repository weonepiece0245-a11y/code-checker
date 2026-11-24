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

rem 加参数 web 用浏览器版；加参数 console 在这个窗口里显示启动过程（排查问题用）
if /i "%~1"=="web" goto web
if /i "%~1"=="console" (
  %PY% -m codecheck.launcher --console
  pause
  exit /b
)

rem 正常情况：交给没有黑色窗口的启动器（pythonw / pyw），这个窗口马上关闭
start "" %PY%w -m codecheck.launcher
if errorlevel 1 (
  echo 无窗口启动失败，改用命令行模式……
  %PY% -m codecheck.launcher --console
  pause
)
exit /b 0

:web
%PY% -m codecheck.updater
if errorlevel 10 (start "" "%~f0" web & exit /b)
%PY% -m codecheck.deps
if errorlevel 1 (
  pause
  exit /b 1
)
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
