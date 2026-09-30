#!/bin/bash
# 归藏 · 一键启动（macOS / Linux）
# 双击运行，或在终端执行 ./启动归藏.command
# 脚本自己认所在目录，不含任何写死的本机路径。
set -u

DIR="$(cd "$(dirname "$0")" && pwd)"
PORT=8770
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

# 端口已经起着：说明服务在跑，直接开界面，不再起第二个
if lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1; then
  echo "  归藏已在运行 → http://127.0.0.1:$PORT"
  open "http://127.0.0.1:$PORT" 2>/dev/null || xdg-open "http://127.0.0.1:$PORT" 2>/dev/null
  exit 0
fi

echo "  归藏启动中 → http://127.0.0.1:$PORT"
nohup "$PY" ui_server.py --port "$PORT" > /tmp/guizang_server.log 2>&1 &

# 轮询就绪，最多等 15 秒（用项目自己的解释器探 HTTP，不依赖 curl）
for _ in $(seq 1 30); do
  if "$PY" -c "import urllib.request as u; u.urlopen('http://127.0.0.1:$PORT/', timeout=1).read(1)" >/dev/null 2>&1; then
    open "http://127.0.0.1:$PORT" 2>/dev/null || xdg-open "http://127.0.0.1:$PORT" 2>/dev/null
    exit 0
  fi
  sleep 0.5
done

echo "  启动超时：看一眼 /tmp/guizang_server.log 最后几行。"
read -r -p "  回车关闭…"
