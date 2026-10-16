#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""请求体长度上了上限：一个离谱的 `Content-Length` 不许把服务端的内存交出去。

上一条（check_post_length.py）收的是「长度认不出来」那一档：`abc` / 负数 / 全角数字。
可还有一档它没收 —— **长度本身完全合法，只是大得离谱**。

为什么这一档比看起来重：`do_POST` 读 body 这一步排在两道门**之前**。

  · `net_guard.cross_site` 要把 body 读干净才回 403（读一半就不读了会把残留字节
    留在流里，keep-alive 下被当成下一个请求 —— check_csrf.py 第 3b 条正钉着
    「被拒之后同一条连接还能用」）；
  · Host 门（DNS rebinding 那一档）更靠后，排在 `json.loads` 之后。

所以一个随手打开的网页（或一次 rebinding）只要发一句 `Content-Length: 5368709120`，
服务端就会照着这个数去 `read(n)`：内存一路涨、线程一路挂，直到自己 OOM —— 拒是最后
拒了，可代价先付了。收口不是「把拒绝提前」（那会动 keep-alive 的错位规矩），而是给
长度加一个上限（`ui_server.MAX_POST_BODY`）：超过它就和「认不出」同一条路，回 None，
`do_POST` 回 400 并主动断连，一个字节都不读。

上限取的是最大那条合法请求的余量：一本封顶 200MB 的书 base64 之后约 267MB，所以
`MAX_POST_BODY = book_import.MAX_BYTES * 2`（400MB），正常导入一个不放宽。

同一个函数还顺手收了一档：Python 3.11+ 给「十进制串转 int」设了 4300 位的默认上限，
一个五千位的 `Content-Length` 原本会让 `int(text)` 当场 ValueError、被总兜底接成一句
英文报错（200 + `Exceeds the limit…`）。要判的本来就只是「有没有超过上限」，所以先比
有效位数再转 —— 位数就超了的连转都不转。

这一条三层都钉：
  ① 纯函数：边界上收得下、过线就回 None、五千位的长度不喂给 `int()`；好值一个不变；
     外加一条「旧写法 `int(text)` 确实会收下这个数」的对照，防空转；
  ② 源码接线：`_body_len` 里真拿 `MAX_POST_BODY` 比过，且上限确实挂在
     `book_import.MAX_BYTES` 上（不写死一个魔法数）；
  ③ 真机：in-process `ThreadingHTTPServer` + `ui_server.Handler`，发一个超大
     `Content-Length` 但**一个字节的 body 都不发** —— 服务端必须当场回 400 并断连
     （老写法会一直等 body，客户端读超时）；边界那一发当镜子，反过来要读超时。
     不联网、不碰真实数据（数据目录在 import ui_server 之前就指进临时沙盒），归 --fast。
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

import book_import  # noqa: E402
import ui_server  # noqa: E402

SERVER = (selftest.REPO / "ui_server.py").read_text(encoding="utf-8")

checks = []


def chk(name, ok, extra=""):
    checks.append((bool(ok), name, "" if ok else str(extra)[:300]))
    print(("  ok  " if ok else " FAIL ") + name + ("" if ok else "  <- " + str(extra)[:300]))


# ── ① 纯函数：长度怎么收（认得出 / 太大） ─────────────────────────
LIMIT = ui_server.MAX_POST_BODY
bl = ui_server._body_len

chk("上限本身是个正数，且比一本封顶书 base64 之后还大",
    LIMIT > 0 and LIMIT >= book_import.MAX_BYTES * 4 // 3,
    (LIMIT, book_import.MAX_BYTES))

chk("边界上收得下：等于上限的那个长度原样回",
    bl(str(LIMIT)) == LIMIT, bl(str(LIMIT)))
chk("过线就拒：上限 + 1 回 None（不再往下 read）",
    bl(str(LIMIT + 1)) is None, bl(str(LIMIT + 1)))
chk("离谱的大数字回 None，且不抛（Int 收得下、只是不读）",
    bl(str(10 ** 18)) is None and bl("9" * 18) is None)
chk("五千位的十进制串也回 None（先比位数，不撞 int() 的 4300 位上限）",
    bl("1" + "0" * 5000) is None)
chk("开头的 0 不算数位：`000` + 上限 仍收得下、同值",
    bl("000" + str(LIMIT)) == LIMIT and bl("0000000005") == 5)

# 对照：裸 int() 对超长十进制串确实会抛（Python 3.11+ 的 4300 位上限）。老版本 /
# 已放宽的构建上这条不成立，所以只在真抛时才登记 —— 不装绿。
try:
    int("1" + "0" * 5000)
except ValueError:
    chk("对照：裸 int() 对五千位十进制串确实抛（位数这一刀不是空转）", True)

chk("好长度一个不变：5 / 0 / 1MB / 前后空白",
    bl("5") == 5 and bl("0") == 0 and bl("1048576") == 1048576
    and bl(" 5 ") == 5 and bl("\t12\r\n") == 12)
chk("认不出的照旧回 None（这一档不许被上限这刀带偏）",
    bl("abc") is None and bl("-1") is None and bl("3.7") is None
    and bl("１２３") is None and bl("+5") is None)
chk("没带这个头算 0（空 body 照旧）", bl(None) == 0 and bl("") == 0)

# 对照：旧写法（`int(text)` 直接收）确实会把这个数当成「该读多少字节」——
# 没有这条，上限那几条断言在「上限被拿掉」时也可能照样绿。
chk("对照：旧写法 int(text) 确实收得下这个离谱的长度（不是空转）",
    int("9" * 18) > LIMIT and int(str(LIMIT + 1)) > LIMIT)

# ── ② 源码接线：上限挂在 _body_len 上、并且绑着 book_import.MAX_BYTES ──
BODY_LEN = SERVER[SERVER.index("def _body_len(raw):"):SERVER.index("def _body_list(")]
chk("接线：_body_len 里比过 MAX_POST_BODY", "MAX_POST_BODY" in BODY_LEN and ">" in BODY_LEN)
chk("接线：先比有效位数再转 int（几千位的长度不喂给 int()）",
    "_MAX_POST_BODY_DIGITS" in BODY_LEN and BODY_LEN.index("_MAX_POST_BODY_DIGITS")
    < BODY_LEN.index("int(digits)"))
chk("接线：有效位数从 MAX_POST_BODY 推出来，不写死魔法数",
    "_MAX_POST_BODY_DIGITS = len(str(MAX_POST_BODY))" in SERVER)
chk("接线：上限从 book_import.MAX_BYTES 推出来，不写死魔法数",
    "MAX_POST_BODY = book_import.MAX_BYTES * 2" in SERVER)
POST = SERVER[SERVER.index("    def do_POST(self):"):]
chk("接线：do_POST 仍走 _body_len，认不出 / 太大时回 400 并断连",
    "_body_len(self.headers.get(\"Content-Length\"))" in POST
    and "self.close_connection = True" in POST and "400" in POST)

# ── ③ 真机：裸 HTTP 发一个超大的 Content-Length，不许等到 body 才回话 ──
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
    return (data.split(b"\r\n", 1)[0] or b"<EMPTY>").decode("latin-1", "replace")


def body_of(data):
    head, _, rest = data.partition(b"\r\n\r\n")
    m = [l for l in head.split(b"\r\n") if l.lower().startswith(b"content-length:")]
    if not m:
        return rest
    return rest[:int(m[0].split(b":", 1)[1])]


good = b'{"kind":"study","seconds":5}'
data, hung = raw_post(b"Content-Length: %d\r\n" % len(good), good)
chk("真机：正常一发照旧 200 + 真 JSON（这一刀没误伤）",
    not hung and "200" in status_of(data) and b'"added"' in data, status_of(data))

# 关键一发：声明一个超大长度，body 一个字节都不发。
# 老写法会 `read(n)` 一直等这些字节 —— 客户端只能读到超时；新写法当场回 400。
t0 = time.time()
data, hung = raw_post(b"Content-Length: %d\r\n" % (LIMIT + 1), timeout=4.0)
chk("真机：上限 + 1 且 body 一个字节不发 → 当场 400（没有去读那 400MB）",
    not hung and "400" in status_of(data) and time.time() - t0 < 3.5,
    (hung, status_of(data), round(time.time() - t0, 2)))
chk("真机：这一发的 400 也是完整响应（有 JSON 正文）",
    b'"ok"' in body_of(data) and b'"msg"' in body_of(data), body_of(data)[:160])

t1 = time.time()
data, hung = raw_post(b"Content-Length: " + b"9" * 18 + b"\r\n", timeout=4.0)
chk("真机：18 位的大长度也当场 400（int() 收得下，但不拿它去 read）",
    not hung and "400" in status_of(data) and time.time() - t1 < 3.5,
    (hung, status_of(data), round(time.time() - t1, 2)))

t2 = time.time()
data, hung = raw_post(b"Content-Length: " + b"1" + b"0" * 5000 + b"\r\n", timeout=4.0)
chk("真机：五千位的长度回 400（不是 200 加一句 int() 的英文报错）",
    not hung and "400" in status_of(data) and b"Exceeds the limit" not in body_of(data)
    and time.time() - t2 < 3.5,
    (hung, status_of(data), body_of(data)[:160]))

# 跨站那一发同样先撞上限：`cross_site` 的门要把 body 读干净才回 403，
# 若上限排在它后面，这一发就会先被读走 —— 顺序正好由这一条钉住。
data, hung = raw_post(b"Content-Length: %d\r\n" % (LIMIT + 1)
                      + b"Origin: https://evil.example\r\n", timeout=4.0)
chk("真机：跨站 + 超大长度也是当场 400（上限排在 cross_site 补读之前）",
    not hung and "400" in status_of(data), (hung, status_of(data)))

# 镜子：边界那一发（正好等于上限）仍去读 body，客户端必然超时 ——
# 证明上限卡在正好那个位置，不是随手把门槛压低了。
data, hung = raw_post(b"Content-Length: %d\r\n" % LIMIT, timeout=1.2)
chk("对照：正好等于上限时仍会去读 body → 客户端读超时（门槛没被压低）",
    hung and not data, (hung, status_of(data)))

srv.shutdown()
srv.server_close()

# ── 汇总 ────────────────────────────────────────────────────────
bad = [n for ok, n, _ in checks if not ok]
print()
print("请求体长度上限体检：通过 %d 项，失败 %d 项" % (len(checks) - len(bad), len(bad)))
for n in bad:
    print("  ✗ " + n)
sys.exit(1 if bad else 0)
