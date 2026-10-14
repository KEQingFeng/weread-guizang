#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""跨站那道门：别的网页不许替用户点归藏的按钮。

为什么要单独一份、还要真起一个服务
    「只听回环」不等于「只有归藏自己的页面打得进来」：请求是从用户这台机器发出去的，
    浏览器对 127.0.0.1 和对公网站点一样地送。所以这道门必须**在服务端**验，而且
    要把「浏览器会怎么发」照原样发一遍才算数 —— 前半段打纯函数（边界表），后半段起
    一个真 `ui_server.py`，用裸 HTTP 把 Content-Type / Origin / Sec-Fetch-Site
    三种头按需要拼出来（urllib 会自动补 Content-Type，反而模拟不了浏览器那种
    `new Blob([...])` 的请求）。

三件事在这里钉住
    1. 判定本身：什么算跨站、什么算自己人、什么算「不是浏览器」，逐条列出。
    2. 接线：`do_POST` 第一件事就是问 net_guard，问在解析 body 之前。
    3. 真的没做：跨站那几发必须既回 403、又没在账本上留下任何痕迹；同源与
       「不是浏览器」（curl / MCP / 门禁）那几发必须照常做成 —— 只拦不误伤。

自己起服务、自己临时目录，跑完整个包删掉；用户真实的 cache/ 与书库一个字节不碰。
"""
import atexit
import http.client
import json
import os
import pathlib
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import net_guard as ng  # noqa: E402

checks = []
HOST = "127.0.0.1:8080"


def chk(name, ok, extra=""):
    checks.append((bool(ok), name, "" if ok else str(extra)[:300]))
    print(("  ok  " if ok else " FAIL ") + name + ("" if ok else "  <- " + str(extra)[:300]))


# ── 1. 判定：边界表 ────────────────────────────────────────────────
# 每一行 (名字, 方法, 头, Host, 期望「拒」)
TABLE = [
    ("不是浏览器：什么标记都没有（curl / 门禁脚本）", "POST", {}, HOST, False),
    ("不是浏览器：Node 那份形状（只有 Sec-Fetch-Mode）", "POST",
     {"Sec-Fetch-Mode": "cors", "Content-Type": "application/json"}, HOST, False),
    ("同源：Origin 与 Host 一致", "POST", {"Origin": "http://" + HOST}, HOST, False),
    ("同源：localhost 打开就用 localhost 比", "POST",
     {"Origin": "http://localhost:8080"}, "localhost:8080", False),
    ("同源：scheme 大小写不算数", "POST", {"Origin": "HTTP://" + HOST}, HOST, False),
    ("同源：host 末尾的点归一掉", "POST",
     {"Origin": "http://127.0.0.1.:8080"}, HOST, False),
    ("同源：浏览器说 same-origin", "POST", {"Sec-Fetch-Site": "same-origin"}, HOST, False),
    ("同源：地址栏直接开（none）", "POST", {"Sec-Fetch-Site": "none"}, HOST, False),
    ("同源：same-site 也放行", "POST", {"Sec-Fetch-Site": "Same-Site"}, HOST, False),
    ("跨站：外站 Origin", "POST", {"Origin": "https://evil.example"}, HOST, True),
    ("跨站：同主机别的端口就是别的源", "POST",
     {"Origin": "http://127.0.0.1:9999"}, HOST, True),
    ("跨站：Origin 不带端口（等于 80）", "POST", {"Origin": "http://127.0.0.1"}, HOST, True),
    ("跨站：沙箱 iframe 的 Origin: null", "POST", {"Origin": "null"}, HOST, True),
    ("跨站：Origin 是空串", "POST", {"Origin": ""}, HOST, True),
    ("跨站：Origin 缺主机名（畸形）", "POST", {"Origin": "https://"}, HOST, True),
    ("跨站：端口不是数字（畸形）", "POST", {"Origin": "http://127.0.0.1:oops"}, HOST, True),
    ("跨站：Host 缺了就没法比，按拒处理", "POST", {"Origin": "http://127.0.0.1:8080"}, "", True),
    ("跨站：没有 Origin，但浏览器说 cross-site", "POST",
     {"Sec-Fetch-Site": "cross-site"}, HOST, True),
    ("跨站：application/json 也一样拒（不靠 Content-Type）", "POST",
     {"Origin": "https://evil.example", "Content-Type": "application/json"}, HOST, True),
    ("只审写操作：GET 带外站 Origin 不拦", "GET",
     {"Origin": "https://evil.example"}, HOST, False),
    ("只审写操作：HEAD 不拦", "HEAD", {"Origin": "https://evil.example"}, HOST, False),
    ("写操作不止 POST：PUT 也拦", "PUT", {"Origin": "https://evil.example"}, HOST, True),
    ("写操作不止 POST：DELETE 也拦", "DELETE",
     {"Origin": "https://evil.example"}, HOST, True),
    ("同源判定：scheme 不认识就不算同源", "POST",
     {"Origin": "ftp://127.0.0.1:8080"}, HOST, True),
]

for name, method, headers, host, want in TABLE:
    got = ng.cross_site(method, headers, host)
    chk("判定：" + name, got == want, {"got": got, "want": want})

# headers 是 http.server 那种大小写不敏感的容器时也要认得出（真机那一半走这条）。
chk("判定：头名大小写不敏感（直接喂 dict 也行）",
    ng.cross_site("POST", {"origin": "https://evil.example"}, HOST) is True
    and ng.cross_site("POST", {"ORIGIN": "http://" + HOST}, HOST) is False)


# ── 2. 接线：这道门真装在了写入口上 ────────────────────────────────
srv_src = (ROOT / "ui_server.py").read_text(encoding="utf-8")
chk("接线：ui_server 导入了 net_guard", "import net_guard" in srv_src)
post_at = srv_src.find("    def do_POST(self):")
chk("接线：找得到 do_POST", post_at > 0)
seg = srv_src[post_at:post_at + 1200]
call = seg.find("net_guard.cross_site(")
ctype_at = seg.find('self.headers.get("Content-Type")')
chk("接线：do_POST 问了 net_guard", call > 0)
chk("接线：问在解析 body / 判 Content-Type 之前", 0 < call < ctype_at, (call, ctype_at))
chk("接线：拒的时候回的是 403，且说清「没有执行」",
    "403" in seg[:1200] and "没有执行" in seg[:1200])


# ── 3. 真机：起服务，按浏览器的发法各来一遍 ────────────────────────
SB = pathlib.Path(tempfile.mkdtemp(prefix="gz-csrf-"))
atexit.register(lambda: shutil.rmtree(SB, ignore_errors=True))
env = dict(os.environ, GUIZANG_SELFTEST_DIR=str(SB),
           GUIZANG_DATA=str(SB / "data"), GUIZANG_BOOKS=str(SB / "books"),
           GUIZANG_SHOT_DIR=str(SB / "shots"))
for _d in ("data", "books", "shots"):
    (SB / _d).mkdir(parents=True, exist_ok=True)


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


port = free_port()
logp = SB / "server.log"
logf = open(logp, "w")
srv = subprocess.Popen([sys.executable, "ui_server.py", "--port", str(port)],
                       cwd=str(ROOT), env=env, stdout=logf, stderr=subprocess.STDOUT)
atexit.register(lambda: srv.terminate())
base = ""
for _ in range(160):
    m = re.search(r"http://127\.0\.0\.1:(\d+)", logp.read_text(errors="replace"))
    if m:
        try:
            cand = "http://127.0.0.1:%s" % m.group(1)
            json.loads(urllib.request.urlopen(cand + "/api/state", timeout=1).read())
            base = cand
            break
        except Exception:
            pass
    time.sleep(0.25)

if not base:
    chk("真机：服务起来了", False, logp.read_text(errors="replace")[-400:])
else:
    chk("真机：服务起来了", True)
    pnum, host = int(base.rsplit(":", 1)[1]), base.replace("http://", "")
    conn = http.client.HTTPConnection("127.0.0.1", pnum, timeout=8)

    def post(path, payload, headers, conn=conn):
        body = payload.encode("utf-8")
        conn.putrequest("POST", path, skip_accept_encoding=True)
        for k, v in headers.items():
            conn.putheader(k, v)
        conn.putheader("Content-Length", str(len(body)))
        conn.endheaders()
        conn.send(body)
        r = conn.getresponse()
        return r.status, r.read().decode("utf-8", "replace")

    def get(path, headers=None, conn=conn):
        conn.putrequest("GET", path, skip_accept_encoding=True)
        for k, v in (headers or {}).items():
            conn.putheader(k, v)
        conn.endheaders()
        r = conn.getresponse()
        return r.status, r.read().decode("utf-8", "replace")

    def today(kind="study"):
        st, txt = get("/api/activity")
        try:
            return int(json.loads(txt)[kind]["today"])
        except Exception:
            return -1

    # 3a. 浏览器那种「Blob 不带 type」的跨站请求：既没有预检、也没有 Content-Type。
    st, txt = post("/api/activity/tick", '{"kind":"study","seconds":300}',
                   {"Origin": "https://evil.example", "Host": host})
    chk("真机：跨站 + 无 Content-Type 被拒（这就是原来漏掉的那一种）", st == 403, (st, txt))
    chk("真机：拒的那一发一个字都没记进账本", today() == 0, today())

    # 3b. 同一连接上接着发下一发：读干净 body 才回，keep-alive 不许错位。
    st, txt = post("/api/activity/tick", '{"kind":"study","seconds":300}',
                   {"Host": host, "Sec-Fetch-Mode": "cors",
                    "Content-Type": "application/json"})
    chk("真机：被拒之后同一条连接还能用（keep-alive 没错位）", st == 200, (st, txt))
    chk("真机：不是浏览器（MCP 那份形状）照常做得成",
        st == 200 and json.loads(txt).get("added") == 300, txt)
    chk("真机：做得成的那一发真的记进了账本", today() == 300, today())

    # 3c. 同源：界面自己那一发。
    st, txt = post("/api/activity/tick", '{"kind":"write","seconds":120}',
                   {"Origin": "http://" + host, "Host": host,
                    "Content-Type": "application/json"})
    chk("真机：同源 + application/json 照常做成",
        st == 200 and json.loads(txt).get("added") == 120, (st, txt))

    # 3d. 三种跨站写法都必须拒，且账本不动。
    for label, hd in (
        ("Origin: null（沙箱 iframe）", {"Origin": "null"}),
        ("只有 Sec-Fetch-Site: cross-site", {"Sec-Fetch-Site": "cross-site"}),
        ("外站 Origin + application/json", {"Origin": "https://evil.example",
                                           "Content-Type": "application/json"}),
    ):
        hd = dict(hd, Host=host)
        st, txt = post("/api/activity/tick", '{"kind":"study","seconds":300}', hd)
        chk("真机：跨站被拒 · " + label, st == 403, (st, txt))
    chk("真机：三发跨站之后账本还是 300（不是「回了 403 却照做」）", today() == 300, today())

    # 3e. 读操作不设门：跨源 GET 的回包页面根本读不到，拦它只会误伤下载 / 封面。
    st, txt = get("/api/state", {"Origin": "https://evil.example", "Host": host})
    chk("真机：GET 带外站 Origin 仍照常回（只审写操作）", st == 200, (st, txt[:80]))

    conn.close()

shutil.rmtree(SB, ignore_errors=True)
bad = [c for c in checks if not c[0]]
print("\n%d/%d 通过" % (len(checks) - len(bad), len(checks)))
for _ok, name, extra in bad:
    print("  ✗ " + name + "  <- " + extra)
sys.exit(1 if bad else 0)
