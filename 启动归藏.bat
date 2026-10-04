@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
REM 端口可以按 GUIZANG_PORT 改（8770 被别的应用占着时用得上）；MCP 适配器读的是同一个变量
if defined GUIZANG_PORT (set "PORT=%GUIZANG_PORT%") else (set "PORT=8770")

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

REM 端口上有人：先问它是哪份代码，再决定开界面、接手，还是别动它。
REM 原来只看端口有没有人听 —— 一个早就跑着的旧后端会把新代码挡住：界面从磁盘现读、
REM 路由表却是旧进程装进内存的，新功能一律 404，而页面只报「后端没启动」。
set "ACT="
set "NOTE="
for /f "usebackq delims=" %%a in (`"%PY%" platform_compat.py verdict %PORT% 2^>nul`) do (
  for /f "tokens=1,* delims=|" %%b in ("%%a") do (set "ACT=%%b" & set "NOTE=%%c")
)

if "%ACT%"=="" (
  echo   问不出端口上是哪份代码，按原样开界面 → http://127.0.0.1:%PORT%
  start "" "http://127.0.0.1:%PORT%"
  exit /b 0
)
if "%ACT%"=="reuse" (
  echo   %NOTE% → http://127.0.0.1:%PORT%
  start "" "http://127.0.0.1:%PORT%"
  exit /b 0
)
if "%ACT%"=="busy" (
  echo   %NOTE%
  echo   界面还是在这儿开 → http://127.0.0.1:%PORT%；跑完任务后页顶会出现「换新后端」，点一下就好
  start "" "http://127.0.0.1:%PORT%"
  exit /b 0
)
if "%ACT%"=="stranger" (
  echo   %NOTE%
  echo   要么把占用 %PORT% 的程序退掉，要么换个端口：set GUIZANG_PORT=8899 后重新双击本脚本
  pause
  exit /b 1
)

REM free：直接起；takeover：带 --takeover 起，让新进程把旧后端那份端口接过来
set "FLAG="
if "%ACT%"=="takeover" (
  echo   %NOTE%
  set "FLAG=--takeover"
)
echo   归藏启动中 → http://127.0.0.1:%PORT%
REM 服务留在最小化的窗口里：出问题能直接看到报错，不会「点了没反应」
start "归藏服务" /min "%PY%" ui_server.py --port %PORT% %FLAG%

REM 认的是代码指纹而不是「端口通了」：交接那几秒旧后端也答话，只看通不通
REM 会把界面开在旧进程上。
for /l %%i in (1,1,40) do (
  "%PY%" platform_compat.py ready %PORT% >nul 2>nul
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
