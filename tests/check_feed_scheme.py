#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""归藏门禁：订阅取回的地址也要过 scheme 闸，file:// 不许替用户读本机文件 (feed.py)。

为什么要单开这一条
    `feed.py` 这条线的地址有三处来源，只有第一处核过 scheme：

      · 用户当场粘进来的 —— `discover()` 开头那句 `_check_url(url)` 挡得住；
      · **页面内容里写的** —— `<link rel="alternate" type="application/rss+xml"
        href="…">` 与响应头 `Link:` 里那几个候选，`_link_rel_alternate()` /
        `_link_header()` 只做 `urljoin` 补全，补出来是什么 scheme 就是什么；
      · **`feed.json` 里存着的那一份** —— 用户能手改、云同步也可能落半截。

    后两处都直接喂给了 `urlopen`，而 urllib 默认认得 `file:`：

        urllib.request.urlopen("file:///Users/…/.ssh/id_rsa")

    会把本机文件原样读回来。于是「订阅发现」这一步只要访问一个
    `href="file:///Users/…/cache/config.json"` 的页面，`add()` 就会把它读进来当订阅
    解析（能解析成 feed 就整篇进书架）。剪藏、平台解析、内置浏览器、订阅抓页这四条
    取页路里，只有「订阅按候选地址取回」这一条漏了 scheme 核验。

这一条钉住
  · 取回口 `_http()` 自己核一遍 scheme / 主机名（不是只靠调用方自觉），
    `_fetch_bytes()` 与 `discover()` 的探测路都从它走；
  · `file:` / `data:` / `ftp:` / `gopher:` / `javascript:` / 没 scheme 的地址一律拒绝，
    回的是中文人话，且**一次请求都不发**；
  · 页面里的 `file://` 候选不再进「发现结果」，`add()` 那句「没找到订阅源」照旧；
  · `feed.json` 里存着 `file://` 的源，刷新只在它自己那行记一句人话错误，不抛穿、
    不把本机文件读成条目，同一次刷新里的正常 http 源照刷；
  · 对照：裸 `urlopen("file://…")` 确实读得回本机文件 —— 这道闸不是空转，是真堵了一个洞。

全程离线：夹具是一个跑在 127.0.0.1 上的临时 http.server，本机文件指向系统临时目录里的
一个假密钥；`GUIZANG_DATA` / `GUIZANG_BOOKS` 都指进沙盒，不碰真实 cache/ 与书库，
不联网、不开浏览器，故归 --fast 静态段。
"""
import atexit
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

# GUIZANG_DATA 必须在 import feed 之前设好：feed.py 导入时就把 DATA_DIR / STORE 定死了。
SANDBOX = tempfile.mkdtemp(prefix="gz-feed-scheme-")
atexit.register(lambda: shutil.rmtree(SANDBOX, ignore_errors=True))
os.environ["GUIZANG_DATA"] = os.path.join(SANDBOX, "data")
os.environ["GUIZANG_BOOKS"] = os.path.join(SANDBOX, "books")
os.makedirs(os.environ["GUIZANG_DATA"], exist_ok=True)
os.makedirs(os.environ["GUIZANG_BOOKS"], exist_ok=True)

import feed  # noqa: E402

FAIL = []
PASSED = 0


def chk(name, cond, extra=""):
    global PASSED
    if cond:
        PASSED += 1
        print("  ok  " + name)
    else:
        FAIL.append(name)
        print("FAIL  " + name + ("" if not extra else "   <- " + str(extra)[:220]))


def raises_value(fn, *args, **kw):
    """跑一下，只认 ValueError（模块里给人话报错都是这个类型）；别的异常算不合格。"""
    try:
        fn(*args, **kw)
    except ValueError:
        return True
    except Exception as e:  # noqa: BLE001
        print("       抛错了类型：%r" % (e,))
        return False
    return False


# ── 沙盒里的「本机私密文件」+ 一个临时站点 ─────────────────────────────
MARK = "SECRET-MARKER-8f3a1c"
SECRET_PATH = os.path.join(SANDBOX, "id_secret.xml")
with open(SECRET_PATH, "w", encoding="utf-8") as f:
    f.write('<?xml version="1.0" encoding="utf-8"?>\n'
            '<rss version="2.0"><channel><title>%s</title>'
            '<item><title>%s 的正文</title></item></channel></rss>' % (MARK, MARK))
SECRET_URL = "file://" + SECRET_PATH

RECORD = {"paths": []}
PORT = 0
BASE = ""


def _rss(title):
    return ('<?xml version="1.0" encoding="utf-8"?>\n'
            '<rss version="2.0"><channel><title>%s</title>'
            '<item><title>%s 的一条</title><link>%s/item1</link></item>'
            '</channel></rss>' % (title, title, BASE))


class Fixture(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, ctype, body):
        blob = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(blob)))
        self.end_headers()
        self.wfile.write(blob)

    def do_GET(self):
        RECORD["paths"].append(self.path)
        if self.path == "/evil":
            # 页面里把一个 file:// 的「订阅源」写进来：href 是攻击者能写的内容
            self._send("text/html; charset=utf-8",
                       '<html><head><link rel="alternate" type="application/rss+xml" '
                       'href="%s"></head><body>一个博客页</body></html>' % SECRET_URL)
        elif self.path == "/mixed":
            # 坏的那个排在前面：过滤掉之后好的那个必须还在
            self._send("text/html; charset=utf-8",
                       '<html><head>'
                       '<link rel="alternate" type="application/rss+xml" href="%s">'
                       '<link rel="alternate" type="application/rss+xml" href="/good.xml">'
                       '</head><body>x</body></html>' % SECRET_URL)
        elif self.path == "/good-page":
            self._send("text/html; charset=utf-8",
                       '<html><head><link rel="alternate" type="application/rss+xml" '
                       'href="/good.xml" title="正经订阅"></head><body>x</body></html>')
        elif self.path == "/good.xml":
            self._send("application/rss+xml; charset=utf-8", _rss("正经订阅"))
        elif self.path == "/other.xml":
            self._send("application/rss+xml; charset=utf-8", _rss("另一个源"))
        else:
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()


def _serve():
    global PORT, BASE
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Fixture)
    PORT = srv.server_address[1]
    BASE = "http://127.0.0.1:%d" % PORT
    srv.serve_forever()


threading.Thread(target=_serve, daemon=True).start()
for _ in range(80):
    if PORT:
        break
    time.sleep(0.05)


def store_raw():
    """直接读盘上的 feed.json：验「真的写下去了」，不能只信函数返回值。"""
    try:
        with open(feed.STORE, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {"feeds": [], "entries": {}, "seen": {}}


def store_text():
    try:
        with open(feed.STORE, encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        return ""


def all_entries():
    raw = store_raw()
    return [e for lst in raw.get("entries", {}).values() for e in lst or []]


# ── 1. 对照：file:// 是真能读本机文件的（这道闸堵的就是它） ─────────────
try:
    got = urllib.request.urlopen(SECRET_URL).read(4096)
    control_reads = MARK.encode("utf-8") in got
except Exception as e:  # noqa: BLE001
    control_reads = False
    print("       对照读本机文件失败：%r" % (e,))
chk("J1 对照：裸 urlopen(\"file://…\") 确实读得回本机文件（老取回口就是这么读的）",
    control_reads)

SRC = (REPO / "feed.py").read_text(encoding="utf-8")
_seg_start = SRC.find("def _http(")
_seg = SRC[_seg_start:SRC.find("def _fetch_bytes(", _seg_start)]
chk("J2 唯一那条取回口 _http 自己核 scheme/host（不是只靠调用方自觉）",
    "_check_url(url)" in _seg, _seg[:200])
chk("J3 只有一条取回口：_fetch_bytes 走 _http（改一处就盖住全部调用方）",
    "_http(url, headers)" in SRC)

# ── 2. _check_url：名单 / 主机名 / 解不出来的地址 ───────────────────────
chk("U1 http / https 照常放行（含端口与路径）",
    feed._check_url("http://example.com/feed.xml") == "http://example.com/feed.xml"
    and feed._check_url("  https://example.com:8443/a?b=1  ") == "https://example.com:8443/a?b=1")
chk("U2 本机 http 地址照常放行（门禁夹具自己就长这样）",
    feed._check_url(BASE + "/good.xml") == BASE + "/good.xml")

for bad, label in ((SECRET_URL, "file:"), ("data:text/plain,hi", "data:"),
                   ("ftp://example.com/x", "ftp:"), ("gopher://example.com/x", "gopher:"),
                   ("javascript:alert(1)", "javascript:"), ("example.com/feed", "没 scheme"),
                   ("https://", "没主机名")):
    try:
        feed._check_url(bad)
        chk("U3 「%s」被拒（%s）" % (bad[:28], label), False, "居然放行了")
    except ValueError as e:
        msg = str(e)
        chk("U3 「%s」被拒、回中文人话（%s）" % (bad[:28], label),
            bool(msg.strip()) and "Invalid" not in msg and "Traceback" not in msg
            and "Error" not in msg, msg)

try:
    feed._check_url("http://[::1")
    chk("U4 半截 IPv6 方括号被拒", False)
except ValueError as e:
    chk("U4 半截 IPv6 方括号给人话（不是英文 Invalid IPv6 URL）",
        "Invalid IPv6" not in str(e) and "解不出来" in str(e), str(e))

for weird in (123, ["http://x"], {"url": "http://x"}, b"http://x"):
    chk("U5 非字符串地址当写坏了、不抛 AttributeError / TypeError（%r）" % (weird,),
        raises_value(feed._check_url, weird))

# ── 3. 取回口：挡下的地址一次请求都不发 ────────────────────────────────
BLOCKED = [SECRET_URL, "data:text/plain,hi", "ftp://example.com/x",
           "gopher://example.com/x", "javascript:alert(1)", "example.com/feed",
           "http://[::1"]
_real_urlopen = urllib.request.urlopen
CALLS = []


def _accounting_urlopen(req, *a, **k):
    CALLS.append(getattr(req, "full_url", str(req)))
    raise AssertionError("被挡下的地址不该真的去取")


urllib.request.urlopen = _accounting_urlopen
try:
    for u in BLOCKED:
        ok = raises_value(feed._http, u)
        chk("C1 _http 拒绝并给人话：%s" % u[:30], ok)
        ok = raises_value(feed._fetch_bytes, u)
        chk("C2 _fetch_bytes 同样拒绝：%s" % u[:30], ok)
    chk("C3 被挡下的地址一次请求都没发（网都没上）", CALLS == [], CALLS)
finally:
    urllib.request.urlopen = _real_urlopen

# ── 4. 发现：页面里写的 file:// 不算候选，好的候选照旧 ─────────────────
found = feed.discover(BASE + "/evil")
chk("D1 页面里唯一的候选是 file:// 时，发现结果为空",
    found == [], found)
chk("D2 发现过程只打了那个页面（没去读本机文件）",
    RECORD["paths"].count("/evil") >= 1, RECORD["paths"][-6:])

found2 = feed.discover(BASE + "/mixed")
urls2 = [c.get("url") for c in found2]
chk("D3 坏候选被滤掉、同页里的 http 候选照旧发现",
    urls2 == [BASE + "/good.xml"], urls2)

found3 = feed.discover(BASE + "/good-page")
chk("D4 正常页面照旧发现（闸不是一刀切拒绝）",
    [c.get("url") for c in found3] == [BASE + "/good.xml"], found3)

# ── 5. 真产品路径：add() 不会把本机文件订成源 ──────────────────────────
chk("A1 add() 面对只有 file:// 候选的页面报「没找到订阅源」",
    raises_value(feed.add, BASE + "/evil"))
chk("A2 本机文件的内容一个字都没进 feed.json",
    MARK not in store_text())
chk("A3 账本里一个源都没多出来", store_raw().get("feeds") in ([], None), store_raw().get("feeds"))

ok = feed.add(BASE + "/good-page")
raw = store_raw()
chk("A4 正常 http 源照旧订得上、条目也进得来",
    len(raw.get("feeds") or []) == 1 and len(all_entries()) >= 1,
    (raw.get("feeds"), len(all_entries())))

# ── 6. 真产品路径：feed.json 里存着 file:// 的源，刷新只让它自己报错 ────
dirty = {
    "feeds": [
        {"id": "f-bad", "url": SECRET_URL, "title": "手改出来的坏源", "group": "", "order": 0},
        {"id": "f-good", "url": BASE + "/other.xml", "title": "同一次里的好源",
         "group": "", "order": 1},
    ],
    "entries": {}, "seen": {}, "settings": {},
}
with open(feed.STORE, "w", encoding="utf-8") as f:
    json.dump(dirty, f, ensure_ascii=False)

try:
    res = feed.refresh(force=True)
    threw = None
except Exception as e:  # noqa: BLE001
    res, threw = None, e

chk("R1 刷新不抛穿（一个坏源不该带走整趟刷新）", threw is None, repr(threw))
errs = {e.get("feed_id"): e.get("error") or "" for e in (res or {}).get("errors", [])}
chk("R2 坏源那一行记的是中文人话、点出 http/https",
    "f-bad" in errs and "http/https" in errs.get("f-bad", ""), errs)
after = store_raw()
bad_entries = after.get("entries", {}).get("f-bad") or []
chk("R3 坏源一条条目都没读进来（本机文件没被当订阅解析）",
    bad_entries == [], bad_entries)
chk("R4 本机文件内容仍然一个字都没落盘", MARK not in store_text())
good_entries = after.get("entries", {}).get("f-good") or []
chk("R5 同一次刷新里的正常 http 源照旧刷到条目",
    len(good_entries) >= 1, len(good_entries))
chk("R6 坏源的地址没被改写（只拒取、不改用户账本）",
    any(f.get("id") == "f-bad" and f.get("url") == SECRET_URL for f in after.get("feeds") or []),
    after.get("feeds"))

print("\n订阅取回地址 scheme 门禁：%d 项通过，失败 %d 项" % (PASSED, len(FAIL)))
for name in FAIL:
    print("  ✗ " + name)
sys.exit(1 if FAIL else 0)
