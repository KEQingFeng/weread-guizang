#!/bin/bash
# 归藏 · 一键启动（macOS / Linux）
# 双击运行，或在终端执行 ./启动归藏.command
# 脚本自己认所在目录，不含任何写死的本机路径。
set -u

DIR="$(cd "$(dirname "$0")" && pwd)"
# 端口可以按 GUIZANG_PORT 改（8770 被别的应用占着时用得上）；MCP 适配器读的是同一个变量。
PORT="${GUIZANG_PORT:-8770}"
cd "$DIR" || exit 1

echo
echo "  归藏 · 微信读书导出"
echo "  目录：$DIR"
echo

# 选 Python：优先项目自带虚拟环境，其次系统 python3
PY="$DIR/.venv/bin/python"
[ -x "$PY" ] || PY="$(command -v python3 || command -v python || true)"
if [ -z "$PY" ]; then
  echo "  没找到 python3。请先安装 Python 3.10 或更新版本，然后重新运行。"
  read -r -p "  回车关闭…"; exit 1
fi

# 第一次运行：建虚拟环境并装依赖（Chromium 约 368MB，慢的话先给终端挂代理）
if [ ! -x "$DIR/.venv/bin/python" ]; then
  echo "  第一次运行：创建虚拟环境并安装依赖。"
  echo "  其中 Chromium 约 368MB，下载慢的话先给终端挂上代理。"
  echo
  "$PY" -m venv .venv || { echo "  创建虚拟环境失败。"; read -r -p "  回车关闭…"; exit 1; }
  PY="$DIR/.venv/bin/python"
  "$PY" -m pip install --upgrade pip
  "$PY" -m pip install -r requirements.txt || { echo "  安装依赖失败。"; read -r -p "  回车关闭…"; exit 1; }
  "$PY" -m playwright install chromium || { echo "  安装 Chromium 失败。"; read -r -p "  回车关闭…"; exit 1; }
  echo
fi

# 端口上有人：先问它是谁，再决定「直接开界面」还是「换个新进程接手」。
# 原来这里只看端口有没有人听 —— 一个几周前留下的旧后端就会把新代码挡住：
# 界面是从磁盘现读的新的，路由表却是旧进程装进内存的旧的，新功能一律 404，
# 页面却报「后端没启动」，把人带去查一件本来没事的事（2026-10-04 报的三条 bug）。
RES="$("$PY" platform_compat.py verdict "$PORT" 2>/dev/null)"
ACT="${RES%%|*}"
NOTE="${RES#*|}"

# 问不出话（解释器坏了、脚本被挪走）就退回老行为：别把本来能用的界面挡住。
if [ -z "$ACT" ]; then
  echo "  问不出端口上是哪份代码，按原样开界面 → http://127.0.0.1:$PORT"
  open "http://127.0.0.1:$PORT" 2>/dev/null || xdg-open "http://127.0.0.1:$PORT" 2>/dev/null
  exit 0
fi

if [ "$ACT" = "reuse" ]; then
  echo "  ${NOTE:-归藏已在运行} → http://127.0.0.1:$PORT"
  open "http://127.0.0.1:$PORT" 2>/dev/null || xdg-open "http://127.0.0.1:$PORT" 2>/dev/null
  exit 0
fi

if [ "$ACT" = "busy" ]; then
  echo "  $NOTE"
  echo "  界面还是在这儿开 → http://127.0.0.1:$PORT；跑完任务后页顶会出现「换新后端」，点一下就好。"
  open "http://127.0.0.1:$PORT" 2>/dev/null || xdg-open "http://127.0.0.1:$PORT" 2>/dev/null
  exit 0
fi

if [ "$ACT" = "stranger" ]; then
  echo "  $NOTE"
  echo "  要么把占用 $PORT 的程序退掉，要么换个端口：GUIZANG_PORT=8899 ./启动归藏.command"
  read -r -p "  回车关闭…"
  exit 1
fi

# free：直接起；takeover：带着 --takeover 起，让新进程把旧后端那份端口接过来。
if [ "$ACT" = "takeover" ]; then
  echo "  $NOTE"
  echo "  归藏换新后端中 → http://127.0.0.1:$PORT"
  FLAG="--takeover"
else
  echo "  归藏启动中 → http://127.0.0.1:$PORT"
  FLAG=""
fi
nohup "$PY" ui_server.py --port "$PORT" $FLAG > /tmp/guizang_server.log 2>&1 &

# 轮询就绪，最多等 20 秒。认的是代码指纹而不是「端口通了」：
# 交接那几秒旧后端还可能答话，只看通不通会把界面开在旧进程上。
for _ in $(seq 1 40); do
  if "$PY" platform_compat.py ready "$PORT" >/dev/null 2>&1; then
    open "http://127.0.0.1:$PORT" 2>/dev/null || xdg-open "http://127.0.0.1:$PORT" 2>/dev/null
    exit 0
  fi
  sleep 0.5
done

echo "  启动超时：看一眼 /tmp/guizang_server.log 最后几行。"
read -r -p "  回车关闭…"
