# -*- coding: utf-8 -*-
"""跳转那道闸：站点把我们 302 到内网，也不算替用户敲开了门。

上一条（check_url_guard.py）管的是「用户粘进来 / 页面里写着的那个地址」，以及内置浏览器
那一跳。这一条管的是另一半：**我们已经发出去之后，对方回的 `Location`**。默认的
`urllib.request.urlopen` 会自己跟着 `Location` 走，而且**不会再问一次**新地址能不能去。
实测：一个放行的站点 302 到 `http://127.0.0.2/`，`clip_article.fetch` 就把内网那一页
当正文取回来了（内网服务真的被敲到，`internal hits = 1`）。剪藏 / 平台解析 / 订阅 /
插图下载 / 封面补取这五条取页路原先都是裸 urlopen，五条都通这条边。

这里全程离线：起两个本机临时 http.server（一个当「外面」的、一个当「内网」的），
只把「外面」那台的地址当放行，然后看内网那台**到底被敲了没有**。另用单元断言钉住
换身写法的跳转目标（纯数字 IP / IPv6 映射 / 非 http scheme），以及「公网跳转照走」——
这道闸是「换个不许去的地方才拦」，不是「一律不许跳」。

判据只有一处：clip_article.url_block_reason（fetch 的前置检查、safe_image_url、
README 里的地址护栏说的都是它），跳转闸只是把同一处判据用在每一跳上。
"""
import http.server
import os
import pathlib
import sys
import tempfile
import threading
import urllib.error
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# 数据目录先进沙盒再 import：这些模块在 import 期就算导出位置，不能指到用户的真书库。
SANDBOX = tempfile.mkdtemp(prefix="guizang-redirect-")
os.environ["GUIZANG_DATA"] = os.path.join(SANDBOX, "cache")
os.environ["GUIZANG_BOOKS"] = os.path.join(SANDBOX, "books")

import clip_article      # noqa: E402
import download_images   # noqa: E402
import feed              # noqa: E402
import web_parse         # noqa: E402

FAIL = []
PASSED = 0


def chk(name, cond, extra=""):
    global PASSED
    if cond:
        PASSED += 1
    else:
        FAIL.append(name)
    print(("PASS  " if cond else "FAIL  ") + name + ("" if cond else "   " + repr(extra)))


# ── A. 跳转目标本身：换身写法也要认出来 ─────────────────────────────
guard = clip_article.RedirectGuard()
base_req = urllib.request.Request("https://example.com/page")

BAD_TARGETS = [
    ("http://2130706433/", "纯数字回环"),
    ("http://0x7f000001/", "十六进制回环"),
    ("http://[::ffff:127.0.0.1]/", "IPv4 映射的 IPv6 回环"),
    ("http://192.168.1.1/admin", "家用内网"),
    ("http://169.254.169.254/latest/meta-data/", "云元数据"),
    ("http://localhost:8080/", "localhost 名字"),
    ("file:///etc/passwd", "非 http 协议"),
    ("ftp://example.com/x", "非 http 协议（ftp）"),
    ("http:///no-host", "没有主机名"),
]
for bad, why in BAD_TARGETS:
    try:
        guard.redirect_request(base_req, None, 302, "Found", {}, bad)
        chk("跳转闸拦下 %s（%s）" % (bad, why), False, "居然跟着跳了")
    except urllib.error.HTTPError as e:
        chk("跳转闸拦下 %s（%s）" % (bad, why), "跳转被拦下" in str(e), str(e)[:60])

# 这道闸不是「一律不许跳」：公网之间的跳转要照常走。
ok = guard.redirect_request(base_req, None, 302, "Found", {},
                            "https://cdn.example.org/a/b?c=1")
chk("公网跳转照走（不是一律拒绝）",
    isinstance(ok, urllib.request.Request) and ok.full_url == "https://cdn.example.org/a/b?c=1",
    getattr(ok, "full_url", ok))

# 同一处判据：safe_image_url 与 fetch 的前置检查是同一把尺子，没有各留一份。
for u in ("http://127.0.0.1/a.jpg", "file:///a.jpg", "https://img.example.com/a.jpg"):
    chk("safe_image_url 与 url_allowed 同口径：%s" % u,
        clip_article.safe_image_url(u) == clip_article.url_allowed(u), u)
chk("半个 IPv6 方括号不抛异常，只当拒绝",
    clip_article.safe_image_url("http://[::1/a.jpg") is False)


# ── B. 五条取页路都先装上这道闸（假 urlopen，不联网） ────────────────
class _Hdrs:
    def get_content_charset(self):
        return "utf-8"

    def items(self):
        return []


class _Resp:
    def __init__(self, blob, url):
        self._blob = blob
        self._url = url
        self.headers = _Hdrs()

    def read(self, n=-1):
        return self._blob

    def geturl(self):
        return self._url

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


_real_urlopen = urllib.request.urlopen
_real_ensure = clip_article.ensure_redirect_guard
wired = []


_BLOB = b"<html><body>" + b"ok" * 400 + b"</body></html>"   # 够过 download_one 的 500 字节线


def _fake_urlopen(req, timeout=None, **kw):
    url = req.full_url if hasattr(req, "full_url") else str(req)
    return _Resp(_BLOB, url)


clip_article.ensure_redirect_guard = lambda: wired.append(1)
urllib.request.urlopen = _fake_urlopen
try:
    wired.clear()
    raw, final = clip_article.fetch("https://example.com/post")
    chk("剪藏 fetch 先装跳转闸", wired and "ok" in raw, (wired, raw[:30]))

    wired.clear()
    txt, jfinal = web_parse.fetch_json("https://example.com/syndication.json")
    chk("平台解析 fetch_json 先装跳转闸", wired and "ok" in txt, (wired, txt[:30]))

    wired.clear()
    res = download_images.download_one("https://img.example.com/a.jpg",
                                      os.path.join(SANDBOX, "a.jpg"), retries=1)
    chk("插图下载 download_one 先装跳转闸", wired and res == "ok", (wired, res))

    wired.clear()
    blob, ffinal, hdrs = feed._http("https://example.com/feed.xml")
    chk("订阅 _http 先装跳转闸", wired and b"ok" in blob, (wired, blob[:30]))
finally:
    urllib.request.urlopen = _real_urlopen
    clip_article.ensure_redirect_guard = _real_ensure


# ── C. 真发一次：内网那台到底被敲了没有 ────────────────────────────
HITS = {"outside": 0, "inside": 0}
SECRET = b"<html><body>internal secret</body></html>"


class _Outside(http.server.BaseHTTPRequestHandler):
    """「外面」那台：/next 回正文，其余一律 302 到 target。"""

    target = ""

    def do_GET(self):
        HITS["outside"] += 1
        if self.path == "/next":
            body = b"<html><body>public page</body></html>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(302)
        self.send_header("Location", type(self).target)
        self.end_headers()

    def log_message(self, *a):
        pass


class _Inside(http.server.BaseHTTPRequestHandler):
    """「内网」那台：只要被敲到就记一笔，并回一份不该流出去的东西。"""

    def do_GET(self):
        HITS["inside"] += 1
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(SECRET)))
        self.end_headers()
        self.wfile.write(SECRET)

    def log_message(self, *a):
        pass


def _serve(handler_cls, host):
    srv = http.server.ThreadingHTTPServer((host, 0), handler_cls)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


outside = _serve(_Outside, "127.0.0.1")
outside_host, outside_port = outside.server_address[0], outside.server_address[1]

inside_srv = None
for _h in ("127.0.0.2", "::1"):
    try:
        inside_srv = _serve(_Inside, _h)
        inside_host = _h
        break
    except OSError:
        continue
if inside_srv is None:
    chk("找得到一台「内网」临时服务用于验证", False, "127.0.0.2 与 ::1 都绑不上")
    inside_host, inside_port = "127.0.0.2", 1
else:
    inside_port = inside_srv.server_address[1]

# 只把「外面」那台当放行：其余（含内网那台与所有换身写法）都交给真的 host_blocked。
_real_blocked = clip_article.host_blocked
clip_article.host_blocked = lambda h: (h or "").strip() != "127.0.0.1"

PUB = "http://127.0.0.1:%d/start" % outside_port
INSIDE = "http://%s:%d/secret" % (inside_host, inside_port)

# C1. 302 到内网：必须拒，且内网那台一次都没被敲。
_Outside.target = INSIDE
try:
    raw, final = clip_article.fetch(PUB)
    chk("剪藏 fetch 拒绝 302 到内网", False, "居然取回了：%r" % raw[:60])
except ValueError as e:
    chk("剪藏 fetch 拒绝 302 到内网（%s）" % str(e)[:50], "跳转被拦下" in str(e), str(e)[:80])
chk("内网那台一次都没被敲（剪藏）", HITS["inside"] == 0, HITS)

# C2. 同一个目标交给插图下载：也只留一句失败，内网照样没被敲。
_Outside.target = INSIDE
res = download_images.download_one(PUB, os.path.join(SANDBOX, "b.jpg"), retries=1)
chk("插图下载拒绝 302 到内网（%s）" % res[:40], res.startswith("fail:"), res)
chk("内网那台一次都没被敲（插图下载）", HITS["inside"] == 0, HITS)

# C3. 放行的跳转照走：相对 Location 解析回同一台，正文与最终地址都对。
_Outside.target = "/next"
try:
    raw, final = clip_article.fetch(PUB)
    chk("公网 / 同站跳转照常取回",
        "public page" in raw and final.endswith("/next"), (final, raw[:40]))
except Exception as e:
    chk("公网 / 同站跳转照常取回", False, "%s: %s" % (type(e).__name__, e))
chk("外面那台确实被访问了（不是什么都没发生）", HITS["outside"] >= 3, HITS)

# 「只装一次」：重复调用不抛、也不把默认 opener 换没。
inside_handlers = []
try:
    clip_article.ensure_redirect_guard()
    clip_article.ensure_redirect_guard()
    opener = getattr(urllib.request, "_opener", None)
    inside_handlers = [type(h).__name__ for h in getattr(opener, "handlers", [])]
except Exception as e:
    chk("ensure_redirect_guard 幂等可重复调用", False, e)
chk("默认 opener 上装着的就是这道闸（幂等）",
    "RedirectGuard" in inside_handlers, inside_handlers)

clip_article.host_blocked = _real_blocked

print("\n跳转护栏体检：%d 项通过，失败 %d 项" % (PASSED, len(FAIL)))
for name in FAIL:
    print("  ✗ " + name)
sys.exit(1 if FAIL else 0)
