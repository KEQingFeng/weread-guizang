#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""插图下载 / 订阅取回也要挡「本机以外的内网那一跳」(download_images.py / feed.py)。

为什么单开一条
    scheme 那道闸（file:// / data: / ftp: 不许替用户取）这两条路早就有了，但它只量
    「怎么取」，不量「去哪儿取」。于是 raw/*.json 里一条

        {"url": "http://169.254.169.254/latest/meta-data/", "file": "x.jpg"}

    或订阅页面里一条

        <link rel="alternate" type="application/rss+xml"
              href="http://192.168.1.1/admin">

    后端仍然会照取 —— 一篇文章的作者（不是用户）就借归藏这台机器的手，去敲了局域网
    或云元数据那扇门。剪藏 / 平台解析 / 内置浏览器三条路早就靠 `clip_article.host_blocked`
    把这一档挡住了，只有这两条路守着旧尺子。

这一条钉住
  · `clip_article.host_lan_blocked` / `url_lan_block_reason`：内网 / 链路本地 /
    元数据 / CGNAT / ULA 一律拒，**回环自己那一档放行**（离线夹具与用户本机的
    图床 / RSS 桥都长这样，全挡会把 `check_download_scheme.py` /
    `check_feed_scheme.py` 的夹具一起打死 —— 见 `host_lan_blocked` 的说明）；
  · `download_images.fetchable_url` 对这类地址回 False，`download_one` 一个请求都不发、
    回 `fail:blocked-url`、`.part` 与落点都不留；`collect_tasks` 只跳这一条；
  · `feed._check_url` 对这类地址抛同口径的人话；`feed._http` 在发请求之前就被挡住；
  · 对照：旧尺子（只看 scheme）确实放行内网 —— 证明这道闸不是空转；同一把
    `clip_article.host_blocked` 早就认得出它们，只是这两条路没接上。

网络层全部用记账的假 urlopen 顶掉，配置 / 落盘全在临时沙盒里，不联网、不碰真实
cache/ 与书库，故归 --fast 静态段。
"""
import contextlib
import json
import os
import shutil
import sys
import tempfile
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

SB = tempfile.mkdtemp(prefix="guizang-lannet-")
os.environ["GUIZANG_DATA"] = os.path.join(SB, "cache")
os.environ["GUIZANG_BOOKS"] = os.path.join(SB, "books")
os.makedirs(os.environ["GUIZANG_DATA"], exist_ok=True)
os.makedirs(os.environ["GUIZANG_BOOKS"], exist_ok=True)

import clip_article as C   # noqa: E402
import download_images as D  # noqa: E402
import feed  # noqa: E402

FAIL = []
N = 0


def chk(name, cond, extra=""):
    global N
    N += 1
    print(("  ok  " if cond else " FAIL ") + name + ("" if cond else "  <- " + repr(extra)))
    if not cond:
        FAIL.append(name)


# ── 1. 共用尺子：内网那一档拒，回环放行 ───────────────────────────────
LAN = (
    "192.168.1.1", "192.168.0.0", "10.0.0.1", "10.255.255.255",
    "172.16.0.1", "172.31.255.255", "169.254.169.254", "100.64.0.1",
    "0.0.0.0", "255.255.255.255", "198.18.0.1",
    "::ffff:192.168.1.1", "[::ffff:10.0.0.1]", "fc00::1", "[fc00::1]",
    "fe80::1", "192.168.%31.1", "3232235777", "0xC0A80101",
)
for host in LAN:
    chk("内网档拦下 %r" % (host,), C.host_lan_blocked(host) is True, host)

LOOPBACK = (
    "127.0.0.1", "127.0.0.2", "127.1", "localhost", "localhost.",
    "::1", "[::1]", "2130706433", "0x7f000001", "::ffff:127.0.0.1",
)
for host in LOOPBACK:
    chk("回环档放行 %r（离线夹具就长这样）" % (host,),
        C.host_lan_blocked(host) is False, host)

PUBLIC = ("example.com", "img.example.com", "8.8.8.8", "1.1.1.1", "2606:4700:4700::1111")
for host in PUBLIC:
    chk("公网档放行 %r" % (host,), C.host_lan_blocked(host) is False, host)

# url 那一层：scheme / 缺主机名 / 解不出来仍是同一句人话。
for u, frag in (("file:///etc/passwd", "只认 http/https"),
                ("data:text/plain,hi", "只认 http/https"),
                ("http://", "没有主机名"),
                ("http://192.168.1.1/x.jpg", "内网"),
                ("http://169.254.169.254/latest/meta-data/", "内网"),
                ("http://[::ffff:10.0.0.1]/x", "内网")):
    why = C.url_lan_block_reason(u)
    chk("url_lan_block_reason(%r) 说人话" % (u,), frag in why, why)
for u in ("http://example.com/a.jpg", "http://127.0.0.1:8770/a.jpg", "https://[::1]/f"):
    chk("url_lan_block_reason(%r) 放行" % (u,), C.url_lan_block_reason(u) == "",
        C.url_lan_block_reason(u))

# ── 2. 对照：旧尺子只看 scheme，内网照样放行；共用那把早就认得出 ───────
def scheme_only(url):
    parts = C.split_url(url)
    return bool(parts) and parts[0] in C.ALLOWED_SCHEMES


for u in ("http://192.168.1.1/admin", "http://169.254.169.254/latest/meta-data/",
          "http://10.0.0.1/x.jpg"):
    chk("对照：只看 scheme 的旧写法放行 %r" % (u,), scheme_only(u) is True)
    chk("对照：共用的 host_blocked 早就拒 %r（是没接上，不是没尺子）" % (u,),
        C.host_blocked(C.split_url(u)[1]) is True)


# ── 3. download_images：真值表 + 一次请求都不发 ──────────────────────
for u in ("http://192.168.1.1/a.jpg", "http://169.254.169.254/latest/meta-data/x",
          "http://[fc00::1]/a.png", "http://10.0.0.1/a.jpg"):
    chk("fetchable_url 拦下 %r" % (u,), D.fetchable_url(u) is False, u)
for u in ("http://127.0.0.1:8770/a.jpg", "http://localhost/a.png",
          "http://img.example.com/a.jpg", "  https://img.example.com/a.png  "):
    chk("fetchable_url 放行 %r" % (u,), D.fetchable_url(u) is True, u)

PNG = b"\x89PNG\r\n\x1a\n" + b"p" * 4000


class FakeResp:
    def __init__(self, body):
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return self.body


def fake_urlopen(body, calls):
    def _open(req, timeout=None):
        calls.append(getattr(req, "full_url", req))
        return FakeResp(body)
    return _open


@contextlib.contextmanager
def patched_urlopen(fn):
    saved = urllib.request.urlopen
    urllib.request.urlopen = fn
    try:
        yield
    finally:
        urllib.request.urlopen = saved


for u in ("http://192.168.1.1/a.jpg", "http://169.254.169.254/latest/meta-data/x"):
    tgt = os.path.join(SB, "blocked.png")
    calls = []
    with patched_urlopen(fake_urlopen(PNG, calls)):
        r = D.download_one(u, tgt, retries=2)
    chk("download_one(%r) 回 fail:blocked-url" % (u,), r == "fail:blocked-url", r)
    chk("download_one(%r) 一个请求都没发" % (u,), calls == [], calls)
    chk("download_one(%r) 不留正式件" % (u,), not os.path.exists(tgt))
    chk("download_one(%r) 不留 .part" % (u,), not os.path.exists(tgt + ".part"))

# 回环自己仍照取（夹具与本地图床不该被误伤）。
ok_tgt = os.path.join(SB, "loopback.png")
with patched_urlopen(fake_urlopen(PNG, [])):
    r = D.download_one("http://127.0.0.1:8770/a.png", ok_tgt, retries=1)
chk("回环地址仍然 ok 且内容一字不差（不误伤夹具）",
    r == "ok" and open(ok_tgt, "rb").read() == PNG, r)

# ── 4. collect_tasks：坏记录只跳一条，公网与回环照收 ─────────────────
raw = os.path.join(SB, "book", "raw")
img_dir = os.path.join(SB, "book", "images")
os.makedirs(raw, exist_ok=True)
with open(os.path.join(raw, "ch1.json"), "w", encoding="utf-8") as fh:
    json.dump({"images": [
        {"url": "https://img.example.com/1.jpg", "file": "ch0001_img01.jpg"},
        {"url": "http://192.168.1.1/secret.jpg", "file": "ch0001_img02.jpg"},
        {"url": "http://169.254.169.254/latest/meta-data/", "file": "ch0001_img03.jpg"},
        {"url": "http://127.0.0.1:8770/local.png", "file": "ch0001_img04.png"},
        {"url": "http://[fc00::1]/x.png", "file": "ch0001_img05.png"},
    ]}, fh, ensure_ascii=False)

tasks, skipped = D.collect_tasks(raw, img_dir)
names = [os.path.basename(t[1]) for t in tasks]
urls = [u for u, _ in tasks]
chk("清单只留公网与回环那两条", names == ["ch0001_img01.jpg", "ch0001_img04.png"],
    (names, urls))
chk("内网三条各记一笔跳过", skipped == 3, skipped)

# ── 5. feed：_check_url 挡内网，_http 发请求之前就被挡住 ──────────────
for u in ("http://192.168.1.1/admin", "http://169.254.169.254/latest/meta-data/",
          "http://10.0.0.1/feed.xml", "http://[fc00::1]/feed"):
    try:
        feed._check_url(u)
        ok, msg = False, "没抛"
    except ValueError as e:
        ok, msg = "内网" in str(e), str(e)
    except Exception as e:  # noqa: BLE001
        ok, msg = False, repr(e)
    chk("_check_url(%r) 抛「内网地址」" % (u,), ok, msg)

for u in ("http://example.com/feed.xml", "http://127.0.0.1:8770/feed",
          "  https://example.com:8443/a?b=1  "):
    try:
        got = feed._check_url(u)
        ok, msg = True, got
    except Exception as e:  # noqa: BLE001
        ok, msg = False, repr(e)
    chk("_check_url 照旧放行 %r" % (u,), ok, msg)

# file:// 那道旧闸不许因为这次改动松掉。
for u in ("file:///etc/passwd", "data:text/plain,hi"):
    try:
        feed._check_url(u)
        ok, msg = False, "没抛"
    except ValueError as e:
        ok, msg = "只认 http/https" in str(e), str(e)
    chk("_check_url(%r) 仍按 scheme 挡" % (u,), ok, msg)

calls = []
with patched_urlopen(fake_urlopen(b"<rss/>", calls)):
    try:
        feed._http("http://192.168.1.1/feed.xml")
        got = "没抛"
    except ValueError as e:
        got = str(e)
    except Exception as e:  # noqa: BLE001
        got = repr(e)
chk("_http 对内网地址抛「内网地址」", "内网" in got, got)
chk("_http 一个请求都没发", calls == [], calls)

shutil.rmtree(SB, ignore_errors=True)
print()
print("插图 / 订阅内网地址门禁：%d 项通过，失败 %d 项" % (N - len(FAIL), len(FAIL)))
if FAIL:
    print("未过：" + " / ".join(FAIL))
sys.exit(1 if FAIL else 0)
