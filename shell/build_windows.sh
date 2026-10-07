#!/bin/bash
#
# 归藏 · Windows 分发包打包脚本
#
#   ./shell/build_windows.sh            → 安装包/归藏-Windows-<版本>.zip
#   ./shell/build_windows.sh <目录>      → 打到指定目录
#
# 为什么给的是 zip 而不是 .exe：打包机是 macOS。PyInstaller / NSIS / Inno Setup
# 都得在 Windows 上编译，这台机器既没有 wine 也没有 makensis，做不出真 .exe。
# 所以给一份「解压即可用」的包，里面带两个脚本：
#   · 安装归藏.bat   复制到 %USERPROFILE%\归藏 + 桌面放「归藏」快捷方式
#   · 启动归藏.bat   不装也行，就地跑
#
# 清单（APP_FILES / APP_DIRS）和隐私体检都不在这里写第二份 —— 全在
# tools/package_windows.py，它读的又是 shell/build_macos.sh 那一处。
#
set -euo pipefail

cd "$(dirname "$0")/.."
ROOT="$(pwd)"
DEST="${1:-安装包}"

PY="$ROOT/.venv/bin/python"
[ -x "$PY" ] || PY="python3"

echo
echo "  归藏 · 打 Windows 分发包"
echo "  项目：$ROOT"
echo "  去向：$DEST"
echo

"$PY" "$ROOT/tools/package_windows.py" "$DEST"

echo
echo "  完成。解压后双击「安装归藏.bat」装；或双击「启动归藏.bat」就地跑。"
echo "  本机是 macOS，装与跑都没在这里验过 —— 包里「首次打开必读.txt」已如实说明。"
echo
