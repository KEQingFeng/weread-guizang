#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""内网地址闸门的百分号编码：`%31%32%37.0.0.1` 就是 `127.0.0.1`，不许放过去。

`clip_article.host_blocked` 是全仓唯一那把「本机 / 内网地址一律拒绝」的尺子：剪藏取页
（`fetch`）、内置浏览器（`web_browse.normalize`）、平台解析（`web_parse._guard`）、
首图与封面兜底（`safe_image_url`）全从这里过。它原先只认**字面量** ——
`urlparse().hostname` 不做百分号解码，`http://%31%32%37.0.0.1/` 递进来的就是
`%31%32%37.0.0.1`；而 `urllib.request.Request._parse` 会 `unquote(self.host)`，
socket 真正连的是 `127.0.0.1`。尺子看的是一串、连的是另一串，于是：

    .venv/bin/python -c "import clip_article as c; print(c.url_allowed('http://%31%32%37.0.0.1/'))"
      -> True      （改之前）

外站页面里的一张 `<img src="http://%31%32%37.0.0.1:8770/api/state">`、或用户随手粘的
一条编码过的链接，就能让归藏替他去敲本机与内网的门，回包还会被收进剪藏 / 封面文件。
修法是唯一一处：`host_blocked` 按 urllib 那一步先 `unquote` 一次，再走原有的
IPv4 / IPv6 / 非常规写法 / 正则那套判据。

这一条只钉这把尺子，三面都要：
  ① 纯函数：编码过的回环 / 私网 / 链路本地（含元数据 169.254.169.254）写法一律拒；
     正常公网域名（含被编码过的公网域名）一个不放宽；
  ② 一处改、四处管：`url_block_reason` / `web_browse.normalize` / `web_parse._guard`
     都拒，且这些拒法是人话，不是 Python 的英文运行期错误；
  ③ 真机对照：进程内起一个真的 127.0.0.1 服务，先证明它活着（直连能打中），再让
     `clip_article.fetch` 去取编码过的地址 —— 必须抛，且服务一次都没被打中。

只碰纯函数与进程内的假服务，不联网（回环而已）、不碰真实 cache/ 与书库，归静态段。
"""
import http.server
import os
import pathlib
import socketserver
import sys
import tempfile
import threading
import urllib.parse
import urllib.request

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

ROOT = tempfile.mkdtemp(prefix="guizang-hostenc-")
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


# ── ① 纯函数：编码过的内网地址一律拒 ────────────────────────────────────
# 每一条都是「浏览器 / getaddrinfo 真会连到那个内网地址」的写法：
# 百分号编码的回环、localhost、私网、链路本地（云元数据 169.254.169.254）。
ENCODED_INTERNAL = [
    "%31%32%37.0.0.1",                     # 127.0.0.1
    "%6c%6f%63%61%6c%68%6f%73%74",         # localhost
    "%31%30.0.0.1",                        # 10.0.0.1
    "192.168.%31.1",                       # 192.168.1.1
    "%31%37%32.16.0.1",                    # 172.16.0.1
    "169.254.169.%32%35%34",               # 169.254.169.254（云元数据）
    "0x7f.0.0.1",                          # 十六进制 127
    "0177.0.0.1",                          # 八进制 127（inet_aton 认）
    "2130706433",                          # 单段整数 127.0.0.1
    "%5B::1%5D",                           # [::1]（编码方括号）
]
for h in ENCODED_INTERNAL:
    chk("编码内网地址一律拒：%s" % h, clip_article.host_blocked(h) is True,
        clip_article.host_blocked(h))

# 自证不是空转：这几种写法只做「字面量」检查确实漏 —— 解码后才露真身，或
# 旧的正则 / ipaddress 对原样串根本认不出。门禁必须能点出「老尺子为什么漏」。
for h, dec in (("%31%32%37.0.0.1", "127.0.0.1"),
               ("%6c%6f%63%61%6c%68%6f%73%74", "localhost")):
    chk("对照：%s 原样串不是内网字面量（旧尺子漏在这）" % h,
        clip_article.BLOCKED_HOSTS.match(h) is None, h)
    chk("对照：%s 解码后确是本机 %s" % (h, dec),
        urllib.parse.unquote(h) == dec and clip_article.host_blocked(dec) is True,
        urllib.parse.unquote(h))

# ── 正常域名一个不放宽（修法不许把公网误伤）────────────────────────────
PUBLIC = ["example.com", "www.youtube.com", "weread.qq.com", "i.ytimg.com",
          "sub.domain.co", "a1.example.com.", "%65xample.com"]
for h in PUBLIC:
    chk("公网域名照常放行：%s" % h, clip_article.host_blocked(h) is False,
        clip_article.host_blocked(h))
chk("空 / None 仍按原来的口径当拒",
    clip_article.host_blocked("") is True and clip_article.host_blocked(None) is True)

# ── ② 一处改、四处管：各处入口都拒，且说的是人话 ─────────────────────────
EVIL = "http://%31%32%37.0.0.1:8770/api/state"
chk("url_block_reason 拒编码回环", clip_article.url_block_reason(EVIL) != "",
    clip_article.url_block_reason(EVIL))
chk("url_allowed 回 False", clip_article.url_allowed(EVIL) is False)

try:
    web_browse.normalize(EVIL)
    wb_ok = False
except web_browse.Blocked as e:
    wb_ok = "本机" in str(e) or "内网" in str(e)
except Exception as e:                                     # noqa: BLE001
    wb_ok = False
    print("      web_browse.normalize 抛的不是 Blocked：%r" % (e,))
chk("web_browse.normalize 拒（Blocked + 人话）", wb_ok)

try:
    web_parse._guard(EVIL)
    wp_ok = False
except ValueError as e:
    wp_ok = "本机" in str(e) or "内网" in str(e)
except Exception as e:                                     # noqa: BLE001
    wp_ok = False
    print("      web_parse._guard 抛的不是 ValueError：%r" % (e,))
chk("web_parse._guard 拒（ValueError + 人话）", wp_ok)

# 首图兜底那把尺子也走同一个判据：编码过的图地址必须被跳过（回 False）。
chk("safe_image_url 不认编码回环这张图",
    clip_article.safe_image_url(EVIL) is False, clip_article.safe_image_url(EVIL))

# ── ③ 真机：起一个真的回环服务，证明「拒」是真没连过去 ───────────────────
HITS = {"n": 0}


class _H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        HITS["n"] += 1
        body = b"<html><body>SECRET-INTERNAL</body></html>"
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


srv = socketserver.TCPServer(("127.0.0.1", 0), _H)
PORT = srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start()

# 先证明服务活着：直连一次能打中（否则下面的「0 次」什么也证明不了）。
try:
    urllib.request.urlopen("http://127.0.0.1:%d/probe" % PORT, timeout=3).read()
    live = HITS["n"] == 1
except Exception as e:                                     # noqa: BLE001
    live = False
    print("      直连探活失败：%r" % (e,))
chk("真机夹具：回环服务活着（直连能打中）", live, HITS["n"])

HITS["n"] = 0
EVIL_URL = "http://%31%32%37.0.0.1:" + str(PORT) + "/"
try:
    got = clip_article.fetch(EVIL_URL)
    blocked = False
    print("      fetch 竟然取回了：%r" % (str(got)[:80],))
except ValueError as e:
    blocked = "本机" in str(e) or "内网" in str(e)
except Exception as e:                                     # noqa: BLE001
    blocked = False
    print("      fetch 抛的不是那句人话：%r" % (e,))
chk("真机：clip_article.fetch 拒编码回环（抛人话）", blocked)
chk("真机：服务一次都没被打中（不是先连再拒）", HITS["n"] == 0, HITS["n"])

srv.shutdown()
srv.server_close()

print("\n内网地址闸门（百分号编码）体检：通过 %d 项，失败 %d 项" % (PASSED, len(FAIL)))
for name in FAIL:
    print("  x " + name)
sys.exit(1 if FAIL else 0)
