#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""归藏门禁：写作快照（`writer.snapshot_now`）落盘不许留半截 `.md`。

为什么单开这一条
    写作这一格的三处写口（正文 / meta / 册账）已经由 `check_writer_atomic.py`
    收进 `writer._write_text_atomic`，可**快照**那一处漏在外面：原先还是裸
    `with open(path, "w", encoding="utf-8")` 直接写正式名。快照跟正文一样是用户
    亲手点出来的东西 —— 「存一份快照」会在这一秒多一个 `versions/…-123456.md`，
    `versions()` 列的就是目录里以 `.md` 收尾的文件，`restore()` 拿它整篇顶回正文。

    直写正式名有两个代价：

      · **半截 .md 会被当成一份快照**。写一半被强杀（关窗口 / 断电 / Ctrl-C）留下的
        那半篇，照样出现在快照列表里，看着和好快照一模一样。用户点「回退到这一版」，
        拿回来的是半篇稿 —— 更坏的是 `restore()` 在顶回之前还会先给自己拍一份快照，
        于是这段半篇正文又被钉进 `versions/` 里。
      · **同一秒里两只手抢同一个名字**。后端是 `ThreadingHTTPServer`：`save()` 每
        `REV_STEP_SNAP` 次自动拍一份（`snapshot()`），用户手点「存一份快照」走
        `/api/writer/snapshot`，两条线可能落在同一秒。旧的「先看文件在不在、再打开写」
        之间有一个窗口，两边都认下同一个 `…-123456.md`，各自按自己的偏移往下写，
        `os.replace` 不存在，顶上去的是两篇交织在一起的正文（本文件 C 段有对照）。

收口
    快照落盘也走 `_write_text_atomic` 这同一个口子：临时件带 pid + 一小串随机，
    `os.replace` 一步顶替，写失败先把临时件收干净再抛。顺手把版本额度改成只数
    `.md` —— 临时件在目录里只是过客，不该占掉一份真快照的名额，更不该被别人那一趟
    收尾时的清额度顺手删掉（正在写的那只手下一步就会 `FileNotFoundError`）。

钉七面
  A. 正常写：一份完整快照落进 `versions/`、读回来一字不差、不留临时件、列得出来；
  B. 对照：老写法直写正式名留下的半截 `.md`，`versions()` 会列出来、`restore()` 会
     真把它顶回正文 —— 这道闸不是空转；
  C. 对照：同一个正式名被两只手直写，顶上去的不是任何一份完整正文；
  D. 写一半被强杀（写入处抛 KeyboardInterrupt）：异常照旧往外抛、不留半截 `.md`、
     临时件收干净、先前那份好快照一字不动；
  E. 并发：六只手在同一秒各存一份，落地的每一份都是**某一份完整正文**（不是交织），
     没有写手抛异常、不留临时件；
  F. 版本额度：目录里躺着一个写一半留下的临时件时，真快照仍然只留 `KEEP_VERSIONS`
     份，且那个临时件不被这一趟收尾顺手删掉；
  G. 接线：`snapshot_now` 走的是 `_write_text_atomic`、裸 `with open(…, "w")` 真的没了、
     额度只数 `.md`，两只手（自动那一拍与手点那一颗）确实都在。

跑法：纯函数 + 线程 + 系统临时目录沙盒书库，不联网、不开浏览器、不起服务，归 --fast 静态段。
"""
import builtins
import os
import shutil
import sys
import tempfile
import threading

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import writer  # noqa: E402

FAIL = []
PASSED = [0]

# 两份长得像正文、又互相认得出的稿子：并发那一段要拿「落地的到底是哪一份」来判。
TEXT = "第一行\n第二行\n" + "正文很长的一段。" * 40
OTHER = "另一份完全不同的稿子\n" + "别的段落。" * 40


def chk(name, cond, extra=""):
    if cond:
        PASSED[0] += 1
        print("  ok  " + name)
    else:
        FAIL.append(name)
        print(" FAIL " + name + (("  <- " + repr(extra)[:220]) if extra else ""))


def leftover(vd):
    """目录里还留着几个 `.tmp` 残件。"""
    return sorted(n for n in os.listdir(vd) if n.endswith(".tmp"))


def read_all(vd):
    out = {}
    for n in sorted(os.listdir(vd)):
        if n.endswith(".md"):
            with open(os.path.join(vd, n), encoding="utf-8") as f:
                out[n] = f.read()
    return out


def draft(lib, title, text):
    """建一篇稿子，回 (book_id, 稿子目录, versions 目录)。"""
    bid, _meta = writer.create(lib, title, text)
    d = writer.dir_of(lib, bid)
    return bid, d, writer.versions_dir(d)


LIB = tempfile.mkdtemp(prefix="guizang-snap-")

# ── A. 正常写：一份完整快照 ─────────────────────────────────────────
print("── A. 正常写：一份完整快照 ─────────────────────────────")
bid, d, vd = draft(LIB, "快照稿", "起头的正文")
name = writer.snapshot_now(d, TEXT)
chk("A1 快照：文件名以 .md 收尾、真落在 versions/ 里",
    name.endswith(".md") and os.path.isfile(os.path.join(vd, name)), (name, sorted(os.listdir(vd))))
chk("A2 快照：读回来与写下去的一字不差", writer.version_text(d, name) == TEXT,
    (writer.version_text(d, name) or "")[:40])
chk("A3 快照：写完不留临时件", leftover(vd) == [], leftover(vd))
chk("A4 快照：versions() 列得出这一份（回退那条路看得见它）",
    name in [v["name"] for v in writer.versions(d)], [v["name"] for v in writer.versions(d)])

# ── B. 对照：老写法直写正式名，半截 .md 会被当成一份可回退的快照 ─────
print("── B. 对照：半截 .md 是一份「看起来正常」的快照 ────────")
bid2, d2, vd2 = draft(LIB, "半截稿", "好端端的正文" * 3)
HALF = "半篇" * 5
half_name = "20200101-000000.md"
with open(os.path.join(vd2, half_name), "w", encoding="utf-8") as f:
    f.write(HALF)                       # 老写法：直写正式名，写到这里被强杀
chk("B1 对照：半截 .md 出现在快照列表里（它看起来就是一份快照）",
    half_name in [v["name"] for v in writer.versions(d2)],
    [v["name"] for v in writer.versions(d2)])
ok, _out = writer.restore(LIB, bid2, half_name)
chk("B2 对照：点回退，稿子真被半篇顶掉 —— 这就是要堵的那一步",
    ok is True and writer.read_text(d2) == HALF, (ok, writer.read_text(d2)[:40]))

# ── C. 对照：同一个正式名被两只手直写 ───────────────────────────────
print("── C. 对照：同一个正式名两只手直写，交织成第三篇 ────────")
cdir = tempfile.mkdtemp(prefix="guizang-snap-c-")
cpath = os.path.join(cdir, "x.md")
f1 = open(cpath, "w", encoding="utf-8")
f1.write("AAAA")                        # 甲先开、写了一段
f2 = open(cpath, "w", encoding="utf-8")  # 乙也开同一个名字（截断）
f2.write("BBBB")
f1.write("CCCC")                        # 甲按自己的偏移接着写
f1.close()
f2.close()
with open(cpath, encoding="utf-8") as f:
    mixed = f.read()
chk("C1 对照：顶上去的既不是甲那一篇、也不是乙那一篇（旧写法的代价）",
    mixed not in ("AAAACCCC", "BBBB"), mixed)

# ── D. 写一半被强杀：不留半截 .md ───────────────────────────────────
print("── D. 写一半被强杀：不留半截 .md ───────────────────────")
bid3, d3, vd3 = draft(LIB, "中止稿", "旧正文")
good_name = writer.snapshot_now(d3, "先存一份好快照")
before = sorted(os.listdir(vd3))

real_open = builtins.open
hit = []


class _Half:
    """顶掉 `open(..., "w")` 的句柄：真写一半，退出时抛中止信号。"""

    def __init__(self, fh):
        self.fh = fh

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.fh.write("半截")
        self.fh.close()
        raise KeyboardInterrupt

    def write(self, s):
        return self.fh.write(s[: max(1, len(s) // 2)])


def _fake_open(file, mode="r", *a, **kw):
    if "w" in str(mode) and str(file).endswith(".tmp"):
        hit.append(os.path.basename(str(file)))
        return _Half(real_open(file, mode, *a, **kw))
    return real_open(file, mode, *a, **kw)


builtins.open = _fake_open
raised = None
try:
    writer.snapshot_now(d3, TEXT)
except BaseException as e:               # noqa: BLE001 —— 这一条就是要看它抛不抛
    raised = e
finally:
    builtins.open = real_open
after = sorted(os.listdir(vd3))
chk("D1 中止：临时件确实开了（这一趟不是空跑）", bool(hit), hit)
chk("D2 中止：异常照旧往外抛（不吞成「存好了」）", isinstance(raised, KeyboardInterrupt), raised)
chk("D3 中止：不留半截 .md（快照列表与写之前一样）", after == before, (before, after))
chk("D4 中止：临时件收干净", leftover(vd3) == [], leftover(vd3))
chk("D5 中止：先前那份好快照一字不动",
    os.path.isfile(os.path.join(vd3, good_name))
    and writer.version_text(d3, good_name) == "先存一份好快照",
    writer.version_text(d3, good_name))

# ── E. 并发：同一秒里几只手各存一份 ─────────────────────────────────
print("── E. 并发：同一秒里几只手各存一份 ─────────────────────")
bid4, d4, vd4 = draft(LIB, "并发稿", "底稿")
texts = ["并发第%d份\n" % i + ("段落%d。" % i) * 60 for i in range(6)]
errs = []


def one(t):
    try:
        writer.snapshot_now(d4, t)
    except BaseException as e:           # noqa: BLE001
        errs.append(repr(e))


ths = [threading.Thread(target=one, args=(t,)) for t in texts]
for t in ths:
    t.start()
for t in ths:
    t.join()
landed = read_all(vd4)
chk("E1 并发：没有一个写手抛异常", errs == [], errs[:3])
chk("E2 并发：不留临时件", leftover(vd4) == [], leftover(vd4))
chk("E3 并发：落地的每一份都是某一份完整正文（不是两份交织）",
    bool(landed) and all(b in texts for b in landed.values()),
    [(n, b[:16]) for n, b in landed.items()])
chk("E4 并发：至少落地一份", len(landed) >= 1, sorted(landed))

# ── F. 版本额度：只数真快照 ─────────────────────────────────────────
print("── F. 版本额度：只数真快照 ─────────────────────────────")
bid5, d5, vd5 = draft(LIB, "额度稿", "底稿")
for i in range(writer.KEEP_VERSIONS):
    with open(os.path.join(vd5, "20000101-%06d.md" % i), "w", encoding="utf-8") as f:
        f.write("旧快照 %d" % i)
stray = os.path.join(vd5, "00000000-000000.md.%d.deadbeef.tmp" % os.getpid())
with open(stray, "w", encoding="utf-8") as f:
    f.write("写一半被强杀留下的临时件")
newest = writer.snapshot_now(d5, "最新的一份")
mds = sorted(n for n in os.listdir(vd5) if n.endswith(".md"))
chk("F1 额度：真快照只留 KEEP_VERSIONS 份", len(mds) == writer.KEEP_VERSIONS, len(mds))
chk("F2 额度：最新那份在、最老那份被挤掉",
    newest in mds and "20000101-000000.md" not in mds, (newest, mds[:2]))
chk("F3 额度：临时件没被当成一份版本、也没被这一趟收尾顺手删掉（正在写的那只手还要用它）",
    os.path.isfile(stray), sorted(os.listdir(vd5))[:3])

# ── G. 接线 ────────────────────────────────────────────────────────
print("── G. 接线 ────────────────────────────────────────────")
with open(os.path.join(ROOT, "writer.py"), encoding="utf-8") as f:
    src = f.read()
body = src.split("def snapshot_now(", 1)[-1].split("\ndef ", 1)[0]
chk("G1 接线：快照落盘走 _write_text_atomic",
    '_write_text_atomic(path, text or "")' in body,
    [l.strip() for l in body.splitlines() if "_write_text_atomic" in l])
chk("G2 接线：snapshot_now 里不再有裸 `with open(…, \"w\")`",
    "with open(" not in body,
    [l.strip() for l in body.splitlines() if "with open(" in l])
chk("G3 接线：版本额度只数 .md", 'endswith(".md")' in body,
    [l.strip() for l in body.splitlines() if "endswith" in l])
with open(os.path.join(ROOT, "ui_server.py"), encoding="utf-8") as f:
    u_src = f.read()
chk("G4 接线：界面「存一份快照」这条路真的在（两只手不是假想）",
    "writer_mod.snapshot_now(" in u_src)
chk("G5 接线：自动那一拍快照也在（同一个目录、同一秒）", "snapshot(d, meta)" in src)

shutil.rmtree(LIB, ignore_errors=True)
shutil.rmtree(cdir, ignore_errors=True)

print()
print("写作快照门禁：通过 %d 项，失败 %d 项" % (PASSED[0], len(FAIL)))
for n in FAIL:
    print("   - " + n)
sys.exit(1 if FAIL else 0)
