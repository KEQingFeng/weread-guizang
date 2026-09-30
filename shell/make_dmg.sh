#!/usr/bin/env bash
# 把 dist/归藏.app 做成一个能发给别人的 .dmg。
#
# 为什么是 dmg 而不是 zip：macOS 上「拖进应用程序文件夹」是所有人认得的
# 安装动作，zip 解压出来的应用容易被随手丢在下载目录里跑。
#
# 用 ditto + hdiutil 这两条系统自带的，不引第三方打包工具。
#
# 用法：bash shell/make_dmg.sh [输出目录]
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
APP="$ROOT/dist/归藏.app"
README="$HERE/首次打开必读.txt"
OUT_DIR="${1:-$HOME/Desktop}"
VERSION="$(/usr/libexec/PlistBuddy -c 'Print :CFBundleShortVersionString' "$APP/Contents/Info.plist" 2>/dev/null || echo 1.0.0)"
DMG="$OUT_DIR/归藏-$VERSION.dmg"

[ -d "$APP" ] || { echo "先跑 shell/build_macos.sh 出成品，再来打 dmg"; exit 1; }

STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT
# 名字里必须带 .app —— ditto 是「把源目录里的东西倒进目标目录」，
# 目标名少了后缀就等于把 bundle 拆成一只普通文件夹，装出去也不再是应用。
STAGE_APP="$STAGE/归藏.app"

echo "归藏 · 打 macOS 安装包"
echo "  来源  $APP"
echo "  版本  $VERSION"

# 用 ditto 而不是 cp -R：ditto 会连签名、扩展属性和资源分叉一起搬，
# cp -R 在某些情况下会把 bundle 的签名搬坏，搬坏的应用在别人机器上
# 会被 Gatekeeper 直接判死。
ditto "$APP" "$STAGE_APP"
cp "$README" "$STAGE/首次打开必读.txt"
ln -s /Applications "$STAGE/应用程序"

# 先把上一次可能挂着的同名卷卸掉，免得 hdiutil 报「资源忙」
if [ -d "/Volumes/归藏 $VERSION" ]; then
  hdiutil detach "/Volumes/归藏 $VERSION" -force >/dev/null 2>&1 || true
fi

rm -f "$DMG"
hdiutil create -quiet -volname "归藏 $VERSION" -srcfolder "$STAGE" \
  -fs HFS+ -format UDZO -ov "$DMG"

# 挂上去看一眼，确认里面的东西是对的 —— 打完就交出去、从不打开，
# 是「交付了个打不开的包」这类事故最常见的来路。
echo
echo "  验收"
MNT="$(hdiutil attach "$DMG" -nobrowse -readonly | awk -F'\t' '/\/Volumes\//{print $NF}' | tail -1)"
trap 'hdiutil detach "$MNT" -force >/dev/null 2>&1 || true; rm -rf "$STAGE"' EXIT

ls -1 "$MNT" | sed 's/^/    /'
[ -d "$MNT/归藏.app" ] || { echo "  ! 包里没有 归藏.app"; exit 1; }
[ -x "$MNT/归藏.app/Contents/MacOS/归藏" ] || { echo "  ! 可执行文件丢了执行位"; exit 1; }
[ -f "$MNT/归藏.app/Contents/Resources/app/ui_server.py" ] || { echo "  ! 源码没进去"; exit 1; }
codesign --verify --deep --strict "$MNT/归藏.app" 2>/dev/null \
  && echo "    签名 通过" || echo "    ! 签名校验没过"

SIZE="$(du -h "$DMG" | cut -f1)"
echo
echo "  完成 → $DMG  ($SIZE)"
echo
echo "  发给别人时提醒一句：第一次打开要按右键 →「打开」，"
echo "  包里那份《首次打开必读.txt》里写着原因和替代做法。"
