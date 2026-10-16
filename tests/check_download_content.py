#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""插图下载的**内容**收口：过线不等于是一张图（download_images.py）。

`download_one` 原先只看长度：`if len(raw) >= min_bytes:` 就 `os.replace` 成正式
插图并回 `"ok"`。可 `url` 是从网页扒下来、用户还能手改的 `raw/*.json` 里的外部
数据，回给你的不一定是一张图：

    · 站点 404 / 登录超时 / WAF 拦截 —— 一页 HTML，几百字节到几十 KB；
    · 接口把错误当数据回 —— 一段 JSON（`{"error": …}`），同样过线。

这些字节过得了 1KB 那条线，于是被当成「下好了一张插图」：`main` 与
`export_precise.download_all_images` 的 ok 计数虚高（进度把失败报成成功），
`images/xxx.jpg` 里躺着的是一页 HTML，还会跟着 EPUB 打包与书库同步一起出去。
要到**下一轮**重跑时 `is_complete` 才按文件头认出它不是图、判定「没下好」重下 ——
也就是说，坏内容至少能在盘上活一整轮，而这一轮的进度说的是「下完了」。

修法是落盘前问两句：回包自称的 `Content-Type`（`content_type_of`）与文件头
（`head_is_image`）。真实回包一定带 headers，`text/html` / `application/json`
这类声明一票否决；声明是 `image/*` 却对不上文件头也否决。回包连 headers 接口都
没有（极简 / 非标准回包）时无从判断，退回改之前只看长度的口径 —— 这一档在这一条
里单独钉住，免得它悄悄变成「坏内容从这里溜过去」。

这一条钉住七件事：
  · 过线但不是图 → 回 `fail:not-image`（与「太小」的 `fail:empty` 分开，日志能看懂），
    正式路径与 `.part` 上什么都不留，但重试次数照用（不是一次就放弃）；
  · 三种「不是图」的声明都拦：`text/html`、`application/json`、`application/octet-stream`；
    连「真实回包但没写 Content-Type」也拦（文件头才是最后的证据）；
  · 自称 `image/*` 而文件头对不上 → 同样拦；
  · 拦截图是暂时的 → 第一次回 HTML、第二次回真图时，最终仍是 ok、落的也是真图；
  · 真图（PNG / JPEG / WebP / GIF）照旧 ok，带不带 Content-Type 都一样；
  · 没有 headers 接口的极简回包 → 退回只看长度（钉住这一档，不是让它偷偷漏）；
  · 对照：老写法（只看长度）确实把 HTML 写成了正式插图 —— 证明这道检查不是空转，
    也说明 `is_complete` 与 `download_one` 现在是同一把尺子。

网络层用记账的假 urlopen 顶掉，不起服务、不联网，故归 --fast 静态段。
"""
import contextlib
import json
import os
import shutil
import sys
import tempfile
import types
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
JPG = b"\xff\xd8\xff\xe0" + b"j" * 4000
WEBP = b"RIFF\x00\x00\x00\x00WEBP" + b"w" * 4000
GIF = b"GIF89a" + b"g" * 4000

# 过线、但不是图：一页「拦截」HTML 与一段错误 JSON。长度都远超 MIN_BYTES(500)。
HTML = b"<!DOCTYPE html><html><body>" + b"rate limited, please retry later. " * 40 + b"</body></html>"
JSON = b'{"error":"gone","detail":"' + b"x" * 800 + b'"}'
assert len(HTML) > D.MIN_BYTES and len(JSON) > D.MIN_BYTES


class FakeResp:
    """真 HTTP 回包的极简替身：有 read / geturl / headers（headers 是普通 dict，
    有 .get，问不到 Content-Type 就回 None，与 `http.client.HTTPMessage` 同形）。"""

    def __init__(self, body, ctype=""):
        self.body = body
        self.headers = {"Content-Type": ctype} if ctype else {}
        self._url = "http://img.example.com/x"

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self, n=-1):
        return self.body

    def geturl(self):
        return self._url


class BareResp:
    """没有 headers 接口的极简回包（门禁里某些假 urlopen 就长这样）。"""

    def __init__(self, body):
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self, n=-1):
        return self.body

    def geturl(self):
        return "http://img.example.com/x"


def fake_urlopen(body, calls=None, ctype=""):
    def _open(req, timeout=None):
        if calls is not None:
            calls.append(getattr(req, "full_url", req))
        return FakeResp(body, ctype)
    return _open


def bare_urlopen(body, calls=None):
    def _open(req, timeout=None):
        if calls is not None:
            calls.append(getattr(req, "full_url", req))
        return BareResp(body)
    return _open


def seq_urlopen(items, calls=None):
    """第 n 次请求回 items[n]（每次是 (body, ctype)）；最后一次之后一直回最后那一条。"""
    box = {"i": 0}

    def _open(req, timeout=None):
        if calls is not None:
            calls.append(getattr(req, "full_url", req))
        body, ctype = items[min(box["i"], len(items) - 1)]
        box["i"] += 1
        return FakeResp(body, ctype)
    return _open


@contextlib.contextmanager
def patched_urlopen(fn):
    saved = urllib.request.urlopen
    urllib.request.urlopen = fn
    try:
        yield
    finally:
        urllib.request.urlopen = saved


@contextlib.contextmanager
def no_sleep():
    """重试之间的 1.5s 等待与「下的对不对」无关，门禁里换掉，别白等。"""
    saved = D.time
    D.time = types.SimpleNamespace(sleep=lambda *_a: None)
    try:
        yield
    finally:
        D.time = saved


SB = tempfile.mkdtemp(prefix="guizang-imgcontent-")
IMG = os.path.join(SB, "images")
os.makedirs(IMG, exist_ok=True)


# ── 0. content_type_of：三档区分开 ──────────────────────────────────
chk("content_type_of：带参数的 Content-Type 只留类型、转小写",
    D.content_type_of(FakeResp(b"", "Text/HTML; charset=utf-8")) == "text/html")
chk("content_type_of：真回包但没写 Content-Type → 空串（不是 None）",
    D.content_type_of(FakeResp(b"", "")) == "")
chk("content_type_of：没有 headers 接口的极简回包 → None",
    D.content_type_of(BareResp(b"")) is None)


# ── 1. 对照：老写法确实把 HTML 写成了正式插图（洞是真的） ─────────────
def old_download(url, fpath, min_bytes=D.MIN_BYTES):
    """改之前 download_one 的形状：只看长度就改名、就回 ok。"""
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


old_target = os.path.join(IMG, "old.jpg")
with patched_urlopen(fake_urlopen(HTML, ctype="text/html")):
    old_r = old_download("http://img.example.com/old.jpg", old_target)
chk("对照：老写法对一页 HTML 回 ok，并把它写成 images/old.jpg",
    old_r == "ok" and open(old_target, "rb").read() == HTML, old_r)
chk("对照：这份「插图」的文件头确实不是图（与 is_complete 同一把尺子）",
    not D.head_is_image(open(old_target, "rb").read()[:12]))


# ── 2. download_one：过线但不是图 → fail:not-image，什么都不落 ────────
NOT_IMAGE = (
    ("HTML", HTML, "text/html"),
    ("JSON", JSON, "application/json"),
    ("二进制流", HTML, "application/octet-stream"),
    ("真回包但没写类型", HTML, ""),          # headers 接口在、只是没有 Content-Type
)
for label, body, ctype in NOT_IMAGE:
    tgt = os.path.join(IMG, "blocked_%s.jpg" % label)
    calls = []
    with no_sleep(), patched_urlopen(fake_urlopen(body, calls, ctype)):
        r = D.download_one("http://img.example.com/%s" % label, tgt, retries=2)
    chk("%s：回 fail:not-image（不是 ok、也不冒充「太小」）" % label,
        r == "fail:not-image", r)
    chk("%s：正式路径上不出现文件" % label, not os.path.exists(tgt))
    chk("%s：不留 .part" % label, not os.path.exists(tgt + ".part"))
    chk("%s：重试次数用满（不是一次就放弃）" % label, len(calls) == 2, calls)

# 自称 image/*，文件头却对不上（拦截页伪装成图）：同样拦
fake_img_tgt = os.path.join(IMG, "fake_img.jpg")
with no_sleep(), patched_urlopen(fake_urlopen(HTML, ctype="image/jpeg")):
    r = D.download_one("http://img.example.com/fake_img.jpg", fake_img_tgt, retries=1)
chk("自称 image/jpeg 却对不上文件头 → 照样 fail:not-image、不落盘",
    r == "fail:not-image" and not os.path.exists(fake_img_tgt), r)

# 刚好压线的非图也拦（边界不是「比 min_bytes 大」才算过线）
edge_tgt = os.path.join(IMG, "edge.jpg")
edge_body = b"<html>" + b"a" * (D.MIN_BYTES - len(b"<html>"))
chk("边界：正正好 MIN_BYTES 的 HTML 长度确实过线", len(edge_body) == D.MIN_BYTES)
with no_sleep(), patched_urlopen(fake_urlopen(edge_body, ctype="text/html")):
    r = D.download_one("http://img.example.com/edge.jpg", edge_tgt, retries=1)
chk("边界：压线的非图照样 fail:not-image、不落盘",
    r == "fail:not-image" and not os.path.exists(edge_tgt), r)


# ── 3. 真图照旧 ok，内容一字不差（带 / 不带 Content-Type 都行） ───────
for label, body, ctype in (("PNG", PNG, "image/png"), ("JPEG", JPG, "image/jpeg"),
                           ("WebP", WEBP, "image/webp"), ("GIF", GIF, "image/gif"),
                           ("没写类型的真图", PNG, "")):
    tgt = os.path.join(IMG, "real_%s" % label)
    with patched_urlopen(fake_urlopen(body, ctype=ctype)):
        r = D.download_one("http://img.example.com/%s" % label, tgt, retries=1)
    chk("%s：真图照旧 ok、内容一字不差" % label,
        r == "ok" and open(tgt, "rb").read() == body, r)


# ── 4. 没有 headers 接口的极简回包：退回只看长度（这一档明着钉住） ────
bare_tgt = os.path.join(IMG, "bare.jpg")
with patched_urlopen(bare_urlopen(HTML)):
    r = D.download_one("http://img.example.com/bare.jpg", bare_tgt, retries=1)
chk("极简回包（没有 headers 接口）：无从判断类型，退回只看长度 → ok 并落盘",
    r == "ok" and os.path.exists(bare_tgt), r)


# ── 5. 拦截页是暂时的：重试还能救回来 ────────────────────────────────
heal_tgt = os.path.join(IMG, "heal_retry.png")
calls = []
with no_sleep(), patched_urlopen(seq_urlopen([(HTML, "text/html"), (PNG, "image/png")], calls)):
    r = D.download_one("http://img.example.com/heal.png", heal_tgt, retries=3)
chk("重试：第一次回 HTML、第二次回真图 → 最终 ok 且落的是真图",
    r == "ok" and open(heal_tgt, "rb").read() == PNG, r)
chk("重试：确实发了两次请求（没有一次就放弃）", len(calls) == 2, calls)
chk("重试：落下的这份 is_complete 认可（一次下好，下一轮不必重下）",
    D.is_complete(heal_tgt))


# ── 6. 清单不受影响：能不能下表要在取回来才知道，不能提前把 URL 丢掉 ──
# collect_tasks 只按 scheme / 落点 / 去重筛；内容这一关在 download_one，
# 因为清单阶段还没发请求。这里钉住这个分工，别让人以为内容检查前移了。
raw = os.path.join(SB, "book", "raw")
os.makedirs(raw, exist_ok=True)
with open(os.path.join(raw, "ch1.json"), "w", encoding="utf-8") as fh:
    json.dump({"images": [{"url": "http://img.example.com/a.jpg", "file": "a.jpg"},
                          {"url": "http://img.example.com/b.json", "file": "b.jpg"}]}, fh)
tasks, skipped = D.collect_tasks(raw, os.path.join(SB, "book", "images"))
chk("清单阶段照收（内容好不好要取回来才知道）", len(tasks) == 2 and skipped == 0, (tasks, skipped))

shutil.rmtree(SB, ignore_errors=True)

print()
print("插图内容门禁（过线不等于是一张图）：%d 项通过，失败 %d 项" % (N - len(FAIL), len(FAIL)))
for n in FAIL:
    print("  x " + n)
sys.exit(1 if FAIL else 0)
