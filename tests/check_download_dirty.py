#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""插图下载的文件名脏值：一条坏记录不许带走整批图（download_images.py）。

`safe_target` 把文件名收成 images/ 下的落点，此前只拦「路径分隔符 / 绝对路径 / ..」。
可 raw/*.json 整份是从网页上扒下来的数据，`file` 里完全可能是 NUL、别的控制字符，
或浏览器把 JS 字符串拦腰截断留下的半个代理码点（U+D800–U+DFFF，JSON 里写作 \\ud800）。
那三种名字在文件系统这一关抛的不是 OSError：

    os.path.getsize("a\\x00b.jpg")   -> ValueError: embedded null character in path
    os.path.getsize("a\\ud800b.jpg") -> UnicodeEncodeError: surrogates not allowed

而 download_one 里 `is_complete(fpath)` 在 try 之外、is_complete 只 except OSError，
主流程又在 `fut.result()` 把异常原样取回来 —— 于是**一张**图的脏名字让这本书其余
几百张图一张都下不来，和 collect_tasks「坏记录只跳过不牵连」的约定正好相反。

这一条是 --fast 的离线门禁，四条口径：
  · safe_target 对 NUL / C0 控制符 / DEL / 落单代理码点一律回 None（真 emoji、
    中文书名这些正常名字一个都不许误伤）；
  · 对照：这些名字直接喂给文件系统确实抛 ValueError / UnicodeEncodeError —— 证明
    这道护栏不是空转；
  · is_complete 只认 OSError 的老写法要改成「算不出大小 = 没下好」，不往外抛；
  · collect_tasks 遇到这些记录只记一笔跳过，其余正常图照下；download_one 即使被
    直接塞一个坏路径，也只回 fail:… 不许抛。

网络层用记账的假 urlopen 顶掉，不起服务、不联网。
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


SB = tempfile.mkdtemp(prefix="guizang-imgdirty-")
IMG = os.path.join(SB, "images")
os.makedirs(IMG, exist_ok=True)

HI, LO = "\ud83d", "\ude00"          # 落单的高位 / 低位代理码点

# ── 1. safe_target：坏名字一律 None ────────────────────────────
for weird in ["a\x00b.jpg", "a" + HI + "b.jpg", "a" + LO + "b.jpg",
              "\x00", "tab\tname.jpg", "nl\nname.jpg", "del\x7fname.jpg",
              "esc\x1b.jpg", "null\x00", HI]:
    chk("文件名 %r 拦下" % (weird,), D.safe_target(IMG, weird) is None,
        D.safe_target(IMG, weird))

# 别洗过头：正常名字（中文、日文、真 emoji、空格、书名号）照放行。
for good in ["ch0001_img01.jpg", "第一章 插图_01.jpeg", "図形.png",
             "😀_cover.webp", "《归藏》封面.png", "a+b&c.gif"]:
    chk("文件名 %r 放行（不误伤正常名字）" % (good,),
        D.safe_target(IMG, good) is not None, D.safe_target(IMG, good))

# 老护栏没被这次改动碰掉：穿越路径照样拒。
for bad in ["../evil.jpg", "a/b.jpg", "..\\evil.jpg", "/etc/passwd", "..", "."]:
    chk("老口径还在：%r 拦下" % (bad,), D.safe_target(IMG, bad) is None,
        D.safe_target(IMG, bad))

# 成对的代理码点经 json 解码已合成一个真字符（emoji 不是代理对），不该被判成脏。
emoji = json.loads('"\\ud83d\\ude00"')
chk("json 解出的真 emoji 是一个字符，不是代理对", emoji == "😀" and len(emoji) == 1, repr(emoji))
chk("真 emoji 名字照放行", D.safe_target(IMG, emoji + ".jpg") is not None)

# ── 2. 对照：这些名字确实过不了文件系统 ────────────────────────
for weird, want in [("a\x00b.jpg", ValueError), ("a" + HI + "b.jpg", UnicodeEncodeError)]:
    try:
        os.path.getsize(os.path.join(IMG, weird))
        raised = None
    except (OSError, ValueError) as e:
        raised = type(e)
    chk("对照：%r 直接喂给 getsize 抛 %s" % (weird, want.__name__),
        raised is not None and issubclass(raised, want), raised)

# ── 3. is_complete：算不出大小 = 没下好，不往外抛 ───────────────
for weird in ["a\x00b.jpg", "a" + HI + "b.jpg", "a" + LO + "b.jpg", "ctrl\x07.jpg"]:
    try:
        got = D.is_complete(os.path.join(IMG, weird))
    except Exception as e:                    # noqa: BLE001 - 就是要证明它不抛
        got = "RAISED %r" % e
    chk("is_complete(%r) 回 False，不抛" % (weird,), got is False, got)

# 正常文件那一档没被这次放宽影响。
good_file = os.path.join(IMG, "real.png")
with open(good_file, "wb") as fh:
    fh.write(PNG)
chk("正常完整图仍判已下好", D.is_complete(good_file) is True)
chk("正常但太小的文件仍判没下好", D.is_complete(good_file, min_bytes=len(PNG) + 1) is False)

# ── 4. collect_tasks：坏记录只跳一条，整批照走 ─────────────────
raw = os.path.join(SB, "book", "raw")
os.makedirs(raw, exist_ok=True)


def put_raw(name, obj):
    # ensure_ascii=True（默认）才写得进落单代理码点：它落成 \ud800 转义，读回来还是脏值。
    with open(os.path.join(raw, name), "w", encoding="utf-8") as fh:
        json.dump(obj, fh)


put_raw("a.json", {"images": [
    {"url": "http://x/1.jpg", "file": "good_01.jpg"},
    {"url": "http://x/2.jpg", "file": "bad\x00.jpg"},
    {"url": "http://x/3.jpg", "file": "bad" + HI + ".jpg"},
    {"url": "http://x/4.jpg", "file": "bad" + LO + ".jpg"},
    {"url": "http://x/5.jpg", "file": "ctl\x01.jpg"},
    {"url": "http://x/6.jpg", "file": "good_02.png"},
]})

tasks, skipped = D.collect_tasks(raw, IMG)
names = [os.path.basename(t[1]) for t in tasks]
chk("坏名字全被跳过，正常的照收", names == ["good_01.jpg", "good_02.png"], names)
chk("跳过条数如实记（4 条脏名字）", skipped == 4, skipped)
chk("清单里没有任何坏落点",
    all(D.safe_target(IMG, os.path.basename(t[1])) is not None for t in tasks), names)

# 整批真跑一遍：坏记录被挡在清单外之后，一条脏名字不该让其余图抛出来 ——
# 这正是 main() 里 `fut.result()` 当初翻车的位置。
calls = []
results = []
with patched_urlopen(fake_urlopen(PNG, calls)):
    for url, fpath in tasks:
        results.append(D.download_one(url, fpath, retries=1))
chk("整批下完，全部 ok", results == ["ok", "ok"], results)
chk("只对清单里的合法落点发请求", len(calls) == 2, calls)
chk("两张图都真落了盘", all(os.path.exists(f) for _u, f in tasks))

# ── 5. download_one 被直接塞坏路径：回 fail，不许抛 ─────────────
for weird in ["direct\x00.png", "direct" + HI + ".png"]:
    with patched_urlopen(fake_urlopen(PNG)):
        try:
            r = D.download_one("http://x/d.png", os.path.join(IMG, weird), retries=1)
        except Exception as e:                # noqa: BLE001 - 要的就是不抛
            r = "RAISED %r" % e
    chk("download_one 坏路径 %r 回 fail:…，不抛" % (weird,),
        isinstance(r, str) and r.startswith("fail:"), r)

shutil.rmtree(SB, ignore_errors=True)

print("\n插图文件名脏值体检：%d 项通过，失败 %d 项" % (N - len(FAIL), len(FAIL)))
for n in FAIL:
    print("  x " + n)
sys.exit(1 if FAIL else 0)
