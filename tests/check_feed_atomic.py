# -*- coding: utf-8 -*-
"""订阅这条线落盘临时件一次一个名字（`feed._save` / `write_opml` / `to_shelf` 的 meta.json）。

为什么这一格要单独钉
    《交接说明》§2 那本账把「固定临时名一族」从 19 处一路收到 11 处，订阅这一格一直
    挂在名单上（`feed._save` / `write_opml` / `to_shelf` 三处）。全项目同一把尺子：
    临时件名带进程号 + 一小串随机（样板 `book_notes._tmp_name`），写完原子顶替，
    写崩了先把残件收干净再抛。这一条把订阅这三处也钉住。

哪一处真够得着并发（写清楚免得下一个人以为是凑数）
    · `write_opml` —— 够得着。`ui_server` 是 `ThreadingHTTPServer`，导 OPML 就是
      一个普通请求（`act=opml_write`，导出后还能「存成文件」）；同一个路径两次导出
      落在同一个固定临时名上，先收尾的那个 `os.replace` 把件整个搬走，后一个轮到
      自己那一句就 `FileNotFoundError` —— 界面「OPML 没写出去」，可前一份明明写出去了。
      这一条的 D / F 两段就是照着这个形状做的对照与复跑。
    · `_save` —— 进程内由 `_LOCK` 串起来，够不着并发；跟着随机化是为了让
      「强杀留下的那半截件」长得有形状（清残稿按 `名字.数字.随机串.tmp` 认），
      而且这一族里不该有人还留着固定名。
    · `to_shelf` 那份 `meta.json` —— 每收一次都开一本新目录（book id 带随机 hex），
      同一个路径撞不上；同样是跟着这一族收口。

这一条钉七面：
  A. `_save`：临时件名两次不一样、带进程号与随机串、以 `.tmp` 收尾，写完全目录无残件；
  B. `write_opml`：同上，写出来的是能解析的 `<opml>`；
  C. `to_shelf`：那份 `meta.json` 也走同一把临时名，落盘后整本书目录里没有 `.tmp`；
  D. 对照：两个写手共用同一个固定临时件名时会当场 `FileNotFoundError`，顶上去的还是
     半截那一份（证明这道闸不是空转）；
  E. 写失败：照旧抛、残件收干净、旧档逐字节不动（`_save` 与 `write_opml` 各一份）；
  F. 并发：六个写手都先把件写完再会齐，然后各收各的尾 —— 对照组（写死固定名）除先
     收尾那一个外全当场 `FileNotFoundError`，正片（真代码）没有一个写手抛异常、
     最后落地的那份解得动。两段都证明这一闸不是空转；
  G. 接线：源码里那三句固定名真的没了，`_tmp_name` 走进程号 + uuid，三处写口都接了。

跑法：纯函数 + 线程 + 系统临时目录沙盒（GUIZANG_DATA 指向沙盒，绝不碰用户真实
数据目录），不联网、不起服务、不开浏览器，归 --fast 静态段。
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
import xml.etree.ElementTree as ET
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

# GUIZANG_DATA 必须在 import feed 之前设好：feed.py 在导入时就把 DATA_DIR / STORE
# 定下来了，设晚了它还是往真实数据目录写。
SANDBOX = tempfile.mkdtemp(prefix="gz-feed-atomic-")
atexit.register(lambda: shutil.rmtree(SANDBOX, ignore_errors=True))
os.environ["GUIZANG_DATA"] = os.path.join(SANDBOX, "data")
os.makedirs(os.environ["GUIZANG_DATA"], exist_ok=True)

import feed  # noqa: E402

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
    d = tempfile.mkdtemp(prefix="gz-feed-atomic-d-", dir=SANDBOX)
    return d


def tmp_left(d):
    return sorted(n for n in os.listdir(d) if n.endswith(".tmp")) if os.path.isdir(d) else []


def tmp_left_deep(root):
    out = []
    for base, _dirs, files in os.walk(root):
        for fn in files:
            if fn.endswith(".tmp"):
                out.append(os.path.join(base, fn))
    return sorted(out)


def capture_tmps(fn):
    """跑一趟 fn，把它以写模式打开的临时件名字记下来。"""
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
    return seen


SHAPE = r"\.%d\.[0-9a-f]{6,}\.tmp$" % os.getpid()


# ── A. `_save`：临时件一次一个名字 ──────────────────────────────────
print("── A. _save：临时件一次一个名字 ───────────────────────")
d = mk()
saved_store = feed.STORE
feed.STORE = os.path.join(d, "feed.json")
DOC = {"feeds": [], "entries": {}, "seen": {}, "settings": {}}
t1 = capture_tmps(lambda: feed._save(DOC))
t2 = capture_tmps(lambda: feed._save(DOC))
chk("A1 feed：写成功", len(t1) == 1, t1)
chk("A2 feed：临时件与正式文件分开，以 .tmp 收尾",
    bool(t1) and t1[0] != "feed.json" and t1[0].endswith(".tmp"), t1)
chk("A3 feed：两次写的临时件名不一样，且带着自己的进程号",
    bool(t1) and bool(t2) and t1[0] != t2[0] and str(os.getpid()) in t1[0], (t1, t2))
chk("A4 feed：进程号后面还跟着一串随机（同一进程里两次也不撞）",
    bool(re.search(SHAPE, t1[0] if t1 else "")), t1)
chk("A5 feed：写完之后没有 .tmp 残件", tmp_left(d) == [], tmp_left(d))
chk("A6 feed：落在盘上的还是能解开的那一份",
    json.load(open(feed.STORE, encoding="utf-8")) == DOC)
feed.STORE = saved_store

# ── B. `write_opml`：临时件一次一个名字 ─────────────────────────────
print("── B. write_opml：临时件一次一个名字 ──────────────────")
d = mk()
opml = os.path.join(d, "订阅.opml")
t1 = capture_tmps(lambda: feed.write_opml(opml))
t2 = capture_tmps(lambda: feed.write_opml(opml))
chk("B1 OPML：写成功", len(t1) == 1, t1)
chk("B2 OPML：临时件名带进程号与随机串",
    bool(t1) and str(os.getpid()) in t1[0] and re.search(SHAPE, t1[0]), t1)
chk("B3 OPML：两次写的临时件名不一样",
    bool(t1) and bool(t2) and t1[0] != t2[0], (t1, t2))
chk("B4 OPML：写完之后没有 .tmp 残件", tmp_left(d) == [], tmp_left(d))
root = None
try:
    root = ET.parse(opml).getroot().tag
except Exception as e:              # noqa: BLE001
    root = "%s: %s" % (type(e).__name__, e)
chk("B5 OPML：落盘的是能解析的 <opml>", root == "opml", root)

# ── C. `to_shelf` 那份 meta.json ────────────────────────────────────
print("── C. to_shelf：meta.json 也走同一把临时名 ────────────")
d = mk()
saved_store = feed.STORE
feed.STORE = os.path.join(d, "feed.json")
FID = "f" * 12
EID = "e" * 12
feed._save({
    "feeds": [{"id": FID, "title": "某源", "url": "https://example.com/feed",
               "site": "https://example.com", "kind": "rss", "group": ""}],
    "entries": {FID: [{"id": EID, "feed_id": FID, "title": "某篇",
                       "link": "https://example.com/a", "author": "甲",
                       "published": "2026-01-01", "content_md": "正文一段。",
                       "has_content": True, "read": False, "starred": False,
                       "tags": [], "pushed": ""}]},
    "seen": {}, "settings": {},
})
books = os.path.join(d, "books")
try:
    caps = capture_tmps(lambda: feed.to_shelf(EID, books))
    info = feed.to_shelf(EID, os.path.join(d, "books2"))
    err = ""
except Exception as e:              # noqa: BLE001
    caps, info, err = [], {}, "%s: %s" % (type(e).__name__, e)
chk("C1 收成书：这趟没抛异常（meta 写口接住了）", err == "", err)
metas = [n for n in caps if re.match(r"meta\.json\.\d+\.[0-9a-f]+\.tmp$", n)]
chk("C2 收成书：meta.json 的临时件带进程号与随机串", bool(metas), caps)
book_dir = info.get("dir", "") if isinstance(info, dict) else ""
meta_path = os.path.join(book_dir, "meta.json")
meta_ok = False
try:
    meta_ok = json.load(open(meta_path, encoding="utf-8")).get("source") == "feed"
except Exception:                   # noqa: BLE001
    meta_ok = False
chk("C3 收成书：meta.json 落在那本书目录里、source 是 feed", meta_ok, meta_path)
chk("C4 收成书：整棵书库目录里没有 .tmp 残件", tmp_left_deep(books) == [], tmp_left_deep(books))
feed.STORE = saved_store

# ── D. 对照：共用固定临时件名会当场塌 ───────────────────────────────
print("── D. 对照：共用固定临时件名会当场塌 ──────────────────")
d = mk()
target = os.path.join(d, "shared.json")
fixed = target + ".tmp"
with open(fixed, "w", encoding="utf-8") as f1:
    json.dump({"who": "甲"}, f1, ensure_ascii=False)
with open(fixed, "w", encoding="utf-8") as f2:      # 后来者一进去就把前一个人截断
    f2.write('{"who": "乙"')
    os.replace(fixed, target)                      # 乙先收尾，把两人共用的件搬走
lost = None
try:
    os.replace(fixed, target)                      # 甲轮到自己那一句
except FileNotFoundError:
    lost = "FileNotFoundError"
chk("D1 对照：共用一个固定 .tmp 时，后一个 os.replace 当场 FileNotFoundError",
    lost == "FileNotFoundError", lost)
landed = open(target, encoding="utf-8").read()
broken = False
try:
    json.loads(landed)
except Exception:                   # noqa: BLE001
    broken = True
chk("D2 对照：顶上去的是半截那一份，正式档解不动", broken, landed[:60])
shutil.rmtree(d, ignore_errors=True)

# ── E. 写失败：照旧抛、残件收干净、旧档不动 ─────────────────────────
print("── E. 写失败：收干净、旧档不动 ────────────────────────")
d = mk()
saved_store = feed.STORE
feed.STORE = os.path.join(d, "feed.json")
with open(feed.STORE, "w", encoding="utf-8") as f:
    f.write('{"good": 1}')
before = open(feed.STORE, "rb").read()
raised = None
try:
    feed._save({"entries": {"x": [{"bad": {1, 2}}]}})   # set 不是 JSON 能序列化的形状
except Exception as e:              # noqa: BLE001 —— 任何异常都算「照旧抛」
    raised = type(e).__name__
chk("E1 feed：真写不进去时照旧抛（不吞）", raised is not None, raised)
chk("E2 feed：写崩后临时件收干净了", tmp_left(d) == [], tmp_left(d))
chk("E3 feed：写崩不毁旧档（逐字节还在）",
    open(feed.STORE, "rb").read() == before, open(feed.STORE, "rb").read()[:60])
feed.STORE = saved_store

opml2 = os.path.join(d, "订阅.opml")
with open(opml2, "w", encoding="utf-8") as f:
    f.write("<opml>旧的那一份</opml>")
before_opml = open(opml2, "rb").read()
real_export = feed.export_opml
feed.export_opml = lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("模型炸了"))
raised2 = None
try:
    feed.write_opml(opml2)
except Exception as e:              # noqa: BLE001
    raised2 = type(e).__name__
finally:
    feed.export_opml = real_export
chk("E4 OPML：导出那一步抛时照旧往外抛", raised2 == "RuntimeError", raised2)
chk("E5 OPML：抛完之后临时件收干净了", tmp_left(d) == [], tmp_left(d))
chk("E6 OPML：写崩不毁旧档（逐字节还在）",
    open(opml2, "rb").read() == before_opml, open(opml2, "rb").read()[:60])
shutil.rmtree(d, ignore_errors=True)

# ── F. 并发：六个线程同时导同一份 OPML ──────────────────────────────
print("── F. 并发：六线程同时导同一份 OPML ──────────────────")
d = mk()
opml3 = os.path.join(d, "订阅.opml")
N = 6
ROUNDS = 40

# F 对照组：把临时名写死成 `path + ".tmp"`（旧写法）。六个写手都先把件打开写完、
# 在栏前会齐，然后各自 os.replace —— 只有第一个搬得走件，其余当场 FileNotFoundError。
bar = threading.Barrier(N)
fixed = opml3 + ".tmp"
control_errs = []


def control_one(i):
    try:
        with open(fixed, "w", encoding="utf-8") as f:
            f.write("<opml>%d</opml>" % i)
        bar.wait(timeout=20)
        os.replace(fixed, opml3)
    except Exception as e:          # noqa: BLE001
        control_errs.append(type(e).__name__)


ts = [threading.Thread(target=control_one, args=(i,)) for i in range(N)]
for t in ts:
    t.start()
for t in ts:
    t.join(timeout=60)
chk("F1 对照：共用一个固定临时名时，除先收尾那一个外全当场 FileNotFoundError",
    control_errs.count("FileNotFoundError") == N - 1 and len(control_errs) == N - 1,
    control_errs)

# F 正片：真代码，六个线程同时导同一份 OPML。
errs = []
bar2 = threading.Barrier(N)


def worker(i):
    try:
        bar2.wait(timeout=20)
        for _k in range(ROUNDS):
            feed.write_opml(opml3, title="归藏订阅-%d" % i)
    except Exception as e:          # noqa: BLE001
        errs.append("%s: %s" % (type(e).__name__, e))


ts = [threading.Thread(target=worker, args=(i,)) for i in range(N)]
for t in ts:
    t.start()
for t in ts:
    t.join(timeout=60)
chk("F2 正片：六线程同时导同一份 OPML，没有一个写手抛异常", errs == [], errs[:3])
chk("F3 正片：落地的那份解析得动、且是某一版的完整标题",
    "归藏订阅-" in open(opml3, encoding="utf-8").read() if os.path.exists(opml3) else False)
r = None
try:
    r = ET.parse(opml3).getroot().tag
except Exception as e:              # noqa: BLE001
    r = "%s: %s" % (type(e).__name__, e)
chk("F4 正片：落地的是完整 <opml>（不是两份字符搅在一起）", r == "opml", r)
chk("F5 正片：并发之后没有 .tmp 残件", tmp_left(d) == [], tmp_left(d))
shutil.rmtree(d, ignore_errors=True)

# ── G. 接线：固定名那句真没了，三处写口都接了 ───────────────────────
print("── G. 接线 ────────────────────────────────────────────")
src = (REPO / "feed.py").read_text(encoding="utf-8")
save_body = src.split("def _save", 1)[1].split("\ndef ", 1)[0]
chk("G1 接线：_save 里不再有 `tmp = STORE + \".tmp\"` 那句固定名",
    'STORE + ".tmp"' not in save_body,
    [l for l in save_body.splitlines() if ".tmp" in l][:3])
opml_body = src.split("def write_opml", 1)[1].split("\ndef ", 1)[0]
chk("G2 接线：write_opml 里不再有 `tmp = p + \".tmp\"` 那句固定名",
    'p + ".tmp"' not in opml_body,
    [l for l in opml_body.splitlines() if ".tmp" in l][:3])
shelf_body = src.split("def to_shelf", 1)[1].split("\ndef ", 1)[0]
chk("G3 接线：to_shelf 那份 meta.json 不再有 `tmp = mp + \".tmp\"`",
    'mp + ".tmp"' not in shelf_body,
    [l for l in shelf_body.splitlines() if ".tmp" in l][:3])
helper = src.split("def _tmp_name", 1)[1].split("\ndef ", 1)[0]
chk("G4 接线：_tmp_name 走进程号 + uuid（与 board / book_notes 同一把尺子）",
    "os.getpid()" in helper and "uuid" in helper, helper[:200])
chk("G5 接线：三处写口都接了 `_tmp_name`（定义一处 + 调用三处）",
    len(re.findall(r"_tmp_name\(", src)) >= 4, len(re.findall(r"_tmp_name\(", src)))
chk("G6 接线：残件回收兜的是 BaseException（中止信号那条路也收）",
    save_body.count("except BaseException") >= 1 and opml_body.count("except BaseException") >= 1,
    (save_body[:0], opml_body[:0]))

print()
if FAIL:
    print("✗ check_feed_atomic：%d 条没过" % len(FAIL))
    for n in FAIL:
        print("   - " + n)
    sys.exit(1)
print("✓ check_feed_atomic：%d 条全过" % PASSED[0])
