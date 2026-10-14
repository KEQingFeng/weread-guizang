#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""请求行里的目标地址解不出来时，不许把连接甩成空响应。

`do_GET` / `do_POST` 第一行都是 `u = urlparse(self.path)`。正常浏览器发的都是
源形式（`/api/state`），这一行看着没事；但 HTTP 允许**绝对形式**的请求目标
（代理就是这么发的：`GET http://host/path HTTP/1.1`），而 `urlparse` 对绝对形式里
写坏的 netloc 会**自己**抛 ValueError：

  · 半个 IPv6 方括号：`http://[::1`、`http://[`、`https://[fe80::1`、`http://[::1]]/x`
    → `ValueError: Invalid IPv6 URL`；
  · NFKC 归一化认不出的字符（全角斜杠 `http://／host`）
    → `ValueError: netloc ... contains invalid characters under NFKC normalization`。

这不是任何一条路由的输入，而是请求行本身写坏了；可这行解析在整个 Handler 的最
前面、没有第二道 try，异常一路穿到 `BaseHTTPRequestHandler` —— 它不会替你回 500，
只是把连接关掉。客户端（内置浏览器、MCP 适配器、前端 fetch）收到的是**空响应**，
与 `check_post_length` 里 `Content-Length: abc`、`check_browse_url` 里半个方括号
那次是同一个症状。

注意 `http.server` 只把以 `//` 开头的裸路径（`//[::1`）折成 `/[::1`，绝对形式那
一种它不碰 —— 所以这条口子确实还在（check_browse_url 修的是 query 里的 url 参数，
不是请求行本身）。

修法是 ui_server._req_target：解不出来回 None，`do_GET` / `do_POST` 回 400 人话，
绝不把裸 ValueError 往外抛。

这一条三层都钉：
  ① 纯函数：好地址照解、绝对形式照解、写坏的两种一律回 None（不抛）；
  ② 源码接线：do_GET / do_POST 都走 _req_target，不再有裸 `urlparse(self.path)`；
  ③ 真机：自己起一个 in-process `ThreadingHTTPServer` + `ui_server.Handler`，用裸 HTTP
     发绝对形式的坏目标，验它回 400 + 完整 JSON（原来是一条空响应、连接被掐）。
     不联网、不碰用户真实数据（数据目录在 import ui_server 之前就指进临时沙盒），
     所以归 --fast。
"""
import pathlib
import socket
import sys
import threading
import time
from http.server import ThreadingHTTPServer

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import selftest  # noqa: E402

sys.path.insert(0, str(selftest.REPO))
selftest.export_sandbox_env()             # 必须在 import ui_server 之前设好环境变量

import ui_server  # noqa: E402

SERVER = (selftest.REPO / "ui_server.py").read_text(encoding="utf-8")

checks = []


def chk(name, ok, extra=""):
    checks.append((bool(ok), name, "" if ok else str(extra)[:300]))
    print(("  ok  " if ok else " FAIL ") + name + ("" if ok else "  <- " + str(extra)[:300]))


# ── ① 纯函数：目标地址怎么收 ─────────────────────────────────────
rt = ui_server._req_target

for good, path, query in [
    ("/api/state", "/api/state", ""),
    ("/api/log?tail=5", "/api/log", "tail=5"),
    ("/", "/", ""),
    ("http://example.com/x?y=1", "/x", "y=1"),      # 绝对形式、host 正常
    ("http://127.0.0.1:8080/api/md", "/api/md", ""),
]:
    r = rt(good)
    chk("好地址照解 %r" % good,
        r is not None and r.path == path and r.query == query,
        None if r is None else (r.path, r.query))

# 写坏的目标：urlparse 自己会抛的那几种写法。
BAD_TARGETS = ["http://[::1", "http://[", "https://[fe80::1", "http://[::1]]/x",
               "http://／host/api/state"]           # 最后一条是全角斜杠（NFKC）
for bad in BAD_TARGETS:
    try:
        out = rt(bad)
        chk("坏目标 %r 该收成 None" % bad, out is None, out)
    except Exception as e:                           # noqa: BLE001 —— 就是来找裸异常的
        chk("坏目标 %r 该收成 None" % bad, False, "%s: %s" % (type(e).__name__, e))

# 对照：裸 urlparse 确实会抛 —— 说明这条门禁不是空转（改回裸 urlparse 就红）。
from urllib.parse import urlparse as _bare  # noqa: E402
raised = False
try:
    _bare("http://[::1")
except ValueError:
    raised = True
chk("对照：裸 urlparse 对半个方括号确实抛 ValueError（收口不是空转）", raised)

# ── ② 源码接线：两个入口都得走 _req_target ───────────────────────
GET = SERVER[SERVER.index("    def do_GET(self):"):SERVER.index("    def do_POST(self):")]
POST = SERVER[SERVER.index("    def do_POST(self):"):]
chk("do_GET 走 _req_target 收口", "_req_target(self.path)" in GET)
chk("do_POST 走 _req_target 收口", "_req_target(self.path)" in POST)
chk("两个入口都不再有裸 urlparse(self.path)",
    "urlparse(self.path)" not in GET and "urlparse(self.path)" not in POST)
chk("解不出来时回 400 + 人话",
    "400" in GET and "请求地址解不出来" in GET
    and "400" in POST and "请求地址解不出来" in POST)

# ── ③ 真机：in-process 起一个真 Handler，发绝对形式的坏目标 ───────
srv = ThreadingHTTPServer(("127.0.0.1", 0), ui_server.Handler)
PORT = srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start()
time.sleep(0.1)


def raw(method, target, extra_headers=b"", body=b"", timeout=5.0):
    """发一条请求，回 (响应字节, 是否读超时)。Connection: close 让服务端回完就关。"""
    req = (method.encode() + b" " + target.encode("latin-1") + b" HTTP/1.1\r\n"
           b"Host: 127.0.0.1\r\n"
           b"Connection: close\r\n" + extra_headers + b"\r\n" + body)
    c = socket.create_connection(("127.0.0.1", PORT), 3)
    try:
        c.sendall(req)
        c.settimeout(timeout)
        data = b""
        while True:
            chunk = c.recv(4096)
            if not chunk:
                return data, False
            data += chunk
    except socket.timeout:
        return data, True
    finally:
        c.close()


def status_of(data):
    return (data.split(b"\r\n", 1)[0] or b"<EMPTY>").decode("latin-1", "replace")


def body_of(data):
    head, _, rest = data.partition(b"\r\n\r\n")
    m = [l for l in head.split(b"\r\n") if l.lower().startswith(b"content-length:")]
    if not m:
        return rest
    return rest[:int(m[0].split(b":", 1)[1])]


# 对照：正常源形式照旧 200 —— 收口没把路由挡掉。
data, hung = raw("GET", "/api/state")
chk("对照：正常源形式 /api/state 仍回 200",
    not hung and "200" in status_of(data), (hung, status_of(data)[:80]))

# 对照：绝对形式但 host 正常，也照旧能路由（不是把绝对形式整个禁掉）。
data, hung = raw("GET", "http://example.com/api/state")
chk("对照：绝对形式 + 正常 host 仍回 200（没把绝对形式整个禁掉）",
    not hung and "200" in status_of(data), (hung, status_of(data)[:80]))

# 真正的口子：绝对形式里的半个方括号，GET / POST 都得回 400 人话。
for method, target in [
    ("GET", "http://[::1"),
    ("GET", "http://["),
    ("GET", "https://[fe80::1"),
    ("GET", "http://[::1]]/x"),
    ("POST", "http://[::1"),
]:
    data, hung = raw(method, target)
    chk("真机 %s %s：回 400 + 完整 JSON（原来是一条空响应、连接被掐）"
        % (method, target),
        not hung and data and "400" in status_of(data) and b'"ok"' in body_of(data),
        (hung, status_of(data), body_of(data)[:120]))
    head = data.split(b"\r\n\r\n", 1)[0]
    chk("真机 %s %s：响应本身是完整的（带 Content-Length）" % (method, target),
        b"content-length:" in head.lower(), head[:160])

# 人话本身也在：界面 / 适配器要能显示它，不能是 Python 的 Invalid IPv6 URL。
data, _ = raw("GET", "http://[::1")
chk("人话是「请求地址解不出来」而不是 Python 的 Invalid IPv6 URL",
    "请求地址解不出来" in data.decode("utf-8", "replace")
    and "Invalid IPv6" not in data.decode("utf-8", "replace"),
    body_of(data)[:160])

srv.shutdown()
srv.server_close()

# ── 汇总 ────────────────────────────────────────────────────────
bad = [n for ok, n, _ in checks if not ok]
print()
print("请求行目标地址收口体检：通过 %d 项，失败 %d 项" % (len(checks) - len(bad), len(bad)))
for n in bad:
    print("  ✗ " + n)
sys.exit(1 if bad else 0)
