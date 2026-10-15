#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""归藏门禁：任何一条 POST 路由没料到的异常，也要回一句人话，不许把连接掐断。

为什么单开这一条
    `tests/test_server_fallback.py` 立过这条规矩，但它盯的是 `/api/action` 一条路 ——
    那一族的异常由 `_action` 就地接住，接得住是因为那层 try 就长在它身上。其余 POST
    路由（`/api/book_state` / `/api/config` / `/api/import` …）各自只有局部的 try：
    落盘那一步（账本位置被占成目录、盘满、目录只读）没被局部 try 圈住时，异常会一路
    穿出 `do_POST`。`BaseHTTPRequestHandler` 不替谁回 500，它只是把连接关掉 —— 前端
    `await post(...)` 收到 RemoteDisconnected、onclick 里没 catch，用户看到的就是
    「点按钮毫无反应」，与当年 Windows 上那个根因同一个症状。

    兜底提到 `handle_one_request` 这一层之后，用真服务把两条不同的路由各喂一次
    「落盘必定失败」，逐条钉住：
      ① 回 200 + JSON（不是断连）
      ② `ok=false`，且 msg 里点出异常类型（与日志里记的那个类型一致）
      ③ 紧接着 `/api/state` 照常（服务没被拖死）
      ④ `/api/log` 里留下「操作失败」那行，并点出是哪条路由
      ⑤ 对照组：把挡路的那一样撤掉，同一个请求立刻正常成功 —— 不是一刀切拒绝

    全程在系统临时目录里起一次性服务（`library.json` / `config.json` 顶成目录来制造
    落盘失败），不联网、不开浏览器、不碰真实 cache/ 与书库，跑完删干净。
"""
import atexit
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import selftest  # noqa: E402

ROOT = selftest.REPO

PASSED = 0
FAIL = []


def chk(name, cond, extra=""):
    global PASSED
    print(("  ok  " if cond else " FAIL ") + name + ("" if cond else "   <- " + str(extra)[:220]))
    if cond:
        PASSED += 1
    else:
        FAIL.append(name)


# ── 1. 源码接线：总兜底装在一个盖得住所有路由的地方 ──────────────────
SRC = (ROOT / "ui_server.py").read_text(encoding="utf-8")
chk("总兜底装在 Handler 上：有 handle_one_request",
    "    def handle_one_request(self):" in SRC)
seg = ""
at = SRC.find("    def handle_one_request(self):")
if at > 0:
    end = SRC.find("\n    def _send(self", at)
    seg = SRC[at:end if end > at else at + 2600]
chk("兜底包住 BaseHTTPRequestHandler 那一发（super().handle_one_request()）",
    "super().handle_one_request()" in seg)
chk("兜底留一行可追的「操作失败」，并点出路由",
    "操作失败" in seg and 'getattr(self, "path"' in seg)
chk("兜底回的是 JSON ok=false 的人话", '"ok": False' in seg and "没做成" in seg)
chk("只补「还没回过话」的那些：认 _sent_response 这个标记",
    "_sent_response" in seg)
chk("两条回包口都标上「已经回过话」（_send 与 _sse_start）",
    SRC.count("_sent_response = True") == 2, SRC.count("_sent_response = True"))
# 总兜底必须盖在 do_POST / do_GET 之外（它俩是 BaseHTTPRequestHandler 调下来的），
# 不然「加一层 do_POST try」只盖得住写、盖不住读，而且会撞坏 check_csrf 的源码切片。
chk("do_POST / do_GET 没被改名或重塑",
    "    def do_POST(self):" in SRC and "    def do_GET(self):" in SRC)

# ── 2. 真机：起一次性沙盒服务，制造两处「落盘必定失败」 ──────────────
SB = pathlib.Path(tempfile.mkdtemp(prefix="gz-postguard-"))
atexit.register(lambda: shutil.rmtree(SB, ignore_errors=True))
DATA, BOOKS = SB / "data", SB / "books"
CACHE = DATA / "cache"
for _d in (DATA, BOOKS, CACHE, SB / "shots"):
    _d.mkdir(parents=True, exist_ok=True)
env = dict(os.environ, GUIZANG_SELFTEST_DIR=str(SB), GUIZANG_DATA=str(DATA),
           GUIZANG_BOOKS=str(BOOKS), GUIZANG_SHOT_DIR=str(SB / "shots"))


def free_port():
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


port = free_port()
logp = SB / "server.log"
srv = subprocess.Popen([sys.executable, "ui_server.py", "--port", str(port)],
                       cwd=str(ROOT), env=env, stdout=open(logp, "w"),
                       stderr=subprocess.STDOUT)
atexit.register(lambda: srv.terminate())
base = ""
for _ in range(160):
    m = re.search(r"http://127\.0\.0\.1:(\d+)", logp.read_text(errors="replace"))
    if m:
        try:
            urllib.request.urlopen("http://127.0.0.1:%s/api/state" % m.group(1), timeout=1)
            base = "http://127.0.0.1:%s" % m.group(1)
            break
        except Exception:
            pass
    time.sleep(0.25)

if not base:
    chk("真机：沙盒服务起来了", False, logp.read_text(errors="replace")[-400:])
else:
    chk("真机：沙盒服务起来了", True)

    def call(method, path, payload=None):
        """回 (状态码, 解出来的 JSON 或 {})；断连 / 其它异常回 (None, {"_exc": …})。"""
        data = None if payload is None else json.dumps(payload).encode()
        req = urllib.request.Request(base + path, data=data, method=method,
                                     headers={"Content-Type": "application/json"}
                                     if data is not None else {})
        try:
            with urllib.request.urlopen(req, timeout=8) as r:
                return r.status, json.loads(r.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as e:
            try:
                return e.code, json.loads(e.read().decode("utf-8", "replace"))
            except Exception:
                return e.code, {}
        except Exception as e:                       # RemoteDisconnected 就落这里
            return None, {"_exc": "%s: %s" % (type(e).__name__, str(e)[:120])}

    def typ_of(text):
        """从一句话里把异常类型名摘出来（`… IsADirectoryError: …` → IsADirectoryError）。"""
        m = re.search(r"([A-Za-z_][A-Za-z0-9_]*(?:Error|Exception)):", text or "")
        return m.group(1) if m else ""

    # 2a. /api/book_state：save_lib 那一步落盘失败（把 library.json 顶成一个目录）
    (CACHE / "library.json").mkdir()
    st, body = call("POST", "/api/book_state",
                    {"books": ["wb123456"], "state": "reading"})
    chk("B1 不再是断连：回 200", st == 200, (st, body))
    chk("B2 ok=false（没做成就直说）", body.get("ok") is False, body)
    chk("B3 msg 里点出异常类型", bool(typ_of(body.get("msg"))), body.get("msg"))
    st2, state = call("GET", "/api/state")
    chk("B4 异常之后仍能服务", st2 == 200 and isinstance(state, dict) and state.get("ok") is not False,
        (st2, str(state)[:120]))

    # 2b. 对照组：撤掉挡路的那一样，同一个请求立刻成功（不是一刀切拒绝）
    shutil.rmtree(CACHE / "library.json")
    st3, body3 = call("POST", "/api/book_state",
                      {"books": ["wb123456"], "state": "reading"})
    chk("B5 对照组：落盘恢复后同一请求成功", st3 == 200 and body3.get("ok") is True,
        (st3, body3))

    # 2c. 第二条路由 /api/config：save_cfg 落盘失败（把 config.json 顶成一个目录）
    (CACHE / "config.json").mkdir()
    st4, body4 = call("POST", "/api/config", {})
    chk("C1 /api/config 落盘失败也回 200 + 人话",
        st4 == 200 and body4.get("ok") is False and bool(typ_of(body4.get("msg"))),
        (st4, body4))

    # 2d. 日志：留下可追的「操作失败」，点名是哪条路由，类型与回包那句一致
    st5, lg = call("GET", "/api/log?tail=80")
    lines = [x.get("s", "") for x in (lg.get("lines") or [])]
    hit = [l for l in lines if "操作失败" in l]
    chk("D1 进展栏留下「操作失败」", bool(hit), lines[-3:])
    chk("D2 日志点名是哪条路由", any("/api/book_state" in l for l in hit)
        and any("/api/config" in l for l in hit), hit[-2:])
    chk("D3 日志里点出异常类型", all(typ_of(l) for l in hit[:2]), hit[:2])
    chk("D4 回包那句的类型与日志记的一致",
        bool(hit) and typ_of(body4.get("msg")) and typ_of(body4.get("msg")) in hit[-1],
        (typ_of(body4.get("msg")), hit[-1:] if hit else []))

    srv.terminate()

print()
print("POST 路由总兜底门禁：%d 项通过，失败 %d 项" % (PASSED, len(FAIL)))
for f in FAIL:
    print("  ✗ " + f)
sys.exit(1 if FAIL else 0)
