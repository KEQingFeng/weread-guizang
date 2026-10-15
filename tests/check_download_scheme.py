#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""插图地址 scheme 收口：file:// / data: 不许替用户取回来写进书库 (download_images.py)。

为什么单开一条
    download_images 的地址来自 raw/*.json —— 整份是从网页扒下来、用户还能手工编辑的
    外部数据，模块自己的注释就写着「不该假定它一定乖」。它此前只装了**跳转**那道闸
    （clip_article.ensure_redirect_guard），跳转闸只管 `Location`，管不了「一开始喂进来
    的就是 file://」。可默认 opener 是认得 `file:` 的：

        urllib.request.urlopen("file:///etc/passwd")

    会把本机文件原样读回来。于是 raw 里一条 `"url": "file:///Users/…/.ssh/id_rsa"` 就会
    被当成插图下进 images/，往后还会跟着书库同步出去。`data:` / `ftp:` / `gopher:` 同理，
    都不是「替用户下了一张插图」。剪藏、平台解析、内置浏览器、订阅四条取页路都显式核过
    scheme，只有插图下载这一条漏了。

这一条钉住
  · `fetchable_url` 只放行 http / https（大小写、前后空白归一；半截 IPv6 这类
    urlparse 解不出来的也算不合法）；
  · `download_one` 遇到非 http(s) 一次都不发请求，回 `fail:blocked-url`，正式路径与
    .part 上什么都不留；
  · `collect_tasks` 把这类记录当坏记录跳过，其余正常图照下；
  · 对照：默认 opener 确实读得回本机文件 —— 证明这道闸不是空转，是真堵了一个洞。

网络层用记账的假 urlopen 顶掉，端到端那条打本机临时端口、file:// 那个指向临时目录里的
一个假密钥文件，不联网、不碰真实书库，故归 --fast 静态段。
"""
import contextlib
import http.server
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import download_images as D  # noqa: E402

FAIL = []
N = 0


def chk(name, cond, extra=""):
    global N
    N += 1
    print(("  ok  " if cond else " FAIL ") + name + ("" if cond else "  <- " + repr(extra)))
    if not cond:
        FAIL.append(name)


PNG = b"\x89PNG\r\n\x1a\n" + b"p" * 4000
# 一个「要是漏出去就糟了」的假密钥：够过 download_one 的 500 字节线，方便演示老路会落盘。
SECRET = b"PRIVATE-KEY-MATERIAL " * 40


class FakeResp:
    def __init__(self, body):
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return self.body


def fake_urlopen(body, calls=None):
    def _open(req, timeout=None):
        if calls is not None:
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


SB = tempfile.mkdtemp(prefix="guizang-imgscheme-")
secret_path = os.path.join(SB, "id_rsa.txt")
with open(secret_path, "wb") as fh:
    fh.write(SECRET)
SECRET_URL = "file://" + secret_path if os.name != "nt" else "file:///" + secret_path.replace("\\", "/")

# ── 1. fetchable_url 的真值表 ─────────────────────────────────────
for url in ("http://img.example.com/a.jpg", "https://img.example.com/a.jpg",
            "HTTPS://Img.Example.COM/a.png", "  https://img.example.com/a.png  "):
    chk("放行 %r" % url, D.fetchable_url(url) is True)
for url in ("file:///etc/passwd", SECRET_URL, "data:image/png;base64,AAAA",
            "ftp://img.example.com/a.jpg", "gopher://x/a", "//img.example.com/a.jpg",
            "images/a.jpg", "", "   ", "http://[::1", None, 123, b"http://x/a.png"):
    chk("拦下 %r" % (url,), D.fetchable_url(url) is False)

# ── 2. 对照：默认 opener 真的读得回本机文件（洞是真的） ────────────
with urllib.request.urlopen(SECRET_URL, timeout=10) as resp:
    leaked = resp.read()
chk("对照：默认 opener 原样读回本机文件（不是理论上的洞）", leaked == SECRET)


def old_download(url, fpath, min_bytes=D.MIN_BYTES):
    """改之前 download_one 的形状：装跳转闸后直接取，不看 scheme。"""
    import clip_article
    clip_article.ensure_redirect_guard()
    req = urllib.request.Request(url, headers=D.HEADERS)
    with urllib.request.urlopen(req, timeout=10) as resp:
        raw = resp.read()
    if len(raw) >= min_bytes:
        with open(fpath, "wb") as fh:
            fh.write(raw)
        return "ok"
    return "fail:empty"


old_target = os.path.join(SB, "leaked.jpg")
chk("对照：老写法把本机密钥文件写成了一张「插图」",
    old_download(SECRET_URL, old_target) == "ok" and open(old_target, "rb").read() == SECRET)

# ── 3. download_one：非 http(s) 一次请求都不发，什么都不留 ────────
for label, url in (("file://", SECRET_URL), ("data:", "data:image/png;base64,AAAA")):
    tgt = os.path.join(SB, "blocked_%s.png" % label.strip(":/"))
    calls = []
    with patched_urlopen(fake_urlopen(PNG, calls)):
        r = D.download_one(url, tgt, retries=2)
    chk("%s：回 fail:blocked-url（不重试、不伪装成网络失败）" % label,
        r == "fail:blocked-url", r)
    chk("%s：一个请求都没发" % label, calls == [], calls)
    chk("%s：正式路径上不出现文件" % label, not os.path.exists(tgt))
    chk("%s：不留 .part" % label, not os.path.exists(tgt + ".part"))

# 真 http 地址照旧走得通（大小写 / 空白也认）。
ok_target = os.path.join(SB, "ok.png")
with patched_urlopen(fake_urlopen(PNG)):
    r = D.download_one("  HTTPS://img.example.com/a.png  ", ok_target, retries=1)
chk("真 https：仍然 ok 且内容一字不差",
    r == "ok" and open(ok_target, "rb").read() == PNG, r)

# ── 4. collect_tasks：坏 scheme 只跳一条，正常图照收 ─────────────
raw = os.path.join(SB, "book", "raw")
img_dir = os.path.join(SB, "book", "images")
os.makedirs(raw, exist_ok=True)
with open(os.path.join(raw, "ch1.json"), "w", encoding="utf-8") as fh:
    json.dump({"images": [
        {"url": "https://img.example.com/1.jpg", "file": "ch0001_img01.jpg"},
        {"url": SECRET_URL, "file": "ch0001_img02.jpg"},
        {"url": "data:image/png;base64,AAAA", "file": "ch0001_img03.jpg"},
        {"url": "ftp://img.example.com/4.jpg", "file": "ch0001_img04.jpg"},
        {"url": "  http://img.example.com/5.jpg  ", "file": "ch0001_img05.jpg"},
        {"url": "http://[::1", "file": "ch0001_img06.jpg"},
    ]}, fh, ensure_ascii=False)

tasks, skipped = D.collect_tasks(raw, img_dir)
urls = [u for u, _ in tasks]
names = [os.path.basename(t[1]) for t in tasks]
chk("清单只留 http/https 两条",
    names == ["ch0001_img01.jpg", "ch0001_img05.jpg"], (names, urls))
chk("收进来的地址是 strip 过的原样", urls == ["https://img.example.com/1.jpg",
                                              "http://img.example.com/5.jpg"], urls)
chk("file:// / data: / ftp: / 半截 IPv6 各记一笔跳过", skipped == 4, skipped)

# ── 5. 端到端：main() 不许把 file:// 那条下进 images/ ────────────
class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        body, code = (PNG, 200) if self.path == "/ok.png" else (b"gone", 404)
        self.send_response(code)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
port = srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start()
try:
    out = os.path.join(SB, "e2e_output")
    e2e_raw = os.path.join(out, "book7", "raw")
    os.makedirs(e2e_raw, exist_ok=True)
    with open(os.path.join(e2e_raw, "ch1.json"), "w", encoding="utf-8") as fh:
        json.dump({"images": [
            {"url": "http://127.0.0.1:%d/ok.png" % port, "file": "ch0001_img01.png"},
            {"url": SECRET_URL, "file": "ch0001_img02.png"},
        ]}, fh, ensure_ascii=False)

    env = dict(os.environ, GUIZANG_OUTPUT=out)
    proc = subprocess.run([sys.executable, os.path.join(ROOT, "download_images.py"), "book7"],
                          cwd=ROOT, env=env, capture_output=True, text=True, timeout=90)
    e2e_img_dir = os.path.join(out, "book7", "images")
    e2e_good = os.path.join(e2e_img_dir, "ch0001_img01.png")
    e2e_bad = os.path.join(e2e_img_dir, "ch0001_img02.png")
    chk("端到端：脚本正常退出", proc.returncode == 0, proc.stderr[-400:])
    chk("端到端：正常图下到了", os.path.exists(e2e_good)
        and open(e2e_good, "rb").read() == PNG)
    chk("端到端：file:// 那条没被下成文件", not os.path.exists(e2e_bad))
    # 整棵 images/ 里都不许出现密钥内容 —— 换了个名字落盘也算漏。
    leaked_files = []
    for root, _dirs, files in os.walk(e2e_img_dir):
        for name in files:
            with open(os.path.join(root, name), "rb") as fh:
                if SECRET in fh.read():
                    leaked_files.append(name)
    chk("端到端：images/ 里任何文件都不含密钥内容", leaked_files == [], leaked_files)
    chk("端到端：坏 scheme 记在那句「坏记录跳过」里，不冒充成下载失败",
        "坏记录" in proc.stdout and "ok=1" in proc.stdout, proc.stdout[-300:])
finally:
    srv.shutdown()
    srv.server_close()

shutil.rmtree(SB, ignore_errors=True)
print()
print("插图地址 scheme 门禁：%d 项通过，失败 %d 项" % (N - len(FAIL), len(FAIL)))
if FAIL:
    print("未过：" + " / ".join(FAIL))
sys.exit(1 if FAIL else 0)
