#!/bin/bash
#
# 归藏 · macOS 打包脚本
#
#   ./shell/build_macos.sh
#
# 产出 dist/归藏.app —— 双击即用的完整程序：原生外壳 + 全部 Python 源码 + 界面 + 图标。
# 只依赖 CommandLineTools 自带的 swiftc / iconutil，不需要完整 Xcode，也不需要 Flutter。
#
# 打出来的包不含虚拟环境、不含 Chromium —— 那两样在用户机上由首启页的「我思故我在」
# 现装（装到 ~/Library/Application Support/归藏/，不污染应用包）。
# 所以包体很小，几百 KB 量级。
#
set -euo pipefail

cd "$(dirname "$0")/.."
ROOT="$(pwd)"

APP_NAME="归藏"
BUNDLE_ID="com.keqingfeng.guizang"
# 版本号从 ui_server.py 里读（那里是界面上显示的那个数），不再两处各写一份 ——
# 之前写过界面 1.1.0、访达 1.0.0 的对不上。读不到就在下一步报错停下。
VERSION="$(sed -n 's/^VERSION *= *"\([^"]*\)".*/\1/p' "$ROOT/ui_server.py" | head -1)"
MIN_MACOS="13.0"

DIST="$ROOT/dist"
APP="$DIST/$APP_NAME.app"
CONTENTS="$APP/Contents"
BIN_DIR="$CONTENTS/MacOS"
RES_DIR="$CONTENTS/Resources"
SRC_DIR="$RES_DIR/app"

# 进包体的源码清单。显式列出，免得把 cache/ output/ __pycache__ 一起拖进去。
APP_FILES=(
  bootstrap.py
  platform_compat.py
  ui_server.py
  ui.html
  login.py
  export_precise.py
  download_images.py
  shelf_add.py
  book_export.py
  book_import.py
  book_notes.py
  clip_article.py
  web_parse.py
  feed.py
  video_note.py
  ffmpeg_tool.py
  sync.py
  requirements.txt
)
APP_DIRS=(
  mcp
  skills
  vendor
)

say() { printf '  %s\n' "$*"; }
die() { printf '\n  ✗ %s\n\n' "$*" >&2; exit 1; }

echo
echo "  归藏 · 打包 macOS 应用"
echo "  项目：$ROOT"
echo

# ---------- 0. 前置检查 ----------

command -v swiftc >/dev/null 2>&1 || die "找不到 swiftc。装一下 CommandLineTools：xcode-select --install"
[ -f "$ROOT/shell/main.swift" ] || die "缺 shell/main.swift"
[ -f "$ROOT/shell/onboarding.html" ] || die "缺 shell/onboarding.html"
for f in "${APP_FILES[@]}"; do
  [ -f "$ROOT/$f" ] || die "缺 $f"
done
for d in "${APP_DIRS[@]}"; do
  [ -d "$ROOT/$d" ] || die "缺目录 $d/"
done

# 根目录的每个 .py 都是运行期模块，一律必须进包。这条不能靠自觉：漏一个的表现是
# 「源码直接跑一切正常、装成 app 一点就哑」，而那正是本仓库唯一没法在开发机复现的
# 路径。0.9.9 就漏过 feed.py / web_parse.py / video_note.py / ffmpeg_tool.py 四个
# —— 所以这里反过来查：根目录冒出新 .py 而没进清单，打包直接 die。
for p in "$ROOT"/*.py; do
  b="$(basename "$p")"
  listed=0
  for f in "${APP_FILES[@]}"; do
    [ "$f" = "$b" ] && listed=1 && break
  done
  [ "$listed" = 1 ] || die "根目录的 $b 没进 APP_FILES（运行期模块必须进包）"
done

SDK=""
TOOLCHAIN=""
if SDK="$(xcrun --show-sdk-path 2>/dev/null)" && [ -n "$SDK" ]; then
  TOOLCHAIN="默认（$(xcode-select -p 2>/dev/null)）"
elif [ -d /Library/Developer/CommandLineTools ] \
     && SDK="$(DEVELOPER_DIR=/Library/Developer/CommandLineTools xcrun --show-sdk-path 2>/dev/null)" \
     && [ -n "$SDK" ]; then
  # 装了完整 Xcode 但没同意许可协议时，xcrun/swiftc 会直接罢工（返回 69），
  # 连带 xcode-select 指到哪儿都没用。CommandLineTools 那一套不受影响，
  # 所以把 DEVELOPER_DIR 掰回 CLT —— 不用 sudo，也不改全局设置。
  export DEVELOPER_DIR=/Library/Developer/CommandLineTools
  TOOLCHAIN="CommandLineTools（默认那套没同意许可协议，已自动绕开）"
fi
[ -n "$SDK" ] || die "没有可用的 Swift 工具链。装 CommandLineTools：xcode-select --install；若装了完整 Xcode，先跑 sudo xcodebuild -license。"

say "工具链  $TOOLCHAIN"
say "swiftc  $(swiftc --version 2>/dev/null | head -1 | cut -d'(' -f1 | xargs)"
say "SDK     $SDK"

# ---------- 1. 编译外壳 ----------

echo
echo "  [1/5] 编译外壳"

mkdir -p "$DIST"
rm -rf "$APP"
mkdir -p "$BIN_DIR" "$RES_DIR" "$SRC_DIR"

compile() {         # compile <arch> <输出路径>
  swiftc \
    -O -swift-version 5 \
    -target "$1-apple-macos$MIN_MACOS" \
    -sdk "$SDK" \
    -framework AppKit -framework WebKit -framework UniformTypeIdentifiers \
    -o "$2" \
    "$ROOT/shell/main.swift"
}

HOST_ARCH="$(uname -m)"
if [ "$HOST_ARCH" = "arm64" ]; then
  # 顺手编出 Intel 那一份，做通用二进制 —— 打包一次，两种 Mac 都能双击。
  # 交叉编译失败（比如 SDK 不全）就退回本机架构，不为此中断打包。
  if compile x86_64 "$DIST/.归藏-x86_64" 2>/dev/null; then
    compile arm64 "$DIST/.归藏-arm64"
    lipo -create "$DIST/.归藏-arm64" "$DIST/.归藏-x86_64" -output "$BIN_DIR/$APP_NAME"
    rm -f "$DIST/.归藏-arm64" "$DIST/.归藏-x86_64"
    say "通用二进制（arm64 + x86_64）"
  else
    rm -f "$DIST/.归藏-x86_64"
    compile arm64 "$BIN_DIR/$APP_NAME"
    say "单架构（arm64）—— 交叉编译不可用，不影响本机运行"
  fi
else
  compile x86_64 "$BIN_DIR/$APP_NAME"
  say "单架构（x86_64）"
fi
chmod +x "$BIN_DIR/$APP_NAME"
say "二进制  $(du -h "$BIN_DIR/$APP_NAME" | cut -f1)"

# ---------- 2. 装源码 ----------

echo
echo "  [2/5] 装入 Python 源码与界面"

for f in "${APP_FILES[@]}"; do
  cp "$ROOT/$f" "$SRC_DIR/$f"
done
for d in "${APP_DIRS[@]}"; do
  # -R 保留目录结构；--exclude 掉 Python 缓存与 macOS 垃圾文件
  rsync -a --exclude '__pycache__' --exclude '*.pyc' --exclude '.DS_Store' \
        "$ROOT/$d" "$SRC_DIR/"
done
cp "$ROOT/shell/onboarding.html" "$RES_DIR/onboarding.html"
say "源码  $(find "$SRC_DIR" -type f | wc -l | xargs) 个文件"

# ---------- 3. Info.plist ----------

echo
echo "  [3/5] 写 Info.plist"
[ -n "$VERSION" ] || die "读不到版本号：ui_server.py 里那行 VERSION 得是 VERSION = \"x.y.z\""
cp "$ROOT/shell/Info.plist" "$CONTENTS/Info.plist"
plutil -replace CFBundleShortVersionString -string "$VERSION" "$CONTENTS/Info.plist" \
  || die "写不进版本号（plutil）"
plutil -lint "$CONTENTS/Info.plist" >/dev/null || die "Info.plist 不合法"
say "bundle id  $BUNDLE_ID"
say "版本       $VERSION（最低 macOS $MIN_MACOS）"

# ---------- 4. 图标 ----------

echo
echo "  [4/5] 画图标"

# make_icon.py 走 PyObjC 的 Quartz；找一个装了这个的 python3。
ICON_PY=""
for c in \
  /Library/Frameworks/Python.framework/Versions/3.14/bin/python3 \
  /Library/Frameworks/Python.framework/Versions/3.13/bin/python3 \
  /Library/Frameworks/Python.framework/Versions/3.12/bin/python3 \
  /opt/homebrew/bin/python3 \
  /usr/local/bin/python3 \
  /usr/bin/python3
do
  if [ -x "$c" ] && "$c" -c 'import Quartz' >/dev/null 2>&1; then ICON_PY="$c"; break; fi
done

if [ -n "$ICON_PY" ]; then
  if "$ICON_PY" "$ROOT/tools/make_icon.py" "$RES_DIR/appicon.icns" >/dev/null; then
    say "appicon.icns  $(du -h "$RES_DIR/appicon.icns" | cut -f1)"
  else
    say "图标生成失败 —— 应用照常能用，只是显示成系统默认图标"
  fi
else
  say "没有带 PyObjC 的 python3，跳过图标 —— 应用照常能用"
fi
rm -rf "$RES_DIR/$APP_NAME.iconset"

# ---------- 5. 签名 ----------

echo
echo "  [5/5] 临时签名"

# 本地自用走 ad-hoc 签名就够（不需要开发者证书）。签过之后 macOS 不会把它
# 当成「来路不明的可执行文件」，转移、复制也不容易出「已损坏」。
if codesign --force --sign - "$APP" 2>/dev/null; then
  say "ad-hoc 签名完成"
else
  say "签名跳过 —— 不影响本机双击运行"
fi

# ---------- 验收 ----------

echo
echo "  验收"
say "包体  $(du -sh "$APP" | cut -f1)"
[ -x "$BIN_DIR/$APP_NAME" ] || die "可执行文件没生成"
say "架构  $(lipo -archs "$BIN_DIR/$APP_NAME" 2>/dev/null || echo 未知)"
say "源码  $SRC_DIR"
if codesign --verify "$APP" 2>/dev/null; then say "签名  通过"; fi

# 包体里不该混进用户数据 —— 顺手断言一下，防以后改坏
for junk in cache output .venv __pycache__; do
  if [ -e "$SRC_DIR/$junk" ]; then
    echo
    echo "  ! 包体里混进了 $junk，清掉"
    rm -rf "$SRC_DIR/$junk"
  fi
done

echo
echo "  完成 → $APP"
echo
echo "  双击就能用：第一次开是首启页，点「我思故我在」把缺的装上，"
echo "  扫码登录后自动进界面。之后每次打开直接进。"
echo
