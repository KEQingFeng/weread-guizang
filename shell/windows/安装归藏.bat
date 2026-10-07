@echo off
chcp 65001 >nul
setlocal enabledelayedexpansion
title 归藏 · 安装

cd /d "%~dp0"

echo.
echo   归藏 · 安装到这台电脑
echo.

REM ---------- 1. 得在解压出来的文件夹里跑 ----------
REM 压缩包窗口里双击时 %CD% 是个临时目录，缺一堆文件；先拦下来，别复制出个半成品。
if not exist "ui_server.py" (
  echo   这个脚本要和 ui_server.py 放在一起，现在这个目录里没有。
  echo   请先把压缩包「全部解压」到一个文件夹，再从那个文件夹里双击我。
  echo.
  pause
  exit /b 1
)

REM ---------- 2. 已经在目标目录里就直说 ----------
set "DEST=%USERPROFILE%\归藏"
if /i "%CD%"=="%DEST%" (
  echo   你现在就在安装目录里：%DEST%
  echo   不用再装一次，直接双击「启动归藏.bat」就能用。
  echo.
  pause
  exit /b 0
)

echo   安装位置    %DEST%
echo   桌面快捷方式  归藏
echo.
echo   会把程序复制过去。
REM 这三样不复制：.venv 里的路径是写死的，搬过去就废；cache/output 是数据和登录态，
REM 属于「就地跑」那份，不该跟着搬家；.git 是开发仓库的东西，跟用户无关。
echo   （跳过 .venv、cache、output、.git —— 环境会在新位置重建，数据留在原地）
if exist ".git" (
  echo   提醒：这份看着是开发仓库（有 .git）。复制时会跳过它。
)
echo.
set /p OK=  直接回车开始安装，输入 n 取消：
if /i "%OK%"=="n" (
  echo   已取消。
  exit /b 0
)

REM ---------- 3. 复制 ----------
echo.
echo   正在复制……
robocopy "%CD%" "%DEST%" /E /XD .venv cache output __pycache__ .git dist /XF runtime.json *.log >nul
REM robocopy 的 0-7 都算成功（1=有文件复制过去了，2/4/6=有额外的目录/文件），8 以上才是真失败。
if errorlevel 8 (
  echo.
  echo   复制失败（robocopy 返回 !errorlevel!）。多半是目标目录被占用或没写权限。
  echo   关掉可能开着的「归藏」窗口再试一次。
  echo.
  pause
  exit /b 1
)
echo   复制完成。

REM ---------- 4. 桌面快捷方式 ----------
REM 图标用包里的 归藏.ico（Windows 上认 ico，不认 macOS 那份 icns）。
powershell -NoProfile -ExecutionPolicy Bypass -Command "$W=New-Object -ComObject WScript.Shell; $L=$W.CreateShortcut([Environment]::GetFolderPath('Desktop')+'\归藏.lnk'); $L.TargetPath='%DEST%\启动归藏.bat'; $L.WorkingDirectory='%DEST%'; $L.IconLocation='%DEST%\归藏.ico'; $L.Description='归藏 · 微信读书导出'; $L.Save()" >nul 2>nul
if errorlevel 1 (
  echo   ! 桌面快捷方式没建成。不影响使用：进 %DEST% 双击「启动归藏.bat」一样的。
) else (
  echo   桌面快捷方式已建好：「归藏」
)

echo.
echo   装好了。第一次打开会建环境、装依赖、下 Chromium（约 370MB），
echo   那一步慢是正常的；窗口别关，它是后端。
echo.
set /p GO=  现在就开始吗？（回车开始 / n 稍后自己点桌面图标）
if /i "%GO%"=="n" (
  echo   好，随时点桌面的「归藏」。
  exit /b 0
)

cd /d "%DEST%"
start "" "%DEST%\启动归藏.bat"
exit /b 0
