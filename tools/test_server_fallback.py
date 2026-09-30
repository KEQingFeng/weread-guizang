#!/usr/bin/env python3
"""回归护栏：操作抛异常时，必须回一句人话，而不是把连接掐断。

这条链是 Windows 上「点按钮毫无反应」的根因：`_action` 没有 try，异常穿出
do_POST → 连接被关 → 前端 `await` 直接 reject → onclick 没 catch → 页面不吭声。
这里起一个真服务、发一个必定抛异常的请求，然后确认三件事：
  ① HTTP 200 且回了 JSON（不是断连）
  ② 紧接着还能正常请求（服务没被拖死）
  ③ 日志里留下了可追的「操作失败」行

跑法：.venv/bin/python tools/test_server_fallback.py
"""
import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PY = str(ROOT / ".venv/bin/python") if os.path.isfile(ROOT / ".venv/bin/python") \
    else (str(ROOT / ".venv/Scripts/python.exe") if os.path.isfile(ROOT / ".venv/Scripts/python.exe") else sys.executable)


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def post(port, payload, timeout=10):
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/api/action",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, json.loads(r.read().decode())


def wait_up(port, tries=60):
    for _ in range(tries):
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/state", timeout=1) as r:
                if r.status == 200:
                    return True
        except Exception:
            time.sleep(0.25)
    return False


def main():
    port = free_port()
    log_path = "/tmp/gz_fallback_test.log"
    with open(log_path, "w") as logf:
        proc = subprocess.Popen([PY, "ui_server.py", "--port", str(port)],
                                cwd=ROOT, stdout=logf, stderr=subprocess.STDOUT, text=True)
    checks = []
    try:
        if not wait_up(port):
            print("服务没起来，测试无法进行")
            return 2

        # ① 必定抛异常：name 传数字，(5 or "").strip() → AttributeError
        try:
            status, body = post(port, {"action": "folder.new", "name": 5})
            checks.append(("HTTP 200", status == 200, status))
            checks.append(("回了 JSON 且 ok=false", body.get("ok") is False, body))
            checks.append(("消息里点出异常类型", "AttributeError" in (body.get("msg") or ""), body.get("msg")))
        except urllib.error.HTTPError as e:
            checks.append(("HTTP 200", False, f"HTTPError {e.code}"))
        except Exception as e:                       # RemoteDisconnected 就落这里
            checks.append(("连接没被掐断", False, f"{type(e).__name__}: {e}"))

        # ② 服务还活着，正常操作照旧
        status, body = post(port, {"action": "no.such.action"})
        checks.append(("异常之后仍能服务", status == 200 and body.get("msg") == "未知操作", body))

        # ③ 用户能看到的「进展」栏里留下了可追的行（log() 只进内存环形缓冲）
        time.sleep(0.4)
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/log?tail=60", timeout=5) as r:
            lines = [x.get("s", "") for x in json.loads(r.read().decode()).get("lines", [])]
        hit = [l for l in lines if "操作失败" in l]
        checks.append(("进展栏留下「操作失败」", bool(hit) and any("AttributeError" in l for l in lines), hit[:1]))
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()

    bad = [c for c in checks if not c[1]]
    for name, ok, detail in checks:
        print(f"  {'PASS' if ok else 'FAIL'}  {name}" + ("" if ok else f"   ← {detail}"))
    print(f"\n{len(checks) - len(bad)}/{len(checks)} 通过")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
