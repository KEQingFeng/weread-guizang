@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
set "PORT=8770"

echo.
echo   归藏 · 微信读书导出
echo   目录：%CD%
echo.

REM 解释器：Windows 的虚拟环境在 Scripts\python.exe（不是 bin/python）
set "PY=%CD%\.venv\Scripts\python.exe"

if not exist "%PY%" (
  REM 优先用 py 启动器：PATH 里的 python 可能是应用商店的占位程序
  set "BASEPY="
  where py >nul 2>nul && set "BASEPY=py -3"
  if not defined BASEPY (
    where python >nul 2>nul && set "BASEPY=python"
  )
  if not defined BASEPY (
    echo   没找到 Python。请先安装 Python 3.10 或更新版本，
    echo   安装时勾选 "Add python.exe to PATH"，然后重新双击本脚本。
    echo.
    pause
    exit /b 1
  )
  echo   第一次运行：创建虚拟环境并安装依赖。
  echo   其中 Chromium 约 370MB，慢的话先给终端挂上代理。
  echo.
  %BASEPY% -m venv .venv
  if errorlevel 1 ( echo   创建虚拟环境失败。 & pause & exit /b 1 )
  "%PY%" -m pip install --upgrade pip
  "%PY%" -m pip install -r requirements.txt
  if errorlevel 1 ( echo   安装依赖失败。 & pause & exit /b 1 )
  "%PY%" -m playwright install chromium
  if errorlevel 1 ( echo   安装 Chromium 失败。 & pause & exit /b 1 )
  echo.
)

REM 已经在跑：直接开界面，不再起第二个
netstat -ano | findstr ":%PORT%" | findstr "LISTENING" >nul 2>nul
if not errorlevel 1 (
  echo   归藏已在运行 → http://127.0.0.1:%PORT%
  start "" "http://127.0.0.1:%PORT%"
  exit /b 0
)

echo   归藏启动中 → http://127.0.0.1:%PORT%
REM 服务留在最小化的窗口里：出问题能直接看到报错，不会「点了没反应」
start "归藏服务" /min "%PY%" ui_server.py --port %PORT%

for /l %%i in (1,1,30) do (
  "%PY%" -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:%PORT%/',timeout=1)" >nul 2>nul
  if not errorlevel 1 (
    start "" "http://127.0.0.1:%PORT%"
    exit /b 0
  )
  ping -n 2 127.0.0.1 >nul
)

echo.
echo   启动超时：看一眼最小化的「归藏服务」窗口里最后几行报错。
echo.
pause
