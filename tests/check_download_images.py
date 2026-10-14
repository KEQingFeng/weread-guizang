#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""图片下载的离线门禁：下不完整 / 写一半 / 坏记录，都不许变成一张永久裂图。

为什么单开一份：download_images.py 是界面上「重新下载图片」和导出收尾
（export_precise.download_all_images）共用的那一条路，此前两边各抄了一遍
`open(fp, "wb")`（先清空再写），而跳过判断只看大小。于是「进程在写的中途被
杀掉」这种最常见的中断，留下的半截图只要过 1KB 就被当成下好了 —— 重下按钮
再点一百次也不会碰它。这里把三条口径钉住：

  A 落盘是「同目录 .part + os.replace」：正式文件要么不存在，要么是完整件；
    中途失败（连不上、短包、改不了名）不许在正式路径上留下任何东西
  B 跳过判断 = 大小达标 **且** 文件头认得出是图片；老版本留下的坏文件会被重下
  C raw/*.json 整份是从网页扒下来的外部数据：坏 JSON / 缺字段 / 越界文件名
    只跳过不拖垮整本，同一个落点撞两次合并成一条（两条线程写同一个文件，
    最后留下谁的半截说不准）

全程离线：HTTP 用记账的假 urlopen 顶掉，端到端那一条打本机临时端口。
"""
import contextlib
import http.server
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

_ORIG_GETADDRINFO = socket.getaddrinfo

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
JPG = b"\xff\xd8\xff\xe0" + b"j" * 4000
HTML = b"<!DOCTYPE html><html><body>" + b"x" * 2000
WEBP = b"RIFF\x00\x00\x00\x00WEBPVP8 " + b"w" * 4000


class FakeResp:
    def __init__(self, body, boom=False):
        self.body = body
        self.boom = boom

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        if self.boom:
            raise OSError("connection reset")
        return self.body


def fake_urlopen(body, boom=False, calls=None):
    def _open(req, timeout=None):
        if calls is not None:
            calls.append(getattr(req, "full_url", req))
        return FakeResp(body, boom)
    return _open


@contextlib.contextmanager
def patched_urlopen(fn):
    saved = urllib.request.urlopen
    urllib.request.urlopen = fn
    try:
        yield
    finally:
        urllib.request.urlopen = saved


SB = tempfile.mkdtemp(prefix="guizang-imgdl-")


def put(path, data):
    with open(path, "wb") as fh:
        fh.write(data)
    return path


# ── 1. import 不该有副作用 ─────────────────────────────────────
chk("import 本模块不动整个进程的名字解析（socket.getaddrinfo 原样）",
    socket.getaddrinfo is _ORIG_GETADDRINFO)

saved_gai = socket.getaddrinfo
try:
    socket.getaddrinfo = lambda *a, **k: [
        (socket.AF_INET6, 1, 6, "", ("::1", 0, 0, 0)),
        (socket.AF_INET, 1, 6, "", ("127.0.0.1", 0)),
    ]
    D.force_ipv4()
    families = [r[0] for r in socket.getaddrinfo("example.com", 80)]
    chk("force_ipv4：IPv6 结果被滤掉，只剩 IPv4",
        families == [socket.AF_INET], families)
    D.force_ipv4()
    chk("force_ipv4：重复调用幂等（不套第二层、结果不变）",
        [r[0] for r in socket.getaddrinfo("example.com", 80)] == [socket.AF_INET])
finally:
    socket.getaddrinfo = saved_gai
chk("force_ipv4 测试完把 socket.getaddrinfo 还原了（不污染后面的用例）",
    socket.getaddrinfo is _ORIG_GETADDRINFO)

# ── 2. 文件头认图 ──────────────────────────────────────────────
chk("文件头：PNG 认得出", D.head_is_image(PNG))
chk("文件头：JPEG 认得出", D.head_is_image(JPG))
chk("文件头：WebP（RIFF 容器）认得出", D.head_is_image(WEBP))
chk("文件头：HTML 错误页认不出", not D.head_is_image(HTML))
chk("文件头：JSON 错误回包认不出", not D.head_is_image(b'{"error":"gone"}'))

# ── 3. 「已下好」的判断 ────────────────────────────────────────
good = put(os.path.join(SB, "good.png"), PNG)
bad = put(os.path.join(SB, "bad.png"), HTML)
small = put(os.path.join(SB, "small.png"), PNG[:300])
chk("完整件：够大且文件头是图 -> 当已下好", D.is_complete(good))
chk("坏文件：够大但文件头不是图 -> 不当已下好（老版本的坏文件会被重下）",
    not D.is_complete(bad))
chk("半截图：文件头对但没到门槛 -> 不当已下好", not D.is_complete(small))
chk("不存在的文件 -> 不当已下好", not D.is_complete(os.path.join(SB, "nope.png")))

# ── 4. 落盘：原子、可跳过、失败不留渣 ──────────────────────────
tgt = os.path.join(SB, "dl_ok.png")
with patched_urlopen(fake_urlopen(PNG)):
    r = D.download_one("http://127.0.0.1/a.png", tgt, retries=1)
chk("下载成功：返回 ok", r == "ok", r)
chk("下载成功：内容一字不差", os.path.exists(tgt) and open(tgt, "rb").read() == PNG)
chk("下载成功：不留 .part 残渣", not os.path.exists(tgt + ".part"))

calls = []
with patched_urlopen(fake_urlopen(PNG, calls=calls)):
    r = D.download_one("http://127.0.0.1/a.png", tgt, retries=1)
chk("已下好的文件：跳过，一个请求都不发", r == "skip" and calls == [], (r, calls))

heal = put(os.path.join(SB, "heal.png"), HTML)
with patched_urlopen(fake_urlopen(PNG)):
    r = D.download_one("http://127.0.0.1/heal.png", heal, retries=1)
chk("坏的已有文件被重下，不再永久跳过",
    r == "ok" and open(heal, "rb").read() == PNG, r)

tgt2 = os.path.join(SB, "dl_boom.png")
with patched_urlopen(fake_urlopen(b"", boom=True)):
    r = D.download_one("http://127.0.0.1/b.png", tgt2, retries=1)
chk("连接中途报错：返回 fail，正式路径上不出现文件",
    r.startswith("fail:") and not os.path.exists(tgt2), r)
chk("连接中途报错：临时件也清掉", not os.path.exists(tgt2 + ".part"))

tgt3 = os.path.join(SB, "dl_short.png")
with patched_urlopen(fake_urlopen(b"tiny")):
    r = D.download_one("http://127.0.0.1/s.png", tgt3, retries=1)
chk("短包（没到门槛）：不当成品落盘", r == "fail:empty" and not os.path.exists(tgt3), r)

tgt4 = os.path.join(SB, "dl_replace.png")
saved_replace = os.replace


def boom_replace(a, b):
    raise OSError("disk full")


try:
    os.replace = boom_replace
    with patched_urlopen(fake_urlopen(PNG)):
        r = D.download_one("http://127.0.0.1/r.png", tgt4, retries=1)
finally:
    os.replace = saved_replace
chk("改名失败（磁盘满）：正式文件不出现", not os.path.exists(tgt4))
chk("改名失败：临时件清掉，不留半截", not os.path.exists(tgt4 + ".part"))
chk("改名失败：如实报 fail", r.startswith("fail:"), r)

# ── 5. 文件名护栏 ──────────────────────────────────────────────
img_dir = os.path.join(SB, "images")
ok_target = D.safe_target(img_dir, "ch0001_img01.jpg")
chk("文件名：正常检查点放行",
    ok_target == os.path.normpath(os.path.join(img_dir, "ch0001_img01.jpg")), ok_target)
for weird in ["../evil.jpg", "a/b.jpg", "..\\evil.jpg", "/etc/passwd",
              "", "   ", "..", ".", None, 123]:
    chk("文件名：%r 拦下" % (weird,), D.safe_target(img_dir, weird) is None,
        D.safe_target(img_dir, weird))

# ── 6. raw/*.json 的坏记录 ─────────────────────────────────────
raw = os.path.join(SB, "book", "raw")
os.makedirs(raw, exist_ok=True)


def put_raw(name, obj):
    with open(os.path.join(raw, name), "w", encoding="utf-8") as fh:
        fh.write(obj if isinstance(obj, str) else json.dumps(obj, ensure_ascii=False))


put_raw("a_good.json", {"images": [
    {"url": "http://x/1.jpg", "file": "ch0001_img01.jpg"},
    {"url": "http://x/2.jpg", "file": "ch0001_img02.jpg"},
    {"url": "http://x/1.jpg", "file": "ch0001_img01.jpg"},
]})
put_raw("b_broken.json", "{ 这不是 JSON")
put_raw("c_missing.json", {"images": [
    {"file": "ch0002_img01.jpg"},
    {"url": "http://x/4.jpg"},
    {"url": "", "file": "ch0002_img03.jpg"},
    {"url": "http://x/6.jpg", "file": "../escape.jpg"},
    "不是字典",
    {"url": "http://x/8.jpg", "file": "ch0002_img04.jpg"},
]})
put_raw("d_noimages.json", {"title": "没有 images 字段"})

tasks, skipped = D.collect_tasks(raw, img_dir)
names = [os.path.basename(t[1]) for t in tasks]
chk("坏 JSON 只记一笔，不当场炸", "ch0001_img01.jpg" in names, (names, skipped))
chk("清单只留合法落点，且按文件顺序",
    names == ["ch0001_img01.jpg", "ch0001_img02.jpg", "ch0002_img04.jpg"], names)
chk("缺 url / 缺 file / 空 url / 越界 / 非字典 / 重复 各自只跳过",
    skipped == 7, skipped)
chk("越界落点没有被当成任务", all("escape" not in n for n in names), names)
chk("目录里没有 raw/*.json 时给空清单，不报错",
    D.collect_tasks(os.path.join(SB, "no-such-dir"), img_dir) == ([], 0))

# ── 7. 端到端：真起一个临时端口，跑一次 main() ──────────────────
class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/ok.png":
            body, code = PNG, 200
        else:
            body, code = b"gone", 404
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
    e2e_raw = os.path.join(out, "book123", "raw")
    os.makedirs(e2e_raw, exist_ok=True)
    with open(os.path.join(e2e_raw, "ch1.json"), "w", encoding="utf-8") as fh:
        json.dump({"images": [
            {"url": f"http://127.0.0.1:{port}/ok.png", "file": "ch0001_img01.png"},
            {"url": f"http://127.0.0.1:{port}/missing.png", "file": "ch0001_img02.png"},
            {"url": f"http://127.0.0.1:{port}/ok.png", "file": "../越界.png"},
        ]}, fh, ensure_ascii=False)

    env = dict(os.environ, GUIZANG_OUTPUT=out)
    proc = subprocess.run([sys.executable, os.path.join(ROOT, "download_images.py"), "book123"],
                          cwd=ROOT, env=env, capture_output=True, text=True, timeout=90)
    e2e_img = os.path.join(out, "book123", "images", "ch0001_img01.png")
    chk("端到端：脚本正常退出", proc.returncode == 0, proc.stderr[-400:])
    chk("端到端：图真下到了 images/ 且内容正确",
        os.path.exists(e2e_img) and open(e2e_img, "rb").read() == PNG, proc.stdout[-300:])
    chk("端到端：计数如实（ok=1 / fail=1）",
        "ok=1" in proc.stdout and "fail=1" in proc.stdout, proc.stdout[-300:])
    chk("端到端：越界文件名的记录被跳过并报出来",
        "坏记录" in proc.stdout and not os.path.exists(os.path.join(out, "book123", "越界.png")))
    chk("端到端：不留 .part", not os.path.exists(e2e_img + ".part"))
finally:
    srv.shutdown()
    srv.server_close()

shutil.rmtree(SB, ignore_errors=True)
print()
print("图片下载门禁：%d 项通过，失败 %d 项" % (N - len(FAIL), len(FAIL)))
if FAIL:
    print("未过：" + " / ".join(FAIL))
sys.exit(1 if FAIL else 0)
