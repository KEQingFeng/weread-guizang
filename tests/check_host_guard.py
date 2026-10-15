#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Host 头收口：只在本机那几个写法上应答，DNS rebinding 借不到这个头。

要守的是一个具体的洞，不是泛泛的安全洁癖。`net_guard.cross_site` 挡住了「用户随手
打开的外站页面替他点归藏」那一类：外站发来的写操作，`Origin` 与 `Host` 不同源，拒。
它的前提是「攻击者的页面」和「归藏」是两个源。DNS rebinding 把这个前提拆掉：

  1. 攻击者让 `evil.example` 的 DNS 先指自己的服务器（页面加载完）；
  2. 再把同一条记录改指 `127.0.0.1`（TTL 很短）；
  3. 页面接着 `fetch("http://evil.example:8770/api/state")` —— 浏览器眼里这还是
     `evil.example`，和页面同源，于是 `Sec-Fetch-Site: same-origin`、`Origin` 与
     `Host` 都是 `evil.example`，`cross_site` 一路放行；跨源读取限制也不再生效，
     整份账本读得到，写操作同样做得成（服务只听 127.0.0.1 拦不住这个 —— 请求是从
     用户这台机器上发出去的）。

浏览器一定会带上「用户以为自己在访问谁」这个 `Host`，rebinding 带的就是攻击者域名。
所以判据不是「Origin 与 Host 同不同源」（它们本来就同源），而是「Host 是不是本机自己」。
macOS 外壳 `shell/main.swift` 的 `isOutside()` 早就拿 `127.0.0.1` / `localhost` / `::1`
做这把尺子了，Python 这层没有 —— 这一条把它补上，并钉住两边是同一组字面量。

三层都钉，全部离线、不联网：

  ① 纯函数 `net_guard.host_allowed`：回环的几种写法放行；外站域名、像本机的子域名
     （`127.0.0.1.evil.com` / `localhost.evil.com`）、私网地址、写坏的端口与半个
     方括号一律拒；缺 Host 回 True（浏览器必带 Host，不带的多是本机进程）。
  ② 源码接线：`do_GET` 与 `do_POST` 都在路由之前问了这一句，回 403 + 人话。
  ③ 真机：in-process 起一个真 `ui_server.Handler`，按浏览器那种「同源 rebinding」
     形状发 GET / POST —— 外站 Host 必须 403、且一个数字都没记进账本；回环 Host
     照常 200。另有一条 ④ 对照：同样一发 `cross_site` 判不出来，说明新尺子不是空转。

跑法：.venv/bin/python tests/check_host_guard.py
"""
import json
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

import net_guard as ng   # noqa: E402
import ui_server         # noqa: E402

checks = []


def chk(name, ok, extra=""):
    checks.append((bool(ok), name, "" if ok else str(extra)[:300]))
    print(("  ok  " if ok else " FAIL ") + name + ("" if ok else "  <- " + str(extra)[:300]))


# ── ① 纯函数：Host 这把尺子的边界表 ───────────────────────────────
ALLOW = [
    ("本机 IPv4", "127.0.0.1"),
    ("本机 IPv4 带端口", "127.0.0.1:8770"),
    ("localhost", "localhost"),
    ("localhost 带端口", "localhost:8080"),
    ("大小写不算数", "LOCALHOST"),
    ("末尾点号归一掉", "127.0.0.1.:8770"),
    ("IPv6 回环（方括号 + 端口）", "[::1]:8770"),
    ("IPv6 回环（只有方括号）", "[::1]"),
    ("IPv6 回环（URL 解析后的裸形状，Swift 那边比的就是这个）", "::1"),
    ("首尾空白不算内容", "  127.0.0.1  "),
    ("缺头：浏览器必带 Host，不带的是本机进程", None),
    ("空串按缺头", ""),
]
DENY = [
    ("外站域名", "evil.example"),
    ("外站域名带端口", "evil.example:8770"),
    ("像是本机的子域名", "127.0.0.1.evil.com"),
    ("像是 localhost 的子域名", "localhost.evil.com"),
    ("私网地址（服务根本不在上面，也不认）", "192.168.1.5"),
    ("通配地址", "0.0.0.0"),
    ("半个方括号", "[::1"),
    ("端口写坏", "127.0.0.1:oops"),
    ("Host 里塞了 userinfo", "evil.example@127.0.0.1"),
    ("Host 里塞了 path", "evil.example/api/state"),
    ("Host 里塞了 fragment", "127.0.0.1#x"),
    ("两个 Host 拼在一起", "127.0.0.1:8080, evil.example"),
    ("空主机名", "http://"),
]

for name, host in ALLOW:
    chk("放行：" + name, ng.host_allowed(host) is True, host)
for name, host in DENY:
    chk("拒绝：" + name, ng.host_allowed(host) is False, host)

# ④ 对照（放在前面，因为它说明「为什么需要这把尺子」）：rebinding 那一发，
# cross_site 本来就该放行 —— 所以只能在 Host 这一层拦。
chk("对照：同源形状的 rebinding，cross_site 判不出来（Origin 与 Host 都是攻击者域名）",
    ng.cross_site("POST", {"Origin": "http://evil.example",
                           "Sec-Fetch-Site": "same-origin"}, "evil.example") is False)
chk("对照：GET 那一发 cross_site 更不管（它只审写操作）",
    ng.cross_site("GET", {"Origin": "http://evil.example"}, "evil.example") is False)
chk("而 host_allowed 两发都拦得住",
    ng.host_allowed("evil.example") is False)

# ④b 一把尺子：与 shell/main.swift 的 isOutside() 同一组字面量。
SWIFT = (selftest.REPO / "shell" / "main.swift").read_text(encoding="utf-8")
iso = SWIFT[SWIFT.index("private func isOutside"):]
iso = iso[:iso.index("private func openOutside")]
chk("Swift 那把尺子就是这三个（isOutside 里逐个比对）",
    all(('"%s"' % h) in iso for h in ng.LOOPBACK_HOSTS),
    [h for h in ng.LOOPBACK_HOSTS if ('"%s"' % h) not in iso])
chk("Python 这把尺子不多不少，就是同样的三个",
    tuple(ng.LOOPBACK_HOSTS) == ("127.0.0.1", "localhost", "::1"), ng.LOOPBACK_HOSTS)


# ── ② 源码接线：两个入口都在路由之前问这一句 ─────────────────────
SERVER = (selftest.REPO / "ui_server.py").read_text(encoding="utf-8")
GET = SERVER[SERVER.index("    def do_GET(self):"):SERVER.index("    def do_POST(self):")]
POST = SERVER[SERVER.index("    def do_POST(self):"):]
chk("接线：do_GET 问了 host_allowed", "net_guard.host_allowed(" in GET)
chk("接线：do_POST 问了 host_allowed", "net_guard.host_allowed(" in POST)
chk("接线：问在 do_GET 解请求行之前",
    0 < GET.find("net_guard.host_allowed(") < GET.find("_req_target(self.path)"))
# do_POST 的第一段留给 cross_site / Content-Type / body 那三件事（check_csrf 的接线断言
# 就盯着那一段），Host 门放在读完 body 之后、第一条路由之前 —— body 已经读完，直接回
# 也不用补读，keep-alive 一样不错位。
chk("接线：do_POST 的 Host 门在第一条路由派发之前",
    0 < POST.find("net_guard.host_allowed(") < POST.find('if u.path == '))
chk("接线：拒的时候回 403 + 人话",
    "403" in GET and "不是冲本机地址来的" in GET
    and "403" in POST and "不是冲本机地址来的" in POST)


# ── ③ 真机：in-process 起一个真 Handler，按浏览器的发法各来一遍 ───
srv = ThreadingHTTPServer(("127.0.0.1", 0), ui_server.Handler)
PORT = srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start()
time.sleep(0.1)


def raw(method, target, host, body=b"", timeout=5.0):
    """发一条请求，回 (响应字节, 是否读超时)。Host 由调用方给，body 自带 Content-Length。"""
    head = (method.encode() + b" " + target.encode("latin-1") + b" HTTP/1.1\r\n"
            + (b"Host: " + host.encode("latin-1") + b"\r\n" if host is not None else b"")
            + b"Connection: close\r\n")
    if body:
        head += b"Content-Type: application/json\r\nContent-Length: %d\r\n" % len(body)
    c = socket.create_connection(("127.0.0.1", PORT), 3)
    try:
        c.sendall(head + b"\r\n" + body)
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


def tick(kind="study", seconds=300):
    return raw("POST", "/api/activity/tick",
               "127.0.0.1:%d" % PORT,
               json.dumps({"kind": kind, "seconds": seconds}).encode())


def today():
    data, _ = raw("GET", "/api/activity", "127.0.0.1:%d" % PORT)
    try:
        return int(json.loads(body_of(data))["study"]["today"])
    except Exception:
        return -1


# 对照：正常回环照旧 200（收口没把自家界面挡在门外）。
data, hung = raw("GET", "/api/state", "127.0.0.1:%d" % PORT)
try:
    _j = json.loads(body_of(data))
except Exception:
    _j = None
chk("对照：Host 是 127.0.0.1，/api/state 照常 200 + 完整 JSON",
    not hung and "200" in status_of(data) and isinstance(_j, dict) and "task" in _j,
    (hung, status_of(data), body_of(data)[:120]))
data, hung = raw("GET", "/api/state", "localhost:%d" % PORT)
chk("对照：Host 是 localhost 也一样放行", not hung and "200" in status_of(data),
    (hung, status_of(data)))

# 真正的洞：rebinding 那一发 GET —— 读整份账本。
data, hung = raw("GET", "/api/state", "evil.example:%d" % PORT)
chk("真机：外站 Host 读 /api/state 被 403（原来能整份读走）",
    not hung and "403" in status_of(data) and json.loads(body_of(data)).get("ok") is False,
    (hung, status_of(data), body_of(data)[:120]))
chk("真机：403 本身是完整响应（带 Content-Length，不是空响应）",
    b"content-length:" in data.split(b"\r\n\r\n", 1)[0].lower(), data[:120])
chk("真机：回过的人话是「不是冲本机地址来的」",
    "不是冲本机地址来的" in data.decode("utf-8", "replace"), body_of(data)[:160])

# 写操作：rebinding 那一发 POST 不许落账。
before = today()
data, hung = raw("POST", "/api/activity/tick", "evil.example:%d" % PORT,
                 json.dumps({"kind": "study", "seconds": 300}).encode())
chk("真机：外站 Host 写 /api/activity/tick 被 403",
    not hung and "403" in status_of(data), (hung, status_of(data), body_of(data)[:120]))
chk("真机：被拒的那一发一个数字都没记进账本", today() == before, (before, today()))

# 同一发换成回环 Host 就该成功 —— 证明上面拒的是 Host，不是把写口整个堵死。
data, hung = tick()
chk("对照：回环 Host 的同一发写操作照常 200",
    not hung and "200" in status_of(data), (hung, status_of(data), body_of(data)[:120]))
chk("真机：成功那一发真的记进了账本", today() == before + 300, (before, today()))

# 「像是本机」的域名也不行：这是最容易写错的一档（后缀匹配就会漏）。
for bad_host in ("127.0.0.1.evil.example:%d" % PORT, "localhost.evil.example:%d" % PORT):
    data, hung = raw("GET", "/api/state", bad_host)
    chk("真机：像本机的域名 %s 也 403" % bad_host,
        not hung and "403" in status_of(data), (hung, status_of(data)))

srv.shutdown()
srv.server_close()

# ── 汇总 ────────────────────────────────────────────────────────
bad = [n for ok, n, _ in checks if not ok]
print()
print("Host 头收口体检：通过 %d 项，失败 %d 项" % (len(checks) - len(bad), len(bad)))
for n in bad:
    print("  ✗ " + n)
sys.exit(1 if bad else 0)
