#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""便签记忆画像落盘（`flomo_notes.write_portrait`）：临时件不许共用固定名。

为什么单开这一条
    便签账本那格（`flomo_notes.save`，`#152`）已经收干净了，画像这一格当时留在原地：
    它写两份文件（`portrait.json` 给机器、`portrait.md` 给人 / Agent），两份各自
    共用一个固定名的临时件（`portrait.json.tmp` / `portrait.md.tmp`）。

为什么这一格真够得着并发
    后端是 `ThreadingHTTPServer`，而画像有两只手，都在请求线程上：

      · 界面那次「生成画像」——`/api/flomo/notes?mode=portrait`（`ui_server.py:4624`，
        那里直接调 `flomo_notes.write_portrait`）；
      · Agent 那次「先读画像再动手」——MCP 的 `flomo_portrait` 工具打同一个路由，
        而 `read_portrait()` 在 `portrait.json` 还不存在时会现算一份并落盘
        （README 给 Agent 的叮嘱就是「和笔记有关的事先调一次 flomo_portrait」）。

    用料不同（一次带着界面手上的 `data`、一次从盘上重读），但落点是同两份文件。
    固定临时名的两种代价与账本那格一模一样：先收尾的 `os.replace` 把共用的件搬走，
    后一个写手当场 `FileNotFoundError`（请求带异常出去）；或者后来者把前一人写了
    一半的件截断，顶成正式文件的是半截 JSON / 拼接过的 Markdown。现在两份文件都走
    `_tmp_name`（pid + 随机串），写完原子顶替，`except BaseException` 先把残件
    收干净再抛。

这一条钉五面：
  A. 正常写：每次两份临时件、名字带进程号与随机串、两次写不撞名，写完全目录无残件；
  B. 对照：两个写手共用同一个固定名时，后一个 `os.replace` 当场 `FileNotFoundError`
     —— 证明这道闸不是空转；
  C. 写失败：照旧抛（含 `KeyboardInterrupt` 这类中止信号）、临时件收干净、旧档
     逐字节不动；
  D. 旁证：六个线程同时问同一份画像，没有一个写手抛异常、两份文件都解得动、
     不留 `.tmp`；
  E. 接线：源码里那句固定名真的没了，且「两只手」都还在（不是假想并发）。

跑法：纯函数 + 线程 + 系统临时目录沙盒，不联网、不起服务、不开浏览器，归 --fast。
"""
import builtins
import json
import os
import pathlib
import re
import shutil
import sys
import tempfile
import threading

HERE = pathlib.Path(__file__).resolve().parent
REPO = HERE.parent
sys.path.insert(0, str(REPO))

import flomo_notes as F  # noqa: E402

FAIL = []
PASSED = [0]


def chk(name, cond, extra=""):
    if cond:
        PASSED[0] += 1
        print("  ok  " + name)
    else:
        FAIL.append(name)
        print(" FAIL " + name + (("  <- " + repr(extra)[:220]) if extra else ""))


def mk():
    return tempfile.mkdtemp(prefix="guizang-portrait-")


def tmp_left(d):
    return sorted(n for n in os.listdir(d) if n.endswith(".tmp")) if os.path.isdir(d) else []


def capture_tmps(fn):
    """跑一趟 fn，把它以写模式打开的 `.tmp` 件名记下来（只要 basename）。"""
    seen = []
    real = builtins.open

    def spy(file, mode="r", *a, **kw):
        try:
            name = os.fspath(file)
        except TypeError:
            name = str(file)
        if any(c in str(mode) for c in "wxa") and str(name).endswith(".tmp"):
            seen.append(os.path.basename(str(name)))
        return real(file, mode, *a, **kw)

    builtins.open = spy
    try:
        fn()
    finally:
        builtins.open = real
    return sorted(seen)


DOC = {"version": 1, "memos": [
    {"id": "m1", "md": "#读书 一句", "tags": ["读书"], "ts": 1792000000},
    {"id": "m2", "md": "#灵感 又一句", "tags": ["灵感"], "ts": 1792003600},
]}

# ── A. 正常写：两份文件、一次一个名字 ────────────────────────────────
print("── A. 正常写：两份文件、一次一个名字 ──────────────────")
d = mk()
wrote = F.write_portrait(d, DOC)
pdir = F.portrait_dir(d)
chk("A1 画像写成功：两份文件都在",
    os.path.isfile(os.path.join(pdir, "portrait.json"))
    and os.path.isfile(os.path.join(pdir, "portrait.md")), wrote)
t1 = capture_tmps(lambda: F.write_portrait(d, DOC))
t2 = capture_tmps(lambda: F.write_portrait(d, DOC))
chk("A2 一次写两份文件，各用一个临时件（JSON 与 MD 都要）",
    len(t1) == 2 and len(t2) == 2, (t1, t2))
chk("A3 临时件与正式文件分开、都以 .tmp 收尾",
    all(not n.endswith(".json") and not n.endswith(".md") and n.endswith(".tmp")
        for n in t1), t1)
chk("A4 两次写的临时件名各不一样，且带着自己的进程号",
    bool(t1) and bool(t2) and set(t1) != set(t2)
    and all(str(os.getpid()) in n for n in t1), (t1, t2))
chk("A5 进程号后面还跟着一串随机（同进程里两次也不撞）",
    all(re.search(r"\.%d\.[0-9a-f]{6,}\.tmp$" % os.getpid(), n) for n in t1), t1)
chk("A6 写完之后画像目录里没有 .tmp 残件", tmp_left(pdir) == [], tmp_left(pdir))
doc = json.load(open(os.path.join(pdir, "portrait.json"), encoding="utf-8"))
chk("A7 落盘的 JSON 解得动，条数与账上一致", doc.get("memos") == 2, doc.get("memos"))
md = open(os.path.join(pdir, "portrait.md"), encoding="utf-8").read()
chk("A8 Markdown 是完整一份（开头标题 + 末尾那句诚实声明）",
    md.startswith("# 我的记忆画像") and md.rstrip().endswith("不含任何一条笔记原文。"),
    md[-60:])
shutil.rmtree(d, ignore_errors=True)

# ── B. 对照：共用固定临时件名，后一个 replace 当场抛 ────────────────
print("── B. 对照：共用固定名会当场抛 ────────────────────────")
d = mk()
pdir = F.portrait_dir(d)
fixed = os.path.join(pdir, "portrait.md.tmp")   # 旧写法：两处共用的那一个名字
with open(fixed, "w", encoding="utf-8") as f1:
    f1.write("甲写了一半的 Markdown")
with open(fixed, "w", encoding="utf-8") as f2:
    f2.write("# 乙写的完整一份")
    os.replace(fixed, os.path.join(pdir, "portrait.md"))   # 乙先收尾，把共用的件搬走
lost = None
try:
    os.replace(fixed, os.path.join(pdir, "portrait.md"))   # 甲轮到自己那一句
except FileNotFoundError:
    lost = "FileNotFoundError"
chk("B1 对照：共用 .tmp 时后一个写手的 os.replace 会当场 FileNotFoundError"
    "（旧写法真会让其中一只手「点了没反应」）", lost == "FileNotFoundError", lost)
shutil.rmtree(d, ignore_errors=True)

# ── C. 写失败：照旧抛、残件收干净、旧档不动 ─────────────────────────
print("── C. 写失败：收干净、旧档不动 ────────────────────────")
d = mk()
F.write_portrait(d, DOC)
pdir = F.portrait_dir(d)
jpath = os.path.join(pdir, "portrait.json")
mpath = os.path.join(pdir, "portrait.md")
before_j, before_m = open(jpath, "rb").read(), open(mpath, "rb").read()

real_replace = os.replace


def boom_factory(exc, fail_at):
    """装一个「第 fail_at 次 replace 才炸」的假 replace。"""
    state = {"n": 0}

    def boom(src, dst, *a, **kw):
        state["n"] += 1
        if state["n"] == fail_at:
            raise exc()
        return real_replace(src, dst, *a, **kw)
    return boom


# C1/C2：JSON 那一份改名时抛 OSError —— 半截临时件必须收掉，旧档一个字不动。
os.replace = boom_factory(OSError, 1)
raised = None
try:
    F.write_portrait(d, DOC)
except BaseException as e:          # noqa: BLE001 —— 任何异常都算「照旧抛」
    raised = type(e).__name__
finally:
    os.replace = real_replace
chk("C1 画像：真写不进去时照旧抛（不吞）", raised is not None, raised)
chk("C2 画像：写崩后没有 .tmp 残件", tmp_left(pdir) == [], tmp_left(pdir))
chk("C3 画像：写崩不毁旧档（两份都逐字节还在）",
    open(jpath, "rb").read() == before_j and open(mpath, "rb").read() == before_m,
    (open(jpath, "rb").read()[:40], open(mpath, "rb").read()[:40]))

# C4：第二份文件（Markdown）改名时抛 KeyboardInterrupt —— 中止信号不继承 Exception，
# 只兜 `except Exception` 会在退出路径上漏一件，这里专门钉 BaseException。
os.replace = boom_factory(KeyboardInterrupt, 2)
raised = None
try:
    F.write_portrait(d, DOC)
except KeyboardInterrupt:
    raised = "KeyboardInterrupt"
except BaseException as e:          # noqa: BLE001
    raised = type(e).__name__
finally:
    os.replace = real_replace
chk("C4 画像：第二份改名时收到中止信号（KeyboardInterrupt），照旧抛",
    raised == "KeyboardInterrupt", raised)
chk("C5 画像：JSON 已经顶替、MD 没顶替时，半截 .tmp 也收干净了",
    tmp_left(pdir) == [], tmp_left(pdir))
chk("C6 画像：旧档在改名失败后仍是完整的一份 JSON",
    isinstance(json.load(open(jpath, encoding="utf-8")), dict), open(jpath, encoding="utf-8").read()[:60])
shutil.rmtree(d, ignore_errors=True)

# ── D. 旁证：六个线程同时问同一份画像 ───────────────────────────────
print("── D. 旁证：六线程并发写 ──────────────────────────────")
d = mk()
errs = []
N = 6
bar = threading.Barrier(N)


def worker(i):
    try:
        bar.wait(timeout=10)
        for _k in range(3):
            # 六只手喂的是同一份料（画像由账本算出来，各请求看到的自然是同一份），
            # 这样 JSON 与 MD 之间不会因为「两次不同的算」而本来就不一致。
            F.write_portrait(d, DOC)
    except Exception as e:          # noqa: BLE001
        errs.append("%s: %s" % (type(e).__name__, e))


ts = [threading.Thread(target=worker, args=(i,)) for i in range(N)]
for t in ts:
    t.start()
for t in ts:
    t.join(timeout=30)
chk("D1 画像：六个线程并发写，没有一个写手抛异常", errs == [], errs[:3])
pdir = F.portrait_dir(d)
chk("D2 画像：并发之后没有 .tmp 残件", tmp_left(pdir) == [], tmp_left(pdir))
ok_parse = False
try:
    doc = json.load(open(os.path.join(pdir, "portrait.json"), encoding="utf-8"))
    md = open(os.path.join(pdir, "portrait.md"), encoding="utf-8").read()
    # 两份都必须是完整的那一版：JSON 解得动、MD 首尾俱在。
    ok_parse = (doc.get("memos") == 2
                and md.startswith("# 我的记忆画像")
                and md.rstrip().endswith("不含任何一条笔记原文。"))
except Exception as e:              # noqa: BLE001
    ok_parse = "%s: %s" % (type(e).__name__, e)
chk("D3 画像：最后落地的两份都完整（不是两份内容交织的半截）",
    ok_parse is True, ok_parse)
shutil.rmtree(d, ignore_errors=True)

# ── E. 接线：固定名那句真没了，两只手真都在 ─────────────────────────
print("── E. 接线 ────────────────────────────────────────────")
src = (REPO / "flomo_notes.py").read_text(encoding="utf-8")
body = src.split("def write_portrait", 1)[-1].split("\ndef ", 1)[0]
chk("E1 接线：write_portrait 里不再有 `name + \".tmp\"` 那句固定名",
    'name + ".tmp"' not in body, [l for l in body.splitlines() if ".tmp" in l][:3])
chk("E2 接线：两份文件都走 _tmp_name（同一个循环里一次一个名字）",
    "_tmp_name(" in body and "for name, text in" in body
    and "os.getpid()" in src.split("def _tmp_name", 1)[-1]
    and "uuid.uuid4().hex" in src.split("def _tmp_name", 1)[-1], body[:200])
chk("E3 接线：失败路径兜的是 BaseException（中止信号也要收残件）",
    "except BaseException" in body, body[-260:])
u_src = (REPO / "ui_server.py").read_text(encoding="utf-8")
chk("E4 接线：界面那次「生成画像」真的会写盘（mode=portrait → write_portrait）",
    "flomo_notes.write_portrait(" in u_src, u_src.count("write_portrait("))
chk("E5 接线：read_portrait 在文件不存在时会现算并落盘 —— 第二只手不是假想",
    "return write_portrait(flomo_dir)" in src)

print()
if FAIL:
    print("✗ check_flomo_portrait_atomic：%d 条没过" % len(FAIL))
    for n in FAIL:
        print("   - " + n)
    sys.exit(1)
print("✓ check_flomo_portrait_atomic：%d 条全过" % PASSED[0])
