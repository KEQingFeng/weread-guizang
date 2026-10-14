#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""请求体长度（Content-Length）脏值不许把 POST 连接掐断 / 挂死。

`do_POST` 原来是 `n = int(self.headers.get("Content-Length") or 0)`。正常浏览器永远
发的是一串非负整数，所以这一行看着没事；但坏客户端（或手写的探针）有两条路会撞上：

  · `Content-Length: abc` —— `int("abc")` 当场抛 ValueError，异常一路穿出 do_POST。
    `BaseHTTPRequestHandler` 不会替你回 500，它只是把连接关掉，客户端收到的是
    RemoteDisconnected（我这边实测的响应就是**空字节**）。这正是 check_query_args.py
    里那种「点了没反应」的症状，只是从查询串挪到了请求头。
  · `Content-Length: -1` —— `self.rfile.read(-1)` 的语义是「一直读到客户端关连接」，
    于是这条连接的线程挂在那儿（实测客户端读超时，服务端 traceback 都没有一条）。

修法是把长度也收口（ui_server._body_len）：认不出（小数 / 负号 / `1e3` / 全角数字）
一律回 None，`do_POST` 回 400 并主动断连，绝不带着一份猜出来的长度往下走 ——
读 0 会把残留的 body 留在流里被 keep-alive 当成下一个请求。

这一条三层都钉：
  ① 纯函数：好长度原样收、脏长度回 None、没这个头算 0；
  ② 源码接线：do_POST 走的是 _body_len，不再有裸 `int(...Content-Length...)`；
  ③ 真机：自己起一个 in-process `ThreadingHTTPServer` + `ui_server.Handler`，用裸 HTTP
     各发一遍。不联网、不碰用户真实数据（数据目录在 import ui_server 之前就指进临时
     沙盒），所以归 --fast。
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


# ── ① 纯函数：长度怎么收 ─────────────────────────────────────────
bl = ui_server._body_len
chk("正常数字原样收", bl("5") == 5 and bl("0") == 0 and bl("1048576") == 1048576,
    (bl("5"), bl("0")))
chk("前后空白也认（代理补的空格）", bl(" 5 ") == 5 and bl("\t12\r\n") == 12,
    (bl(" 5 "), bl("\t12\r\n")))
chk("没带这个头算 0（空 body）", bl(None) == 0 and bl("") == 0 and bl("   ") == 0,
    (bl(None), bl(""), bl("   ")))
chk("abc / 空里带字回 None（原来就是这一路 int() 抛）", bl("abc") is None)
chk("负数回 None（read(-1) 会一直读到客户端关连接）", bl("-1") is None and bl("-0") is None)
chk("小数 / 科学计数 / 十六进制回 None", bl("3.7") is None and bl("1e3") is None
    and bl("0x10") is None, (bl("3.7"), bl("1e3"), bl("0x10")))
chk("带正号也不算（协议里是 1*DIGIT，收得下但别猜）", bl("+5") is None)
chk("全角数字不算（str.isdigit 认它、int() 也认它，长度是协议字段）",
    bl("１２３") is None and bl("٥") is None, (bl("１２３"), bl("٥")))
chk("非标量（同一个头传两遍）回 None，不抛",
    bl(["5", "5"]) is None and bl({"5": 1}) is None, (bl(["5", "5"]), bl({"5": 1})))

# ── ② 源码接线：do_POST 必须走它 ─────────────────────────────────
POST = SERVER[SERVER.index("    def do_POST(self):"):]
chk("do_POST 用的是 _body_len 收口", "_body_len(self.headers.get(\"Content-Length\"))" in POST)
chk("裸 int(Content-Length) 那条老写法不在了",
    "int(self.headers.get(\"Content-Length\")" not in POST)
chk("长度认不出时回 400 并主动断连（不留残留 body 给下一个请求）",
    "self.close_connection = True" in POST and "400" in POST)

# ── ③ 真机：in-process 起一个真 Handler，用裸 HTTP 发脏长度 ───────
srv = ThreadingHTTPServer(("127.0.0.1", 0), ui_server.Handler)
PORT = srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start()
time.sleep(0.1)


def raw_post(extra_headers, body=b"", path="/api/activity/tick", timeout=5.0):
    """发一条 POST，回 (响应字节, 是否读超时)。Connection: close 让服务端回完就关。"""
    req = (b"POST " + path.encode() + b" HTTP/1.1\r\n"
           b"Host: 127.0.0.1\r\n"
           b"Content-Type: application/json\r\n"
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
    return data.split(b"\r\n", 1)[0].decode("latin-1", "replace")


def body_of(data):
    head, _, rest = data.partition(b"\r\n\r\n")
    m = [l for l in head.split(b"\r\n") if l.lower().startswith(b"content-length:")]
    if not m:
        return rest
    return rest[:int(m[0].split(b":", 1)[1])]


good_body = b'{"kind":"study","seconds":5}'
data, hung = raw_post(b"Content-Length: %d\r\n" % len(good_body), good_body)
chk("正常长度照走：200 + 真 JSON（不是被这一改挡掉）",
    not hung and "200" in status_of(data) and b'"added"' in data, status_of(data))

data, hung = raw_post(b"Content-Length: abc\r\n")
chk("Content-Length: abc 回 400 + JSON（原来是一条空响应、连接被掐）",
    not hung and "400" in status_of(data) and b'"ok"' in body_of(data),
    (hung, status_of(data), body_of(data)[:120]))

t0 = time.time()
data, hung = raw_post(b"Content-Length: -1\r\n", good_body, timeout=4.0)
chk("Content-Length: -1 当场回 400（不再 read(-1) 挂住连接）",
    not hung and "400" in status_of(data) and time.time() - t0 < 3.5,
    (hung, status_of(data), round(time.time() - t0, 2)))

data, hung = raw_post(b"Content-Length: 3.7\r\n")
chk("Content-Length: 3.7 回 400（不截成 3）",
    not hung and "400" in status_of(data), (hung, status_of(data)))

data, hung = raw_post(b"")
chk("没带 Content-Length：照常当空 body 处理（这条 POST 仍回 200）",
    not hung and "200" in status_of(data), (hung, status_of(data)))

# 4xx 那份 400 还得是「能读完整份」的正常响应：有 Content-Length、有 JSON 正文。
data, _ = raw_post(b"Content-Length: abc\r\n")
head = data.split(b"\r\n\r\n", 1)[0]
chk("400 的响应本身是完整的（带 Content-Length 头，不是半截）",
    b"content-length:" in head.lower() and b'"msg"' in body_of(data), head[:160])

srv.shutdown()
srv.server_close()

# ── 汇总 ────────────────────────────────────────────────────────
bad = [n for ok, n, _ in checks if not ok]
print()
print("请求体长度收口体检：通过 %d 项，失败 %d 项" % (len(checks) - len(bad), len(bad)))
for n in bad:
    print("  ✗ " + n)
sys.exit(1 if bad else 0)
