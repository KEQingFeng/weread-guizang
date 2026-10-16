#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""归藏门禁：用户能手改的两处读口也要认 BOM（剪藏补 meta / 插图清单）。

为什么单开这一条
    「记事本另存为 UTF-8 会多塞三字节 BOM」只坏在**读**那一侧：写口一律照旧
    `utf-8`，读口一律 `utf-8-sig`。这一族此前逐轮收口，第十七轮结束后审计时
    当场数出**还剩两处**（不在任何 PR 的报告里，见《交接说明》§2）：

      ① `clip_article.save_clip` —— 它把刚由 `book_import` 建好的 `meta.json`
         **内联**读回来补剪藏专属字段，用的是裸 `utf-8`，外面罩
         `except Exception: meta = {}`；紧接着 `meta.update(...)` 再
         `_write_json_atomic` 写回去。带 BOM 时读回空 dict，等于**以空账为底
         重写那份 meta**，用户改过的其它字段整份丢掉。同模块的
         `book_layout.read_meta` 早就是 `utf-8-sig`，收口就是改走它，不再补
         第三份读法。
      ② `download_images.collect_tasks` —— 扫 `raw/*.json` 列插图任务那份是裸
         `utf-8`，解不动只 `skipped += 1`；带 BOM 的页面整份被当坏记录，那一页
         的插图一张都不重下，界面上只少几张图、看不出为什么。它与 `#175` 收的
         `raw` 账同形，但是**另一趟扫目录**，不在那条 PR 的 diff 里。

    两处都够不着 crash、够得着静默丢数据，所以判据是「带 BOM 与干净档逐字等价」，
    而不是「不抛就行」。

这一条钉住
  · 对照：裸 `utf-8` 读带 BOM 的 JSON 当场抛 —— 这两处原先就是这么读的；
  · 剪藏：`meta.json` 被手改过（带 BOM + 多一个字段）时，`save_clip` 补上剪藏
    字段且**不冲掉**用户那个字段；坏档 / 缺档照旧当空 dict、不抛；
  · 行为级接线：`save_clip` 真的走了 `book_layout.read_meta`（装间谍记录调用，
    不靠源码字面量 —— 字面量挡不住「改走另一份读法」这类变异）；
  · 插图：带 BOM 的 `raw/*.json` 照旧列得出任务，真正坏掉的档仍旧只跳一条、
    不牵连 BOM 那一份；
  · 源码：`collect_tasks` 那句读口写的是 `utf-8-sig`。

全程在系统临时目录的沙盒里跑，不联网、不开浏览器、不碰真实书库，故归
`--fast` 静态段。
"""
import atexit
import io
import json
import os
import shutil
import sys
import tempfile
import unittest.mock as mock
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

SB = tempfile.mkdtemp(prefix="gz-read-bom-")
atexit.register(lambda: shutil.rmtree(SB, ignore_errors=True))
os.environ["GUIZANG_DATA"] = os.path.join(SB, "cache")
os.environ["GUIZANG_BOOKS"] = os.path.join(SB, "books")
os.makedirs(os.environ["GUIZANG_DATA"], exist_ok=True)
os.makedirs(os.environ["GUIZANG_BOOKS"], exist_ok=True)

import book_import as B  # noqa: E402
import book_layout  # noqa: E402
import clip_article as C  # noqa: E402
import download_images as D  # noqa: E402

FAIL = []
N = [0]


def chk(name, cond, extra=""):
    N[0] += 1
    print(("  ok  " if cond else " FAIL ") + name + ("" if cond else "  <- " + repr(extra)))
    if not cond:
        FAIL.append(name)


BOM = b"\xef\xbb\xbf"


def write_json_with_bom(path, obj):
    with open(path, "wb") as f:
        f.write(BOM + json.dumps(obj, ensure_ascii=False).encode("utf-8"))


def read_json(path):
    """干净读，utf-8-sig 解 BOM —— 门禁自己核对落盘内容用。"""
    with io.open(path, encoding="utf-8-sig") as f:
        return json.load(f)


def tmp_left(d):
    return sorted(n for _r, _ds, fs in os.walk(d) for n in fs if n.endswith(".tmp"))


# ── 1. 对照：裸 utf-8 读带 BOM 的档，当场解不动 ───────────────────────
CTL = os.path.join(SB, "bom_control.json")
write_json_with_bom(CTL, {"a": 1})
_bare = "did-not-raise"
try:
    with open(CTL, encoding="utf-8") as f:
        json.load(f)
except Exception as e:  # noqa: BLE001
    _bare = type(e).__name__
chk("对照：裸 utf-8 读带 BOM 的 JSON 当场抛（这两处原先就是这么读的）",
    _bare != "did-not-raise", _bare)
chk("对照：utf-8-sig 读得回同一份内容（BOM 不是坏数据）", read_json(CTL) == {"a": 1})


# ── 2. 剪藏：带 BOM 的 meta 不冲掉用户字段，剪藏字段照补 ──────────────
def fake_extract(url):
    return {"title": "带 BOM 的剪藏", "author": "作者乙", "site": "测试站",
            "date": "2026-10-16", "url": url, "cover": "",
            "words": 88, "markdown": "正文第一段。\n\n第二段。"}


_real_import = B.import_book
_real_read_meta = book_layout.read_meta


def import_then_edit_meta(out_dir, *a, **kw):
    """照常收书，再模拟「有人在这两步之间手改了这份 meta，另存为带 BOM」。"""
    info = _real_import(out_dir, *a, **kw)
    mp = os.path.join(info["dir"], "meta.json")
    doc = read_json(mp)
    doc["user_note"] = "用户自己加的一行"
    write_json_with_bom(mp, doc)
    return info


def import_then_break_meta(out_dir, *a, **kw):
    """收完书把 meta 敲成带 BOM 的半截档：缺档 / 坏档仍该当空 dict 往下走。"""
    info = _real_import(out_dir, *a, **kw)
    with open(os.path.join(info["dir"], "meta.json"), "wb") as f:
        f.write(BOM + b"{ not json")
    return info


CALLS = []


def spy_read_meta(book_dir):
    CALLS.append(book_dir)
    return _real_read_meta(book_dir)


with mock.patch.object(B, "import_book", side_effect=import_then_edit_meta), \
        mock.patch.object(book_layout, "read_meta", side_effect=spy_read_meta):
    info = C.save_clip(os.path.join(SB, "clips"), "http://example.com/bom",
                       extract_fn=fake_extract)

meta = read_json(os.path.join(info["dir"], "meta.json"))
chk("剪藏：带 BOM 的 meta 被正确读回（不是当空账丢掉）",
    meta.get("user_note") == "用户自己加的一行", meta)
chk("剪藏：剪藏专属字段照旧补上",
    meta.get("source") == "clip" and meta.get("url") == "http://example.com/bom"
    and meta.get("chars") == 88, meta)
chk("剪藏：整本书目录不留 .tmp", tmp_left(info["dir"]) == [], tmp_left(info["dir"]))
chk("接线：save_clip 真的走了 book_layout.read_meta（行为级，不靠源码字面量）",
    info["dir"] in CALLS, CALLS)

with mock.patch.object(B, "import_book", side_effect=import_then_break_meta):
    info2 = C.save_clip(os.path.join(SB, "clips_bad"), "http://example.com/bad",
                        extract_fn=fake_extract)
meta2 = read_json(os.path.join(info2["dir"], "meta.json"))
chk("剪藏：带 BOM 的坏 meta 照旧当空 dict、剪藏字段仍写进去、不抛",
    meta2.get("source") == "clip" and meta2.get("url") == "http://example.com/bad",
    meta2)
chk("剪藏：坏 meta 那条也 finish 得干净（不留 .tmp）", tmp_left(info2["dir"]) == [])


# ── 3. 插图清单：带 BOM 的 raw/*.json 照旧列得出任务 ──────────────────
RAW = os.path.join(SB, "book", "raw")
IMG = os.path.join(SB, "book", "images")
os.makedirs(RAW, exist_ok=True)
BOM_RAW = os.path.join(RAW, "ch1.json")
write_json_with_bom(BOM_RAW, {"images": [
    {"url": "https://img.example.com/1.jpg", "file": "ch0001_img01.jpg"},
    {"url": "https://img.example.com/2.jpg", "file": "ch0001_img02.jpg"},
]})
with open(os.path.join(RAW, "broken.json"), "w", encoding="utf-8") as f:
    f.write("{ 这不是 JSON")

tasks, skipped = D.collect_tasks(RAW, IMG)
names = [os.path.basename(t[1]) for t in tasks]
chk("插图：带 BOM 的 raw/*.json 照旧列出两张任务",
    names == ["ch0001_img01.jpg", "ch0001_img02.jpg"], (names, skipped))
chk("插图：真正坏掉的档仍旧只跳一条、不牵连 BOM 那一份", skipped == 1, skipped)

_old = "did-not-raise"
try:
    with open(BOM_RAW, encoding="utf-8") as f:
        json.load(f)
except Exception as e:  # noqa: BLE001
    _old = type(e).__name__
chk("对照：老读法（裸 utf-8）读这份带 BOM 的 raw，整页一张插图都列不出",
    _old != "did-not-raise", _old)


# ── 4. 源码：两处读口写的是 BOM 认得的读法 ────────────────────────────
src_dl = io.open(os.path.join(REPO, "download_images.py"), encoding="utf-8").read()
_dl_start = src_dl.find("def collect_tasks(")
_dl_seg = src_dl[_dl_start:src_dl.find("def main(", _dl_start)]
chk("源码：collect_tasks 的 raw 读口写的是 utf-8-sig",
    'encoding="utf-8-sig"' in _dl_seg, _dl_seg[:300])

src_clip = io.open(os.path.join(REPO, "clip_article.py"), encoding="utf-8").read()
_c_start = src_clip.find("def save_clip(")
_c_seg = src_clip[_c_start:src_clip.find("def clip_many(", _c_start)]
chk("源码：save_clip 走 read_meta、不再内联裸 utf-8 读 meta",
    "book_layout.read_meta(info[\"dir\"])" in _c_seg
    and 'open(mp, encoding="utf-8")' not in _c_seg, _c_seg[:400])

print()
print("读口认 BOM 收尾门禁：%d 项通过，失败 %d 项" % (N[0] - len(FAIL), len(FAIL)))
if FAIL:
    print("未过：" + " / ".join(FAIL))
sys.exit(1 if FAIL else 0)
