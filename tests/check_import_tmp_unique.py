#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""归藏门禁：导入 / 剪藏那份 meta 的临时件也收进「一次一个名字」这一族。

为什么要单开一条
    `book_import` 是「把一份文件收进书库」的唯一入口（导入本地书 / 剪藏 / 订阅
    收成书 / 视频重建 / 安娜下载全从它过），它给每本书写三份账：`meta.json` /
    `_catalog.json` / `_progress.json`。而 `clip_article.save_clip` 紧接着把
    `meta.json` 读回来补上剪藏专属字段（来源链接 / source=clip / 字数）再写一遍。
    这两个写口原先都是最老那一档：

        tmp = path + ".tmp"        # 固定名，没有随机串
        # …写…
        os.replace(tmp, path)      # 写一半崩了就把 meta.json.tmp 留在书目录里

    固定名的毛病在这一族别的口上都实测过：先收尾的那个 `os.replace` 把共用的
    临时件整个搬走，后一个写手轮到自己那句时件已经不在，当场 FileNotFoundError。
    而这条写口连失败清理都没有 —— `json.dump` 写到一半断掉（盘满 / 编码不进去）
    就在书目录里留一个只有磁盘看得见的 `meta.json.tmp`，界面上删不掉。

这一条钉住
  · 临时件名带 pid + 随机串，两次取的名字不一样、落在目标同一目录里；
  · `_write_json` / `_write_json_atomic` 正常写完不留 `.tmp`，内容完整；
  · 写崩（序列化不了）照旧抛，旧档逐字节不动、不留 `.tmp`；
  · 中止信号（KeyboardInterrupt 这条 `BaseException` 路径）也把临时件收干净再抛；
  · 多线程并写同一个口不抛、最后落地的账本一定解得动、不留残件；
  · 剪藏那份 meta 照旧摘掉落单的半个代理码点（保证没有为原子写丢掉旧收口）；
  · 端到端：`import_book` 与 `save_clip` 真落一本书，整本目录无 `.tmp`、剪藏字段在；
  · 对照：固定临时名在同一交错下确实会 `FileNotFoundError` —— 门禁不空转。

全程在系统临时目录的沙盒里跑，不联网、不开浏览器、不碰用户真实书库，故归
`--fast` 静态段。
"""
import atexit
import io
import json
import os
import shutil
import sys
import tempfile
import threading
import unittest.mock as mock
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

SB = tempfile.mkdtemp(prefix="gz-import-tmp-")
atexit.register(lambda: shutil.rmtree(SB, ignore_errors=True))
os.environ["GUIZANG_DATA"] = os.path.join(SB, "cache")
os.environ["GUIZANG_BOOKS"] = os.path.join(SB, "books")
os.makedirs(os.environ["GUIZANG_DATA"], exist_ok=True)
os.makedirs(os.environ["GUIZANG_BOOKS"], exist_ok=True)

import book_import as B  # noqa: E402
import clip_article as C  # noqa: E402

FAIL = []
N = [0]


def chk(name, cond, extra=""):
    N[0] += 1
    print(("  ok  " if cond else " FAIL ") + name + ("" if cond else "  <- " + repr(extra)))
    if not cond:
        FAIL.append(name)


def tmp_left(d):
    """整棵目录树里以 `.tmp` 收尾的文件（不管叫固定名还是随机名）。"""
    return sorted(n for _r, _ds, fs in os.walk(d) for n in fs if n.endswith(".tmp"))


# ── A. 临时件名：pid + 随机串，落在目标同一目录 ──────────────────────
WORK = os.path.join(SB, "work")
os.makedirs(WORK, exist_ok=True)
P = os.path.join(WORK, "meta.json")
t1, t2 = B._tmp_name(P), B._tmp_name(P)
chk("A1 名字以 .tmp 收尾", t1.endswith(".tmp") and t2.endswith(".tmp"), (t1, t2))
chk("A2 两次取的名字不一样（不是固定名）", t1 != t2, (t1, t2))
chk("A3 名字里带 pid 段", t1.startswith(P + "." + str(os.getpid()) + "."), t1)
chk("A4 与目标同目录（改名不跨文件系统）",
    os.path.dirname(t1) == os.path.dirname(P), (t1, P))

# ── B. 正常写：内容完整、不留残件 ────────────────────────────────────
B._write_json(P, {"title": "书", "n": 1})
chk("B1 正常写：内容完整落盘",
    json.load(io.open(P, encoding="utf-8")) == {"title": "书", "n": 1})
chk("B2 正常写：书目录里不留 .tmp", tmp_left(WORK) == [], tmp_left(WORK))

# ── C. 写崩：抛出去、旧档不动、不留残件 ──────────────────────────────
before = io.open(P, "rb").read()
try:
    B._write_json(P, {"bad": {1, 2, 3}})          # set 没法 json.dump
    raised = False
except Exception:
    raised = True
chk("C1 序列化不了照旧抛", raised)
chk("C2 写失败不留 .tmp 残件", tmp_left(WORK) == [], tmp_left(WORK))
chk("C3 写失败旧档逐字节不动", io.open(P, "rb").read() == before)

# ── D. 中止信号那条退出路径（BaseException）也收干净再抛 ────────────
with mock.patch.object(B.os, "replace", side_effect=KeyboardInterrupt):
    try:
        B._write_json(P, {"v": 2})
        aborted = False
    except KeyboardInterrupt:
        aborted = True
chk("D1 中止信号照旧抛出去（没被吞成返回值）", aborted)
chk("D2 中止路径也把临时件收干净", tmp_left(WORK) == [], tmp_left(WORK))
chk("D3 中止时旧档一个字没动", io.open(P, "rb").read() == before)

# ── E. 多线程并写同一个口：不抛、最后一定解得动 ─────────────────────
CPATH = os.path.join(WORK, "concurrent.json")
NTH = 40
barrier = threading.Barrier(NTH)
errs = []


def worker(i):
    try:
        barrier.wait(timeout=20)
        B._write_json(CPATH, {"i": i, "pad": "字" * 200})
    except Exception as e:                       # noqa: BLE001 —— 体检就是要看它抛不抛
        errs.append("%s: %s" % (type(e).__name__, e))


threads = [threading.Thread(target=worker, args=(i,)) for i in range(NTH)]
for t in threads:
    t.start()
for t in threads:
    t.join(timeout=30)
chk("E1 并写不抛（固定名这一栏会扑空）", errs == [], errs[:3])
try:
    doc = json.load(io.open(CPATH, encoding="utf-8"))
    parsed = isinstance(doc, dict) and 0 <= doc.get("i", -1) < NTH
except Exception:
    parsed = False
chk("E2 最后落地的账本一定解得动", parsed is True)
chk("E3 并写之后不留 .tmp 残件", tmp_left(WORK) == [], tmp_left(WORK))

# ── F. 剪藏那份 meta 的落盘口 ────────────────────────────────────────
CDIR = os.path.join(SB, "clip")
os.makedirs(CDIR, exist_ok=True)
CM = os.path.join(CDIR, "meta.json")
C._write_json_atomic(CM, {"title": "坏\ud800码", "site": "测试站"})
got = json.load(io.open(CM, encoding="utf-8"))
chk("F1 半个代理码点照旧被摘掉（没有为原子写丢掉旧收口）",
    got.get("title") == "坏码", got)
chk("F2 剪藏 meta 正常写不留 .tmp", tmp_left(CDIR) == [], tmp_left(CDIR))
before2 = io.open(CM, "rb").read()
try:
    C._write_json_atomic(CM, {"bad": object()})   # object 没法 json.dump
    raised2 = False
except Exception:
    raised2 = True
chk("F3 剪藏 meta 写失败照旧抛", raised2)
chk("F4 剪藏 meta 写失败不留 .tmp", tmp_left(CDIR) == [], tmp_left(CDIR))
chk("F5 剪藏 meta 写失败旧档不动", io.open(CM, "rb").read() == before2)
with mock.patch.object(C.os, "replace", side_effect=KeyboardInterrupt):
    try:
        C._write_json_atomic(CM, {"v": 3})
        aborted2 = False
    except KeyboardInterrupt:
        aborted2 = True
chk("F6 剪藏 meta 中止路径也收干净", aborted2 and tmp_left(CDIR) == [],
    (aborted2, tmp_left(CDIR)))

# ── G. 端到端：真收一本书 / 真剪一篇文章 ─────────────────────────────
i1 = B.import_book(os.path.join(SB, "imported"), "b.md",
                   "# 第一章\n\n正文内容。\n".encode("utf-8"), title="导入书")
chk("G1 导入书：meta.json 落盘、整本目录不留 .tmp",
    os.path.isfile(os.path.join(i1["dir"], "meta.json")) and tmp_left(i1["dir"]) == [],
    tmp_left(i1["dir"]))


def fake_extract(url):
    return {"title": "测试文章", "author": "作者甲", "site": "测试站",
            "date": "2026-10-16", "url": url, "cover": "",
            "words": 123, "markdown": "这是正文。\n\n第二段。"}


info = C.save_clip(os.path.join(SB, "clips"), "http://example.com/a",
                   extract_fn=fake_extract)
meta = json.load(io.open(os.path.join(info["dir"], "meta.json"), encoding="utf-8"))
chk("G2 剪藏入库：meta 补上了剪藏专属字段",
    meta.get("source") == "clip" and meta.get("url") == "http://example.com/a"
    and meta.get("chars") == 123, meta)
chk("G3 剪藏入库：整本书目录不留 .tmp", tmp_left(info["dir"]) == [], tmp_left(info["dir"]))
chk("G4 剪藏入库：章节目录与正文都在",
    os.path.isdir(os.path.join(info["dir"], "chapters"))
    and any(n.endswith(".md") for n in os.listdir(os.path.join(info["dir"], "chapters"))))

# ── H. 源码：两个写口都换成了独名 + BaseException 收残件 ────────────
BSRC = io.open(os.path.join(REPO, "book_import.py"), encoding="utf-8").read()
seg = BSRC[BSRC.find("def _write_json"):BSRC.find("def _make_book_dir")]
chk('H1 book_import：不再有固定名 `tmp = path + ".tmp"`',
    'tmp = path + ".tmp"' not in BSRC)
chk("H2 book_import：_write_json 走 _tmp_name + BaseException 收残件",
    "def _tmp_name(path)" in BSRC and "tmp = _tmp_name(path)" in seg
    and "except BaseException" in seg, seg[:200])
CSRC = io.open(os.path.join(REPO, "clip_article.py"), encoding="utf-8").read()
cseg = CSRC[CSRC.find("def _write_json_atomic"):CSRC.find("def save_clip")]
chk('H3 clip_article：不再有固定名 `tmp = mp + ".tmp"`',
    'tmp = mp + ".tmp"' not in CSRC)
chk("H4 clip_article：save_clip 的 meta 走 _write_json_atomic",
    "_write_json_atomic(mp, meta)" in CSRC and "except BaseException" in cseg, cseg[:200])

# ── I. 对照：固定临时名在同一交错下确实会扑空 ───────────────────────
ctl = os.path.join(SB, "control.json")
fixed = ctl + ".tmp"
io.open(fixed, "w", encoding="utf-8").write('{"who": 1}')   # 写手一
io.open(fixed, "w", encoding="utf-8").write('{"who": 2}')   # 写手二把临时件截断
os.replace(fixed, ctl)                                       # 写手一收尾，件被搬走
try:
    os.replace(fixed, ctl)                                   # 写手二收尾 → 扑空
    collided = False
except FileNotFoundError:
    collided = True
chk("对照：两个写手共用固定临时件名时，后一个 os.replace 确实扑空", collided)

print()
print("导入 / 剪藏落盘临时件门禁：%d 项通过，失败 %d 项" % (N[0] - len(FAIL), len(FAIL)))
if FAIL:
    print("未过：" + " / ".join(FAIL))
sys.exit(1 if FAIL else 0)
