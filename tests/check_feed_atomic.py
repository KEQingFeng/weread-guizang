#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""订阅这一格的落盘临时件一次一个名字（feed.py）。

为什么单开这一条
    `feed.py` 原先有三处落盘共用固定的 `<目标> + ".tmp"`：

      · 账本 `_save()` —— `<数据目录>/feed.json`（刷新 / 标已读 / 打标签 / 删除 /
        OPML 导入这些请求线程都会整份写回）；
      · OPML 导出 `write_opml()` —— `<数据目录>/订阅.opml`；
      · 订阅收成书 `to_shelf()` 里那份 `meta.json`（同一个文件另有一个写口
        `book_import._write_json`，也是固定名）。

    固定名的代价有两种，都不体面：两个写手并着走时，先收尾的那个把共用的临时件
    整个搬走，后一个轮到自己那句 `os.replace` 已经扑空、当场 `FileNotFoundError`
    （界面「存不下」，可前一笔其实已经落盘）；更坏的一种是两人同时在写那一个件，
    后来者一打开就把它截断，两边各按自己的偏移往下写，最后顶成正式文件的是一份
    内容交织的稿子 —— 读口 `_load` 解不动就整份退回空账，用户看到的是「订阅源
    全没了」，而那本账是刚刚两个请求各自按规矩写的。

这一条钉住
  · 正常写：内容一个字不改、改名完成、不留 `.tmp`；
  · 名字一次一个：两次保存的临时件不同名，形状是 `<目标>.<pid>.<8 位随机>.tmp`，
    不再是固定的 `<目标>.tmp`；
  · 真两个线程并着写：两边都不抛、最后落地的账本一定解得动、不留残件；
  · 对照：共用同一个临时件名时，顶上去的不是任何一份完整的账，后一个 `os.replace`
    还会当场 `FileNotFoundError` —— 证明这道闸不是空转；
  · 写失败 / 中止信号（KeyboardInterrupt）：旧档逐字节不动，随机名残件也收干净；
  · 接线：三处都走 `_write_json` / `_write_text`，兜的是 `BaseException`。

全程离线、只写临时沙盒（`GUIZANG_DATA` 指进去），不联网、不开浏览器，归 --fast 静态段。
"""
import atexit
import builtins
import json
import os
import re
import shutil
import sys
import tempfile
import threading

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

# GUIZANG_DATA 必须在 import feed 之前设好：feed.py 导入时就把 DATA_DIR / STORE 定死了。
SANDBOX = tempfile.mkdtemp(prefix="gz-feed-atomic-")
atexit.register(lambda: shutil.rmtree(SANDBOX, ignore_errors=True))
os.environ["GUIZANG_DATA"] = os.path.join(SANDBOX, "data")
os.environ["GUIZANG_BOOKS"] = os.path.join(SANDBOX, "books")
os.makedirs(os.environ["GUIZANG_DATA"], exist_ok=True)
os.makedirs(os.environ["GUIZANG_BOOKS"], exist_ok=True)

import feed  # noqa: E402

FAIL = []
PASSED = [0]


def chk(name, cond, extra=""):
    if cond:
        PASSED[0] += 1
        print("  ok  " + name)
    else:
        FAIL.append(name)
        print("FAIL  " + name + ("" if not extra else "   <- " + str(extra)[:220]))


def tmps(folder=None):
    """沙盒目录里以 .tmp 收尾的残件（随机名也一样看得见）。"""
    folder = folder or os.environ["GUIZANG_DATA"]
    try:
        return sorted(n for n in os.listdir(folder) if n.endswith(".tmp"))
    except FileNotFoundError:
        return []


def read_store():
    with open(feed.STORE, encoding="utf-8") as f:
        return json.load(f)


def save_raw(data):
    """直接调 `_save`（原子写那一层），绕开 `_write` 那把进程内的锁。"""
    feed._save(data)


print("── A. 正常写：内容原样、改名完成、不留残件 ──────────────────")
DOC = {"feeds": [{"id": "f1", "url": "https://example.com/rss", "title": "例源"}],
       "entries": {}, "seen": {}, "settings": {"group": "默认"}}
save_raw(DOC)
chk("A1 账本落得下、读得回，内容一个字不改", read_store() == DOC, read_store())
chk("A2 正常路径不留 .tmp", tmps() == [], tmps())

# ── B. 名字一次一个：形状 + 两次不同名 ────────────────────────────
opened = []
_real_open = builtins.open


def _spy_open(file, mode="r", *a, **k):
    path = str(file)
    # 只记「写模式打开、以 .tmp 收尾」的那一个 —— 正常读账本不算。
    if "w" in str(mode) + str(k.get("mode", "")) and path.endswith(".tmp"):
        opened.append(path)
    return _real_open(file, mode, *a, **k)


builtins.open = _spy_open
try:
    save_raw(DOC)
    save_raw({"feeds": [], "entries": {}, "seen": {}, "settings": {}})
finally:
    builtins.open = _real_open

chk("B1 两次保存的临时件不同名（固定名会互相截断）",
    len(opened) == 2 and opened[0] != opened[1], opened)
chk("B2 名字不是固定的 `<目标> + .tmp`",
    all(not p.endswith(os.path.basename(feed.STORE) + ".tmp") for p in opened), opened)
_pat = re.compile(r"^%s\.%d\.[0-9a-f]{8}\.tmp$"
                  % (re.escape(feed.STORE), os.getpid()))
chk("B3 名字是 `<目标>.<pid>.<8 位随机>.tmp`",
    len(opened) == 2 and all(_pat.match(p) for p in opened), opened)

# ── C. 对照：共用同一个临时件名会写出什么 ────────────────────────
print("── C. 对照：共用同一个临时件名会写出什么 ────────────────────")
save_raw({"who": "上一份好的"})
good_before = open(feed.STORE, "rb").read()
PAY_A = {"who": "甲", "pad": "甲" * 80}
PAY_B = {"who": "乙", "pad": "乙" * 80}
# 与旧写法同一个序列化口径（ensure_ascii=False + indent=2），否则「哪一份」比不干净。
CA = json.dumps(PAY_A, ensure_ascii=False, indent=2).encode("utf-8")
CB = json.dumps(PAY_B, ensure_ascii=False, indent=2).encode("utf-8")
old = feed.STORE + ".tmp"          # 旧写法：两个写手共用的那一个名字
cut = max(1, len(CA) // 2)         # 甲写到一半，两份都够长这条对照才作数
fa = open(old, "wb")
fb = open(old, "wb")
fa.write(CA[:cut]); fa.flush()
fb.write(CB); fb.flush()           # 乙重开把甲那截截断、整份写
fa.write(CA[cut:]); fa.close()     # 甲从自己的偏移接着往下写
fb.close()
chk("C1 对照：临时件被两个写手交织的那阵，正式账本一字未动",
    open(feed.STORE, "rb").read() == good_before)
os.replace(old, feed.STORE)
raw = open(feed.STORE, "rb").read()
chk("C2 对照：共用 .tmp 时顶上去的不是任何一份完整的账（旧写法真会把账写坏）",
    raw not in (CA, CB), raw[:80])

shared = feed.STORE + ".tmp"
with open(shared, "w", encoding="utf-8") as f1:
    json.dump({"who": "甲"}, f1, ensure_ascii=False)
with open(shared, "w", encoding="utf-8") as f2:      # 乙也开工，还抢先顶替了
    json.dump({"who": "乙"}, f2, ensure_ascii=False)
    os.replace(shared, feed.STORE)
lost = None
try:
    os.replace(shared, feed.STORE)                   # 甲轮到自己那一句
except FileNotFoundError:
    lost = "FileNotFoundError"
chk("C3 对照：共用 .tmp 时后一个 replace 当场 FileNotFoundError（异常穿出回调）",
    lost == "FileNotFoundError", lost)

# ── D. 真两个线程并着写：都不抛、账本解得动、不留残件 ──────────────
print("── D. 真两个线程并着写 ──────────────────────────────────────")
ERRS = []


def hammer(who):
    def run():
        for i in range(120):
            try:
                feed._save({"who": who, "i": i, "pad": who * 300,
                            "feeds": [], "entries": {}, "seen": {}, "settings": {}})
            except BaseException as e:               # noqa: BLE001 —— 只记下来，别咽掉
                ERRS.append("%s: %r" % (who, e))
    return run


ts = [threading.Thread(target=hammer("甲")), threading.Thread(target=hammer("乙"))]
for t in ts:
    t.start()
for t in ts:
    t.join(timeout=60)
chk("D1 两个写手跑完都没抛（固定名会让后一个 replace 扑空）", ERRS == [], ERRS)
ok, why = True, ""
try:
    read_store()
except Exception as e:                               # noqa: BLE001
    ok, why = False, "%s: %s" % (type(e).__name__, e)
chk("D2 并写之后落地的账本一定解得动（不是两份交织）", ok, why)
chk("D3 并写之后不留 .tmp 残件", tmps() == [], tmps())

# ── E. 写失败 / 中止信号：照旧抛、残件收干净、旧档不动 ────────────
print("── E. 写失败 / 中止信号 ─────────────────────────────────────")
save_raw({"good": "旧的一份"})
before = open(feed.STORE, "rb").read()
raised = None
try:
    save_raw({"bad": {1, 2}})                        # set 不是 JSON 类型，必然写不进去
except TypeError:
    raised = "TypeError"
except Exception as e:                               # noqa: BLE001
    raised = type(e).__name__
chk("E1 真写不进去时照旧抛（不吞异常、不静默丢这一趟）", raised == "TypeError", raised)
chk("E2 写崩后随机名残件也收干净了", tmps() == [], tmps())
chk("E3 写崩不毁旧档（旧的一份逐字节还在）",
    open(feed.STORE, "rb").read() == before, open(feed.STORE, "rb").read()[:80])

# 中止信号：KeyboardInterrupt 不继承 Exception，只兜 Exception 会把它漏出退出路径。
_real_dump = feed.json.dump


def _boom(*a, **k):
    raise KeyboardInterrupt()


feed.json.dump = _boom
ki = None
try:
    save_raw({"during": "interrupt"})
except KeyboardInterrupt:
    ki = "KeyboardInterrupt"
finally:
    feed.json.dump = _real_dump
chk("E4 写一半来中止信号：照旧往外抛（不吞）", ki == "KeyboardInterrupt", ki)
chk("E5 中止信号路径也不留 .tmp 残件", tmps() == [], tmps())
chk("E6 中止信号不毁旧档",
    open(feed.STORE, "rb").read() == before, open(feed.STORE, "rb").read()[:80])

# ── F. OPML 导出：同一个原子写口 ──────────────────────────────────
print("── F. OPML 导出 ─────────────────────────────────────────────")
opml_path = os.path.join(os.environ["GUIZANG_DATA"], "订阅.opml")
p = feed.write_opml(opml_path, title="门禁订阅")
chk("F1 OPML 落得下、能解析、标题对", os.path.exists(p) and "<opml" in open(p, encoding="utf-8").read()
    and "门禁订阅" in open(p, encoding="utf-8").read(), p)
chk("F2 OPML 正常路径不留 .tmp", tmps() == [], tmps())

before_opml = open(p, "rb").read()
_real_replace = feed.os.replace
feed.os.replace = lambda *a, **k: (_ for _ in ()).throw(OSError("改名扑空"))
try:
    raised = None
    try:
        feed.write_opml(opml_path, title="换一份")
    except OSError:
        raised = "OSError"
    chk("F3 改名失败照旧抛", raised == "OSError", raised)
finally:
    feed.os.replace = _real_replace
chk("F4 改名失败后随机名残件收干净了", tmps() == [], tmps())
chk("F5 改名失败不毁旧 OPML", open(p, "rb").read() == before_opml)

# ── G. 接线：三处都走同一把尺子 ───────────────────────────────────
print("── G. 接线 ──────────────────────────────────────────────────")
SRC = open(os.path.join(REPO, "feed.py"), encoding="utf-8").read()
for bad in ('tmp = STORE + ".tmp"', 'tmp = p + ".tmp"', 'tmp = mp + ".tmp"'):
    chk("G1 固定名写法已消失：%s" % bad, bad not in SRC)
chk("G2 模块里有 `_tmp_name`（pid + 随机串）",
    "_tmp_name" in SRC and "uuid.uuid4().hex[:8]" in SRC and "os.getpid()" in SRC)
chk("G3 失败 / 中止路径收残件（`except BaseException` + `_discard`）",
    "except BaseException" in SRC and "_discard(tmp)" in SRC)
chk("G4 账本走 `_write_json(STORE, data)`", "_write_json(STORE, data)" in SRC)
chk("G5 OPML 走 `_write_text(p, export_opml(...))`",
    "_write_text(p, export_opml(title=title))" in SRC)
chk("G6 订阅收成书的 meta 走 `_write_json(mp, meta)`", "_write_json(mp, meta)" in SRC)
chk("G7 `_write_json` / `_write_text` 都兜 BaseException 收残件",
    SRC.count("except BaseException:") >= 2)

print("\n订阅落盘临时件体检：%d 项通过，失败 %d 项" % (PASSED[0], len(FAIL)))
for name in FAIL:
    print("  ✗ " + name)
sys.exit(1 if FAIL else 0)
