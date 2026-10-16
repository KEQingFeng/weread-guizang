#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""内网地址闸门·兼容字符那一档：`1²7.0.0.1` 这类「字面不是 IP、连的是回环」的写法。

为什么要单开一条
    `clip_article.host_blocked` 是全仓唯一那把「本机 / 内网地址一律拒绝」的尺子，
    剪藏取页、内置浏览器、平台解析、首图与封面兜底全走它。上一档补齐的是
    **百分号编码**（`%31%32%37.0.0.1`）：`urlparse().hostname` 不解码，而 urllib 会
    `unquote(self.host)`，尺子看的是一串、连的是另一串。

    这一条堵的是同一句话的第二档——**IDNA / nameprep 的相容字符映射**。CPython 的
    `socket.getaddrinfo` 收到非 ASCII 主机名时会先走 `str.encode("idna")`：那一步做
    RFC 3491 的 nameprep（NFKC + 大小写折叠 + 映射），把相容字符折回基本字符。于是

        1²7.0.0.1     （上标 2，U+00B2）  → getaddrinfo 连 127.0.0.1
        lºcalhost     （阳性序数 º，U+00BA）→ getaddrinfo 连 localhost
        127。0。0。1  （表意句号，U+3002）→ 同上
        ①②⑦.0.0.1   （带圈数字）        → 同上

    这几个串在字面量上既不是一个 IP、也匹配不上 `127.` / `localhost` 前缀，于是
    旧的 `host_blocked` 一路放行。**而且这一档真的够得着**：折出来的主机名是
    latin-1 能编码的，urllib 那道 Host 头的编码不会拦它（`¹²⁷` 那种 latin-1 编不出
    的才会被 urllib 顺手拦住），所以 `http://1²7.0.0.1:8770/api/state` 会真的连到
    127.0.0.1、回包还被当成正文收下——本条门禁起一个进程内回环假服务实测过。

修法与这一族其它档同一处
    在 `host_blocked` 里先按 `socket.getaddrinfo` 那一步做一次 IDNA 归一
    （`_idna_host`），再拿原样与本名各判一遍字面 / IP。一处改，四条路一起管住
    （`url_block_reason` / `web_browse.normalize` / `web_parse._guard` / `safe_image_url`）。

这一条钉六面，全离线（只碰纯函数与回环假服务，不联网、不开浏览器、不碰真实
cache/ 与书库），归 --fast 静态段：
    ① 对照：证明这一档是真洞 —— 字面判拒不了、`getaddrinfo` 却连回环、裸 `urlopen`
       实测把内网回包取回来了；
    ② 纯函数：一族的换身写法逐条拒；
    ③ 公网（含正常的国际化域名）一个不许误伤；
    ④ 认不出的输入只回布尔、绝不抛（延续 check_url_guard_neg 的契约）；
    ⑤ 四条路接线都走这一处；
    ⑥ 真机：`clip_article.fetch` / `web_browse.browse` 对换身地址必须拒且一次都没打中。
"""
import http.server
import ipaddress
import os
import pathlib
import socket
import sys
import tempfile
import threading
import urllib.parse
import urllib.request

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

ROOT = tempfile.mkdtemp(prefix="guizang-hostidna-")
os.environ["GUIZANG_DATA"] = os.path.join(ROOT, "cache")
os.environ["GUIZANG_BOOKS"] = os.path.join(ROOT, "books")

import clip_article                                            # noqa: E402
import web_browse                                              # noqa: E402
import web_parse                                               # noqa: E402

FAIL = []
PASSED = 0


def chk(name, cond, extra=""):
    global PASSED
    print(("PASS  " if cond else "FAIL  ") + name + ("" if cond else "   " + repr(extra)[:220]))
    if cond:
        PASSED += 1
    else:
        FAIL.append(name)


# ── ① 对照：这一档是真洞，不是想出来的 ─────────────────────────────────
# 三个串各有代表性：上标（latin-1 能编码，urllib 拦不住）、表意句号 / 带圈数字
# （latin-1 编不出，urllib 顺手会拦），都要由护栏本身拒掉，不能指望编码层。
EVIL = "1²7.0.0.1"

chk("机制：getaddrinfo('1²7.0.0.1') 解出来就是 127.0.0.1",
    {i[4][0] for i in socket.getaddrinfo(EVIL, 80, proto=socket.IPPROTO_TCP)}
    == {"127.0.0.1"},
    socket.getaddrinfo(EVIL, 80, proto=socket.IPPROTO_TCP))
chk("机制：'1²7.0.0.1' 是 latin-1 能编码的（urllib 那道 Host 头拦不住它）",
    EVIL.encode("latin-1") == b"1\xb27.0.0.1")
try:
    "¹²⁷.0.0.1".encode("latin-1")
    chk("对照：'¹²⁷.0.0.1' latin-1 编不出（所以它本来就被 urllib 顺手挡住）", False,
        "居然编得出")
except UnicodeEncodeError:
    chk("对照：'¹²⁷.0.0.1' latin-1 编不出（所以它本来就被 urllib 顺手挡住）", True)

# 旧尺子为什么漏：字面量既不是内网字面量、也解析不出 IP、老式 IPv4 也不认。
chk("对照：'1²7.0.0.1' 匹配不上旧的名字正则（旧尺子漏在这）",
    clip_article.BLOCKED_HOSTS.match(EVIL) is None, EVIL)
try:
    ipaddress.ip_address(EVIL)
    chk("对照：'1²7.0.0.1' 不是 ipaddress 认的 IP", False, "居然认")
except ValueError:
    chk("对照：'1²7.0.0.1' 不是 ipaddress 认的 IP", True)
chk("对照：'1²7.0.0.1' 也不是老式 IPv4 写法（旧尺子这条路也不认）",
    clip_article._legacy_ipv4(EVIL) is None, clip_article._legacy_ipv4(EVIL))
# 归一是修法本身：本名才是 socket 会连的那个名字。
chk("修法：_idna_host('1²7.0.0.1') 归一成 127.0.0.1",
    clip_article._idna_host(EVIL) == "127.0.0.1", clip_article._idna_host(EVIL))


# ── 一个进程内的回环假服务：证明「真会被敲到」与「现在敲不到」────────────
class _Handler(http.server.BaseHTTPRequestHandler):
    hits = []

    def do_GET(self):
        _Handler.hits.append(self.path)
        body = b"<html><body>INTERNAL-SECRET-MARKER</body></html>"
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


_SRV = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
_PORT = _SRV.server_address[1]
threading.Thread(target=_SRV.serve_forever, daemon=True).start()

try:
    # 空闲对照：服务是真活的，走常规回环地址取得到东西。
    _Handler.hits.clear()
    with urllib.request.urlopen("http://127.0.0.1:%d/ok" % _PORT, timeout=5) as r:
        alive = b"INTERNAL-SECRET-MARKER" in r.read()
    chk("对照：回环假服务活着（常规地址取得到东西）", alive and _Handler.hits == ["/ok"],
        _Handler.hits)

    # 真洞的对照：换身地址走**裸 urlopen**（没有护栏）确实被敲中。
    _Handler.hits.clear()
    with urllib.request.urlopen("http://%s:%d/secret" % (EVIL, _PORT), timeout=5) as r:
        leaked = b"INTERNAL-SECRET-MARKER" in r.read()
    chk("对照：裸 urlopen 用 '1²7.0.0.1' 真把内网回包取回来了（洞是真的）",
        leaked and _Handler.hits == ["/secret"], _Handler.hits)

    # ── ⑥ 真机：两条产品路对换身地址都必须拒、且一次都没打中 ─────────────
    _Handler.hits.clear()
    try:
        clip_article.fetch("http://%s:%d/secret" % (EVIL, _PORT))
        chk("clip_article.fetch 拒换身回环地址并给人话", False, "居然取回来了")
    except ValueError as e:
        chk("clip_article.fetch 拒换身回环地址并给人话（不是英文运行期错误）",
            "本机或内网" in str(e) and "Invalid" not in str(e), str(e))
    chk("clip_article.fetch：服务一次都没被打中（不是先连再拒）",
        _Handler.hits == [], _Handler.hits)

    _Handler.hits.clear()
    got = web_browse.browse("http://%s:%d/secret" % (EVIL, _PORT))
    chk("web_browse.browse 对换身回环地址回 ok=False + 人话",
        isinstance(got, dict) and got.get("ok") is False
        and "本机或内网" in (got.get("msg") or ""), got)
    chk("web_browse.browse：服务一次都没被打中", _Handler.hits == [], _Handler.hits)
finally:
    _SRV.shutdown()
    _SRV.server_close()


# ── ② 纯函数：一族的换身写法逐条拒 ─────────────────────────────────────
# 每条括号里是 getaddrinfo 真正会连到的地址（由 `_idna_host` 归一得出，见上面）。
IDNA_INTERNAL = [
    "1²7.0.0.1",                # 上标 2 → 127.0.0.1
    "1²7.0.0.1.",               # 同上，带结尾点
    "127.0.0.¹",                # 尾段上标 1
    "¹²⁷.0.0.1",                # 表头三段全是上标
    "1⁰.0.0.1",                 # 上标 0 → 10.0.0.1
    "1⁹2.168.1.1",              # 上标 9 → 192.168.1.1
    "¹⁶9.254.169.254",          # 上标 6 → 169.254.169.254（云元数据）
    "¹⁷2.16.0.1",               # 上标 7 → 172.16.0.1
    "lºcalhost",                # 阳性序数 º → localhost
    "127。0。0。1",              # 表意句号 U+3002
    "127．0．0．1",              # 全角句号 U+FF0E
    "127｡0｡0｡1",               # 半角片假名句号 U+FF61
    "①②⑦.0.0.1",               # 带圈数字
    "%31%C2%B2%37.0.0.1",       # 百分号编码 + 上标叠在一起
]
for h in IDNA_INTERNAL:
    chk("换身内网地址一律拒：%s（%s）" % (h, clip_article._idna_host(urllib.parse.unquote(h))),
        clip_article.host_blocked(h) is True, clip_article.host_blocked(h))

# 修法只收紧、不放宽：前一档（百分号编码）与再前一档（老式 IP）照旧拒。
for h in ("%31%32%37.0.0.1", "2130706433", "0x7f000001", "0177.0.0.1",
          "127.1", "localhost.", "::1", "[::ffff:127.0.0.1]"):
    chk("既有档照旧拒：%s" % h, clip_article.host_blocked(h) is True,
        clip_article.host_blocked(h))


# ── ③ 公网一个不许误伤（含正常的国际化域名）──────────────────────────
PUBLIC = ["example.com", "www.youtube.com", "weread.qq.com", "cdn.example.org",
          "1.1.1.1", "8.8.8.8", "11.0.0.1", "172.15.0.1", "172.32.0.1", "a.b.c.d",
          "例子.测试", "münchen.de", "xn--fsqu00a.xn--0zwm56d", "%65xample.com"]
for h in PUBLIC:
    chk("公网照旧放行：%s" % h, clip_article.host_blocked(h) is False,
        clip_article.host_blocked(h))


# ── ④ 认不出的输入只回布尔、绝不抛（延续反面输入那条契约）────────────
WEIRD = [None, "", "   ", "-", "--1", "1_0.0.0.1", "9" * 5000, "127..0.1",
         "lº" * 200, "%", "%zz", "。[::1]", "\x00localhost", "１２７"]
for h in WEIRD:
    try:
        got = clip_article.host_blocked(h)
    except Exception as e:                       # noqa: BLE001 —— 就是来找它抛不抛
        chk("host_blocked(%r) 不抛" % (h if len(str(h)) < 24 else str(h)[:21] + "…",),
            False, "%s: %s" % (type(e).__name__, e))
        continue
    chk("host_blocked(%r) 只回布尔" % (h if len(str(h)) < 24 else str(h)[:21] + "…",),
        isinstance(got, bool), repr(got))


# ── ⑤ 四条路接线都走这一处 ────────────────────────────────────────────
EVIL_URL = "http://%s:8770/api/state" % EVIL
chk("url_block_reason 拒换身回环地址",
    clip_article.url_block_reason(EVIL_URL) != "", clip_article.url_block_reason(EVIL_URL))
try:
    web_browse.normalize(EVIL_URL)
    chk("内置浏览器 normalize 拒换身回环地址", False, "居然放行")
except web_browse.Blocked as e:
    chk("内置浏览器 normalize 拒换身回环地址", "本机或内网" in str(e), str(e))
try:
    web_parse._guard(EVIL_URL)
    chk("平台解析 _guard 拒换身回环地址", False, "居然放行")
except ValueError as e:
    chk("平台解析 _guard 拒换身回环地址", "本机或内网" in str(e), str(e))
chk("封面 safe_image_url 拒换身回环地址",
    clip_article.safe_image_url("http://%s/cover.jpg" % EVIL) is False,
    clip_article.safe_image_url("http://%s/cover.jpg" % EVIL))

SRC = (REPO / "clip_article.py").read_text(encoding="utf-8")
chk("host_blocked / _idna_host 各只有一处定义（一把尺子）",
    SRC.count("def host_blocked(") == 1 and SRC.count("def _idna_host(") == 1,
    (SRC.count("def host_blocked("), SRC.count("def _idna_host(")))
chk("host_blocked 里确实调用了归一（不是写着不接线）",
    "_idna_host(h)" in SRC)
chk("原样与本名都过同一段名字正则 / IP 判据（不是只判其中一份）",
    "BLOCKED_HOSTS.match(cand)" in SRC and "ipaddress.ip_address(name)" in SRC)

print()
print("内网地址闸门·兼容字符（IDNA 换身写法不再溜过去）：%d 项通过，失败 %d 项"
      % (PASSED, len(FAIL)))
for n in FAIL:
    print("  ✗ " + n)
sys.exit(1 if FAIL else 0)
