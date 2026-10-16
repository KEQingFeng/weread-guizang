#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""订阅这一格的落盘临时件一次一个名字（feed.py）。

为什么单开这一条
    `feed.py` 原先有三处落盘共用固定的 `<目标> + ".tmp"`：

      · 账本 `_save()` —— `<数据目录>/feed.json`。**同进程够不着并发、跨进程够得着**：
        那把 `RLock` 罩住的不止刷新，全仓 19 处 `_save(data)` 有 18 处落在 `@_locked`
        的函数里（标已读、收藏、打标签、删订阅、建分组、改设置都在内）、第 19 处在
        `_write` 自己的 `with _LOCK` 里，「读整份 → 改一笔 → 写整份」在同进程里是串着的；
        够得着的是 `feed.py` 那个 `__main__`（`python3 feed.py --refresh`）与界面各起
        一把进程内的锁、写的是同一个文件；
      · OPML 导出 `write_opml()` —— `<数据目录>/订阅.opml`。**这一处真够得着**：默认名
        固定、这一句不在任何锁里，而服务是 `ThreadingHTTPServer`，两次「导出 OPML」的
        请求各占一个线程（`ui_server.py:4326`）—— H 节就是照这个形状铺的；
      · 订阅收成书 `to_shelf()` 里那份 `meta.json`。**这一处够不着并发**：
        `book_import._make_book_dir` 每次带一段随机名，每收成一本书开的是新书目录，
        另一个写口 `book_import._write_json` 写的也是这一本自己的目录 —— 收进来是
        为了这一族只留一把尺子、残件只有一种形状，不是为了修一个现成的 crash。

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
  · 写口这一层两个线程并着写：两边都不抛、最后落地的账本一定解得动、不留残件
    （同进程的调用侧本来被 `_LOCK` 串着，这一节测的是 `_save` 落盘本身，见上面
    「账本」那一段的口径；D1 在固定名下会红，所以它不是空转）；
  · 对照：共用同一个临时件名时，顶上去的不是任何一份完整的账，后一个 `os.replace`
    还会当场 `FileNotFoundError` —— 证明这道闸不是空转；
  · 写失败 / 中止信号（KeyboardInterrupt）：旧档逐字节不动，随机名残件也收干净；
  · OPML 那一格真并发（H 节，六线程各导 40 趟同一个目标）：没有一个写手抛、落地的
    是一份解得动的完整 `<opml>`、不留残件，并且带同一节的固定名对照组；
  · 收成书那一格跑一趟真的 `to_shelf`（I 节）：这趟不抛、`meta.json` 的临时件也是
    随机名、那本书目录里 `source == "feed"`、整棵书库不留残件 —— 顺手把「这一格
    每次开的是新书目录、够不着并发」这个结论钉住（I5）；
  · 接线：三处都走 `_write_json` / `_write_text`，兜的是 `BaseException`。

H / I 两节是从并行提的 `#167` 里移植的（它探到了 OPML 并发与收成书的行为面，但把
账本的并发判成「`_save` 有锁串着、够不着」—— 本轮按源码核过：`_write` 那把 `RLock`
只盖住刷新 / 抓全文 / 清理，标已读、收藏、打标签、删订阅、建分组、改设置都是锁外裸
`_save`）。它的实现与 `#168` 收的是同一格同一把尺子，所以产品码取 `#168`。

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

# ── D. 写口两个线程并着写：都不抛、账本解得动、不留残件 ──────────────
# 这一节直接并着调 `feed._save`，绕开调用侧那把 `_LOCK`（同进程里 19 处 `_save`
# 全在锁内，本来撞不到一起）—— 钉的是临时件名字本身一次一个：固定名下 D1 会红
# （后一个 `os.replace` 扑空），随机名下两只手也顶不出交织的账。
print("── D. 写口两个线程并着写 ────────────────────────────────────")
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

# ── H. OPML 并发：六只同时进来的手导同一个目标 ─────────────────────
# 这一节是从 #167 移植的（它收的正是 #168 没探到的那一面）。OPML 导出的目标是
# 调用方给的，默认落 <数据目录>/订阅.opml —— 双击导出、两个窗口各导一次，写的就是
# 同一个路径；这一格是订阅三处写口里除账本之外唯一够得着并发的。
print("── H. OPML 并发（六线程导同一份）────────────────────────")
HD = tempfile.mkdtemp(prefix="gz-feed-atomic-opml-", dir=SANDBOX)
opml_shared = os.path.join(HD, "订阅.opml")
N = 6

# H1 对照组：把临时名写死成 `path + ".tmp"`（旧写法）。六个写手各把件写完、在栏前
#    会齐，然后各自 os.replace —— 只有第一个搬得走件，其余当场 FileNotFoundError。
#    这一条同时是「H 不是空转」的证明：旧写法确实会当场塌。
bar = threading.Barrier(N)
fixed_name = opml_shared + ".tmp"
control_errs = []


def control_one(i):
    try:
        with _real_open(fixed_name, "w", encoding="utf-8") as cf:
            cf.write("<opml>%d</opml>" % i)
        bar.wait(timeout=20)
        os.replace(fixed_name, opml_shared)
    except BaseException as e:          # noqa: BLE001
        control_errs.append(type(e).__name__)


ts = [threading.Thread(target=control_one, args=(i,)) for i in range(N)]
for t in ts:
    t.start()
for t in ts:
    t.join(timeout=60)
chk("H1 对照：共用一个固定临时名时，除先收尾那一个外全当场 FileNotFoundError",
    len(control_errs) == N - 1 and set(control_errs) == {"FileNotFoundError"},
    control_errs)

# H2–H5 正片：真代码，六个线程各导 40 趟同一个目标。
errs, bar2 = [], threading.Barrier(N)


def opml_worker(i):
    try:
        bar2.wait(timeout=20)
        for _k in range(40):
            feed.write_opml(opml_shared, title="归藏订阅-%d" % i)
    except BaseException as e:          # noqa: BLE001
        errs.append("%s: %s" % (type(e).__name__, e))


ts = [threading.Thread(target=opml_worker, args=(i,)) for i in range(N)]
for t in ts:
    t.start()
for t in ts:
    t.join(timeout=120)
chk("H2 正片：六线程同时导同一份 OPML，没有一个写手抛异常", errs == [], errs[:3])
landed_opml = ""
try:
    with _real_open(opml_shared, encoding="utf-8") as lf:
        landed_opml = lf.read()
except Exception as e:                  # noqa: BLE001
    landed_opml = "%s: %s" % (type(e).__name__, e)
chk("H3 正片：落地的那一份是某一版的完整开头（不是两版搅在一起）",
    landed_opml.startswith("<?xml") and "归藏订阅-" in landed_opml, str(landed_opml)[:80])
root_tag = None
try:
    import xml.etree.ElementTree as ET
    root_tag = ET.fromstring(landed_opml).tag
except Exception as e:                  # noqa: BLE001
    root_tag = "%s: %s" % (type(e).__name__, e)
chk("H4 正片：落地的那一份解得动，根元素是 <opml>", root_tag == "opml", root_tag)
chk("H5 正片：并发之后目录里没有 .tmp 残件", tmps(HD) == [], tmps(HD))
shutil.rmtree(HD, ignore_errors=True)

# ── I. 收成书那一格：真跑一趟 to_shelf（写的是新书目录里的 meta.json）────────
# 这一格按 #168 的原话是「还有一个写口在导入那一侧」，本轮核过：**够不着并发** ——
# book_import._make_book_dir 每次带一段 uuid4().hex[:8]，每收成一本书开的是新书目录。
# 所以这一节钉的不是「不撞车」，而是「这趟真跑得通、meta.json 走的是同一把尺子、
# 整棵书库里不留残件」：接线面（G6）只看源码，看不出 `to_shelf` 能不能真落一本书。
print("── I. 收成书：to_shelf 那一格实测 ───────────────────────")
ID = tempfile.mkdtemp(prefix="gz-feed-atomic-shelf-", dir=SANDBOX)
saved_store = feed.STORE
feed.STORE = os.path.join(ID, "feed.json")
FID, EID = "f" * 12, "e" * 12
save_raw({"feeds": [{"id": FID, "title": "例源", "url": "https://example.com/rss",
                     "site": "https://example.com", "kind": "rss", "group": ""}],
          "entries": {FID: [{"id": EID, "feed_id": FID, "title": "某篇",
                             "link": "https://example.com/a", "author": "甲",
                             "published": "2026-01-01",
                             "content_md": "正文一段，够 import_book 认出 Markdown。",
                             "has_content": True, "read": False, "starred": False,
                             "tags": [], "pushed": ""}]},
          "seen": {}, "settings": {}})
shelf_err, shelf_tmps, info = "", [], {}


def _spy(file, mode="r", *a, **k):
    path = str(file)
    if any(c in str(mode) for c in "wxa") and path.endswith(".tmp"):
        shelf_tmps.append(os.path.basename(path))
    return _real_open(file, mode, *a, **k)


books_root = os.path.join(ID, "books")
builtins.open = _spy
try:
    try:
        info = feed.to_shelf(EID, books_root)
    except BaseException as e:          # noqa: BLE001
        shelf_err = "%s: %s" % (type(e).__name__, e)
finally:
    builtins.open = _real_open
chk("I1 收成书：这趟没抛异常（正文走 content_md，不联网）", shelf_err == "", shelf_err)
chk("I2 收成书：meta.json 的临时件也带进程号与随机串",
    any(re.match(r"meta\.json\.\d+\.[0-9a-f]{6,}\.tmp$", n) for n in shelf_tmps),
    shelf_tmps[:4])
meta_file = os.path.join(info.get("dir", "") if isinstance(info, dict) else "", "meta.json")
meta_source = None
try:
    with _real_open(meta_file, encoding="utf-8") as mf:
        meta_source = json.load(mf).get("source")
except Exception as e:                  # noqa: BLE001
    meta_source = "%s: %s" % (type(e).__name__, e)
chk("I3 收成书：meta.json 落在这本书自己的目录里、source 是 feed",
    meta_source == "feed", meta_source)
stray = []
for base, _dirs, files in os.walk(books_root):
    stray += [os.path.join(base, fn) for fn in files if fn.endswith(".tmp")]
chk("I4 收成书：整棵书库目录里没有 .tmp 残件", stray == [], stray[:3])
# I5 把「这一格够不着并发」这个结论本身钉住：同一篇再收一次，落的是**另一个**书目录
# （`_make_book_dir` 每次带一段随机名）。这条红了就说明上面那段注释与 §2 那本账都该改写。
second_dir = None
try:
    second_dir = feed.to_shelf(EID, books_root).get("dir")
except BaseException as e2:             # noqa: BLE001
    second_dir = "%s: %s" % (type(e2).__name__, e2)
chk("I5 收成书：每次收的都是新书目录（同一篇再收一次落的是另一个目录）",
    bool(second_dir) and second_dir != info.get("dir"),
    (info.get("dir"), second_dir))
feed.STORE = saved_store
shutil.rmtree(ID, ignore_errors=True)

print("\n订阅落盘临时件体检：%d 项通过，失败 %d 项" % (PASSED[0], len(FAIL)))
for name in FAIL:
    print("  ✗ " + name)
sys.exit(1 if FAIL else 0)
