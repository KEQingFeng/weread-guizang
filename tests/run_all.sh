#!/usr/bin/env bash
# 归藏一键门禁：改完代码跑这一个脚本，绿了再谈提交。
#
#   bash tests/run_all.sh              # 全套（静态检查 + 真机浏览器套件）
#   bash tests/run_all.sh --fast       # 只跑不依赖浏览器的那几项，秒级
#
# 沙盒：所有套件都写进一个临时目录（书库 / cache / 截图），绝不碰用户真实的
# cache/、output/ 和 ~/Documents/归藏。服务是一次性的，脚本退出就关掉。
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(dirname "$HERE")"
cd "$ROOT" || exit 1

FAST=0
[ "${1:-}" = "--fast" ] && FAST=1

PY="$ROOT/.venv/bin/python"
[ -x "$PY" ] || PY="$ROOT/.venv/Scripts/python.exe"
[ -x "$PY" ] || PY="$(command -v python3 || command -v python)"

SANDBOX="${GUIZANG_SELFTEST_DIR:-${TMPDIR:-/tmp}/guizang-selftest}"
export GUIZANG_SELFTEST_DIR="$SANDBOX"
export GUIZANG_DATA="$SANDBOX/cache"
export GUIZANG_BOOKS="$SANDBOX/books"
export GUIZANG_SHOT_DIR="$SANDBOX/shots"
mkdir -p "$GUIZANG_DATA" "$GUIZANG_BOOKS" "$GUIZANG_SHOT_DIR"

FAILED=()
PASSED=0
run() {  # run <名字> <命令…>
  local name="$1"; shift
  echo
  echo "── $name ──────────────────────────────────────────"
  if "$@"; then
    PASSED=$((PASSED + 1))
  else
    echo "✗ $name 未通过"
    FAILED+=("$name")
  fi
}

# ── 静态：不需要浏览器，也不需要起服务 ──────────────────────────
run "Python 语法" "$PY" -m compileall -q -x '(/\.venv/|/dist/|/cache/|/output/|/node_modules/|/安装包/)' .
run "内联 JS 语法" "$PY" tests/check_inline_js.py
run "控件体检" "$PY" tests/audit_ui.py
run "个人信息扫描" "$PY" tests/check_privacy.py
run "跨平台口径" "$PY" tests/test_platform_compat.py
# 三条新线（订阅 / 视频 / 平台解析）都是离线自测：夹具跑在本机临时 http.server 上，
# 数据目录全指进各自沙盒，不联网、不碰用户真实书库。
run "RSS 订阅（发现 / 抓取 / 去重 / 入库）" "$PY" tests/check_feed.py
run "平台解析（知乎 / 小红书 / X）" "$PY" tests/check_web_parse.py
run "ffmpeg 按需下载（离线）" "$PY" tests/check_ffmpeg_tool.py
run "视频转笔记（下载 / 转写 / 总结 / 导图）" "$PY" tests/check_video_note.py

if [ "$FAST" = "1" ]; then
  echo; echo "静态门禁：通过 $PASSED 项，失败 ${#FAILED[@]} 项"
  for f in "${FAILED[@]:-}"; do [ -n "$f" ] && echo "  ✗ $f"; done
  [ ${#FAILED[@]} -eq 0 ] || exit 1
  exit 0
fi

# ── 一次性沙盒服务 ──────────────────────────────────────────────
"$PY" tests/seed.py || { echo "书架铺不上，真机套件没法跑"; exit 1; }
PORT=$("$PY" -c 'import socket;s=socket.socket();s.bind(("127.0.0.1",0));print(s.getsockname()[1]);s.close()')
LOG="$SANDBOX/server.log"
"$PY" ui_server.py --port "$PORT" >"$LOG" 2>&1 &
SRV=$!
trap 'kill $SRV 2>/dev/null' EXIT

# 服务自己会往后找空闲端口，所以地址要从它自己打印的那行里取，别赌。
ACTUAL=""
for _ in $(seq 1 120); do
  ACTUAL=$(sed -n 's|.*http://127\.0\.0\.1:\([0-9]*\).*|\1|p' "$LOG" | tail -1)
  if [ -n "$ACTUAL" ]; then
    GUIZANG_PROBE_PORT="$ACTUAL" "$PY" - <<'PY' && break
import json, os, sys, urllib.request
try:
    json.loads(urllib.request.urlopen(
        "http://127.0.0.1:%s/api/state" % os.environ["GUIZANG_PROBE_PORT"], timeout=1).read())
except Exception:
    sys.exit(1)
PY
  fi
  kill -0 $SRV 2>/dev/null || { echo "服务进程已退出，日志："; cat "$LOG"; exit 1; }
  sleep 0.25
done

if [ -z "${ACTUAL:-}" ]; then
  echo "沙盒服务没起来，日志："; cat "$LOG"; exit 1
fi
BASE="http://127.0.0.1:$ACTUAL"
export GUIZANG_TEST_URL="$BASE"
echo "沙盒服务：$BASE   书库：$GUIZANG_BOOKS"

# ── 真机：Playwright + Chromium ─────────────────────────────────
# 每套件跑之前都重铺一次书架：套件之间不许互相看数据。
# 上一轮就栽在这儿 —— 取证书留了一本空书在书架上，下一套件的「第一张卡」点到它，
# 报出「没有可打包的内容」，看着像后端的错，其实是测试互相踩了。
fresh_shelf() { "$PY" tests/seed.py --force >/dev/null || exit 1; }

fresh_shelf; run "布局校验" "$PY" tests/ui_check.py "$BASE/"
fresh_shelf; run "重新取书取证" "$PY" tests/check_refetch.py "$BASE"
fresh_shelf; run "视口回归" "$PY" tests/check_viewports.py "$BASE"
fresh_shelf; run "书架交互" "$PY" tests/check_shelf.py "$BASE"
fresh_shelf; run "笔记编辑器" "$PY" tests/check_notes_editor.py "$BASE"
fresh_shelf; run "阅读器续读与大纲" "$PY" tests/check_reader_flow.py "$BASE"
fresh_shelf; run "订阅与视频两屏" "$PY" tests/check_media_views.py "$BASE"
run "首启页" "$PY" tests/check_onboarding.py
fresh_shelf; run "异常兜底" "$PY" tests/test_server_fallback.py

echo
echo "剪藏真机走查默认跳过：公众号链接会过期，不入库。要跑得先备好链接表："
echo "  GUIZANG_CLIP_LINKS=/path/to/links.txt \"$PY\" tests/check_clip_live.py \"$BASE\""

kill $SRV 2>/dev/null
echo
echo "════════════════════════════════════════════════════════"
echo "通过 $PASSED 项，失败 ${#FAILED[@]} 项"
for f in "${FAILED[@]:-}"; do [ -n "$f" ] && echo "  ✗ $f"; done
echo "截图在 $GUIZANG_SHOT_DIR"
[ ${#FAILED[@]} -eq 0 ] || exit 1
