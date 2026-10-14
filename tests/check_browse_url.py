#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""内置浏览器：解不出来的地址要回一页人话，不能把 /api/browse 甩成空响应。

`web_browse.normalize` 里那行 `urllib.parse.urlparse(u)` 会**自己**抛异常：
半个 IPv6 方括号（`http://[::1`、`https://[`、`http://[::1]]/x`）会让它当场
`ValueError: Invalid IPv6 URL`。这不是 `Blocked`，而这条路的两处兜底都只接
`Blocked`：

  · `browse()` 只 `except Blocked` 接 normalize，于是 ValueError 一路穿出去；
  · `/api/browse` 那条路由拿了结果就 `if not r.get("ok")`，没有第二道 try。

`do_GET` 从头到尾也没有兜底，异常穿到 `BaseHTTPRequestHandler` 手上就是把连接
关掉 —— 客户端收到的是**空响应**（与 check_post_length 里 `Content-Length: abc`
同一个症状）。用户在地址栏敲一个写坏的方括号地址、或外站页面里的链接拼成了
半个方括号，看到的不是「这一页没取回来」那张自带错误页，而是 iframe 一片空白；
界面这一层根本没机会说人话。剪藏那边早就收口了（clip_article.url_block_reason
的 `except ValueError` 注释里点名的就是这一类），内置浏览器这一份漏了。

修法与剪藏同口径：解不出来的地址按 Blocked 拒绝，绝不把裸 ValueError 往外抛。

这一条三层都钉：
  ① 纯函数：半个方括号 / 多余的方括号一律 Blocked 带人话，不进 isinstance 判断前的裸异常；
  ② 对照 + 接线：裸 urlparse 确实会抛（说明这条门禁不是空转）、normalize 里有这道收口、
     跳转守卫也只把 Blocked 翻成 HTTPError；
  ③ 真机：自己起一个 in-process `ThreadingHTTPServer` + `ui_server.Handler`，用裸 HTTP
     GET 那条 `/api/browse`，验它回的是 200 + 完整的一页人话（原来是一条空响应）。
     不联网（地址在发请求之前就被拒了）、不碰用户真实数据。
"""
import pathlib
import socket
import sys
import threading
import time
import urllib.error
import urllib.parse
from http.server import ThreadingHTTPServer

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import selftest  # noqa: E402

sys.path.insert(0, str(selftest.REPO))
selftest.export_sandbox_env()             # 必须在 import ui_server 之前设好环境变量

import ui_server  # noqa: E402
import web_browse as wb  # noqa: E402

SOURCE = (selftest.REPO / "web_browse.py").read_text(encoding="utf-8")

checks = []


def chk(name, ok, extra=""):
    checks.append((bool(ok), name, "" if ok else str(extra)[:300]))
    print(("  ok  " if ok else " FAIL ") + name + ("" if ok else "  <- " + str(extra)[:300]))


# 解不出来的方括号地址：urlparse 自己会抛 ValueError 的那几种写法。
BAD_BRACKETS = ["http://[::1", "https://[", "http://[::1]]/x", "http://[",
                "HTTP://[::1", "  http://[::1  "]

# ── ① 纯函数：解不出来 = Blocked 带人话，不是裸 ValueError ──────
for bad in BAD_BRACKETS:
    try:
        out = wb.normalize(bad)
        chk("normalize(%r) 该拒却放行" % bad, False, "放行成 %r" % out)
    except wb.Blocked as e:
        chk("normalize(%r) 收成 Blocked 带人话" % bad, bool(str(e).strip()), str(e))
    except Exception as e:                       # noqa: BLE001 —— 就是来找裸异常的
        chk("normalize(%r) 收成 Blocked 带人话" % bad, False,
            "%s: %s" % (type(e).__name__, e))

# 至少有一条钉住这句人话本身：界面那页要显示它，不能是 `Invalid IPv6 URL`。
try:
    wb.normalize("http://[::1")
except wb.Blocked as e:
    chk("人话是「解不出来」而不是 Python 的 Invalid IPv6 URL",
        "解不出来" in str(e) and "Invalid IPv6" not in str(e), str(e))

for bad in BAD_BRACKETS:
    try:
        r = wb.browse(bad)
        chk("browse(%r) 永远回 dict、不抛" % bad,
            isinstance(r, dict) and r.get("ok") is False and bool(r.get("msg")), r)
    except Exception as e:                       # noqa: BLE001
        chk("browse(%r) 永远回 dict、不抛" % bad, False,
            "%s: %s" % (type(e).__name__, e))

# ── ② 对照 + 接线 ───────────────────────────────────────────────
# 对照：裸 urlparse 对同一个输入确实会抛。没有这一条，上面那组断言可能只是空转
# （比如哪天 urlparse 改了行为，这门禁就悄悄变成「永远绿」）。
try:
    urllib.parse.urlparse("http://[::1")
    chk("对照：裸 urlparse 对半个方括号确实会抛（这门禁不是空转）", False, "居然没抛")
except ValueError as e:
    chk("对照：裸 urlparse 对半个方括号确实会抛（这门禁不是空转）", True, str(e))

# 接线：normalize 里得有这道收口，且它包住的是 urlparse + hostname 那两行。
NORM = SOURCE[SOURCE.index("def normalize(raw):"):SOURCE.index("class _RedirectGuard")]
chk("normalize 里有 except ValueError 的收口", "except ValueError:" in NORM)
chk("收口翻成的是 Blocked（不是别的异常）",
    "raise Blocked" in NORM[NORM.index("except ValueError:"):])

# 跳转守卫：解不出来的 Location 也只翻成 HTTPError，不许把 ValueError 冒给 opener。
guard = wb._RedirectGuard()
try:
    guard.redirect_request(None, None, 302, "Found", {}, "http://[::1")
    chk("跳转守卫对半个方括号回 HTTPError（Blocked 那一路）", False, "居然放行了")
except urllib.error.HTTPError as e:
    chk("跳转守卫对半个方括号回 HTTPError（Blocked 那一路）", True, str(e))
except Exception as e:                           # noqa: BLE001
    chk("跳转守卫对半个方括号回 HTTPError（Blocked 那一路）", False,
        "%s: %s" % (type(e).__name__, e))

# 收口没把尺子改松：正常地址照走，内网照拒。
chk("正常地址照旧补 https", wb.normalize("ai-bot.cn") == "https://ai-bot.cn")
chk("正常 http 地址原样留着",
    wb.normalize("http://example.com/x") == "http://example.com/x")
for host in ("http://127.0.0.1/x", "http://192.168.1.1/admin"):
    try:
        wb.normalize(host)
        chk("内网地址照旧拒 %s" % host, False, "放行了")
    except wb.Blocked as e:
        chk("内网地址照旧拒 %s" % host, bool(str(e)), str(e))

# ── ③ 真机：in-process 起一个真 Handler，裸 HTTP GET /api/browse ──
srv = ThreadingHTTPServer(("127.0.0.1", 0), ui_server.Handler)
PORT = srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start()
time.sleep(0.1)


def raw_get(target, timeout=5.0):
    """发一条 GET，回 (响应字节, 是否读超时)。Connection: close 让服务端回完就关。"""
    req = (b"GET " + target.encode("latin-1") + b" HTTP/1.1\r\n"
           b"Host: 127.0.0.1\r\n"
           b"Connection: close\r\n\r\n")
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


for target, label in [
    ("/api/browse?url=" + urllib.parse.quote("http://[::1", safe=""), "http://[::1"),
    ("/api/browse?url=" + urllib.parse.quote("https://[", safe=""), "https://["),
]:
    data, hung = raw_get(target)
    chk("真机 %s：回 200 + 一页人话（原来是一条空响应、连接被掐）" % label,
        not hung and data and "200" in status_of(data), (hung, status_of(data)))
    chk("真机 %s：那一页是「这一页没取回来」" % label,
        "这一页没取回来" in data.decode("utf-8", "replace"), data[:200])
    head = data.split(b"\r\n\r\n", 1)[0]
    chk("真机 %s：响应本身是完整的（带 Content-Length）" % label,
        b"content-length:" in head.lower(), head[:160])

# 内网地址这条老路照旧：也回一页人话，不发任何网络请求。
data, hung = raw_get("/api/browse?url=" + urllib.parse.quote("http://192.168.1.1/x", safe=""))
chk("真机内网地址：照旧回 200 + 一页人话",
    not hung and "200" in status_of(data) and b"<html" in data.lower(),
    (hung, status_of(data)))

srv.shutdown()
srv.server_close()

# ── 汇总 ────────────────────────────────────────────────────────
bad = [n for ok, n, _ in checks if not ok]
print()
print("内置浏览器地址收口体检：通过 %d 项，失败 %d 项" % (len(checks) - len(bad), len(bad)))
for n in bad:
    print("  ✗ " + n)
sys.exit(1 if bad else 0)
