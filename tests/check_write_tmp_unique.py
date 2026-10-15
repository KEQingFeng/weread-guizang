# -*- coding: utf-8 -*-
"""三个落盘口（笔记 / 画板 / 导图）：临时件不许共用固定名。

为什么单开一条门禁
    `book_notes._write_json` / `_write_text`、`board._write_json` / `_write_bytes`、
    `mindmap._write_json` 原先都把临时件写死成 `path + ".tmp"`，而这三条保存路
    **都没走 `ui_server._WRITE_LOCK`**，后端又是 `ThreadingHTTPServer`：

      · `/api/mindmap` 的保存与 AI 画图那条任务线都会调 `mindmap.save_map`，
        同一本书被两处同时存；
      · `/api/board` 的保存与导出可以同时进来（同一块板的两次保存也行）；
      · `/api/mynotes` 的自动保存与 `/api/mynotes/export` 各写一份。

    两个写手同时进来时，固定临时名有两种代价（与 `check_anna_atomic` 同一个形状）：
    先收尾的那个 `os.replace` 把共用的临时件整个搬走，后一个轮到自己那一句时临时件
    已经不在 —— 抛 `FileNotFoundError`，界面看到「保存失败」，可前一笔已经落盘；
    或者后开的那一个把前一截截断，两份内容交织着顶成正式文件。

收口
    临时件名带进程号 + 一小串随机（`<名>.<pid>.<rand>.tmp`），与 `ui_server._write_json`
    同一把尺子；写失败时先把临时件收干净再抛，旧档始终完整。`board.delete_board` 的
    purge 也认得这个新名字（连同旧的固定名一起收）—— 被强杀留下的那一份只有磁盘看得见。

这一条钉四面：
  A. 正常写：每次临时件名都不一样、带进程号、以 `.tmp` 收尾，写完全目录无残件；
  B. 对照：两个写手共用同一个固定临时件名时，后一个 `os.replace` 当场
     `FileNotFoundError` —— 证明这道闸不是空转；
  C. 写失败：照旧抛、临时件收干净、旧档逐字节不动；
  D. 画板 purge 连唯一临时名与旧的固定名一起收，且不碰别的板。

跑法：纯函数 + 线程 + 系统临时目录沙盒，不联网、不起服务、不开浏览器，归 --fast。
"""
import builtins
import json
import os
import pathlib
import shutil
import sys
import tempfile
import threading

HERE = pathlib.Path(__file__).resolve().parent
REPO = HERE.parent
sys.path.insert(0, str(REPO))

import board          # noqa: E402
import book_notes     # noqa: E402
import mindmap        # noqa: E402

HI = chr(0xD800)      # 落单的半个代理码点，用 chr() 现造，源码里不放字面量

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
    return tempfile.mkdtemp(prefix="guizang-wtmp-")


def tmp_left(d):
    return sorted(n for n in os.listdir(d) if n.endswith(".tmp")) if os.path.isdir(d) else []


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


BOARD_DOC = {"id": "b1", "title": "板一", "canvas": {"objects": [{"type": "rect"}]}}
MAP_DOC = {"title": "图一", "nodes": [{"id": "a", "text": "根", "children": []}],
           "links": []}
NOTES_DOC = {"marks": [], "entries": []}


# ── A. 正常写：临时件一次一个名字、带进程号、不留残件 ────────────────
print("── A. 正常写：临时件一次一个名字 ──────────────────────")

cases = []


def board_case(d):
    r = board.save_board(d, BOARD_DOC)
    return r.get("ok") is True


def map_case(d):
    r = mindmap.save_map(d, MAP_DOC)
    return r.get("ok") is True


def notes_case(d):
    return book_notes.save_notes(d, NOTES_DOC) is True


def notes_md_case(d):
    r = book_notes.export_notes(d, NOTES_DOC)
    return bool(r.get("path"))


for label, fn in (("画板 board._write_json", board_case),
                  ("导图 mindmap._write_json", map_case),
                  ("笔记 book_notes._write_json", notes_case),
                  ("笔记 book_notes._write_text", notes_md_case)):
    d = mk()
    case_dir = os.path.join(d, "boards") if label.startswith("画板") else d
    t1 = capture_tmps(lambda: fn(d))
    t2 = capture_tmps(lambda: fn(d))
    chk("A1 %s：写成功" % label, bool(t1), t1)
    chk("A2 %s：临时件与正式文件分开，以 .tmp 收尾" % label,
        bool(t1) and not t1[0].endswith(".json") and t1[0].endswith(".tmp"), t1)
    chk("A3 %s：两次写的临时件名不一样，且带着自己的进程号" % label,
        bool(t1) and bool(t2) and t1[0] != t2[0] and str(os.getpid()) in t1[0],
        (t1, t2))
    chk("A4 %s：写完之后没有 .tmp 残件" % label, tmp_left(case_dir) == [], tmp_left(case_dir))
    shutil.rmtree(d, ignore_errors=True)

# ── B. 对照：共用固定临时件名，后一个 replace 当场抛 ─────────────────
print("── B. 对照：共用固定临时件名会当场抛 ──────────────────")
d = mk()
target = os.path.join(d, "shared.json")
old = target + ".tmp"
with open(old, "w", encoding="utf-8") as f1:
    json.dump({"who": "甲"}, f1, ensure_ascii=False)
with open(old, "w", encoding="utf-8") as f2:
    json.dump({"who": "乙"}, f2, ensure_ascii=False)
    os.replace(old, target)            # 乙先收尾，把两人共用的临时件搬走
lost = None
try:
    os.replace(old, target)            # 甲轮到自己那一句
except FileNotFoundError:
    lost = "FileNotFoundError"
chk("B1 对照：共用 .tmp 时后一个写手的 os.replace 会当场 FileNotFoundError（旧写法真会丢这一笔）",
    lost == "FileNotFoundError", lost)
shutil.rmtree(d, ignore_errors=True)

# ── C. 写失败：照旧抛、临时件收干净、旧档不动 ───────────────────────
print("── C. 写失败：收干净、旧档不动 ────────────────────────")
FAIL_CASES = [
    ("画板 board._write_json", lambda p: board._write_json(p, {"bad": {1, 2}})),
    ("导图 mindmap._write_json", lambda p: mindmap._write_json(p, "坏" + HI)),
    ("笔记 book_notes._write_json", lambda p: book_notes._write_json(p, {"bad": {1, 2}})),
    ("笔记 book_notes._write_text", lambda p: book_notes._write_text(p, "坏" + HI)),
]
for label, bad in FAIL_CASES:
    d = mk()
    p = os.path.join(d, "x.json")
    with open(p, "w", encoding="utf-8") as f:
        f.write('{"good": 1}')
    before = open(p, "rb").read()
    raised = None
    try:
        bad(p)
    except Exception as e:              # noqa: BLE001 —— 任何异常都算「照旧抛」
        raised = type(e).__name__
    chk("C1 %s：真写不进去时照旧抛（不吞）" % label, raised is not None, raised)
    chk("C2 %s：写崩后临时件收干净了" % label, tmp_left(d) == [], tmp_left(d))
    chk("C3 %s：写崩不毁旧档（逐字节还在）" % label,
        open(p, "rb").read() == before, open(p, "rb").read()[:60])
    shutil.rmtree(d, ignore_errors=True)

# ── D. 画板 purge：唯一临时名与旧固定名一起收 ───────────────────────
print("── D. 画板 purge：两种临时名一起收 ─────────────────────")
d = mk()
bd = os.path.join(d, "boards")
os.makedirs(bd, exist_ok=True)
board.save_board(d, BOARD_DOC)
legacy = os.path.join(bd, "b1.json.tmp")                 # 旧写法残留
uniq = os.path.join(bd, "b1.json.%d.deadbeef.tmp" % os.getpid())  # 新写法残留
other = os.path.join(bd, "b2.json.%d.cafebabe.tmp" % os.getpid())  # 别的板的残件
for p in (legacy, uniq, other):
    with open(p, "w", encoding="utf-8") as f:
        f.write("{}")
res = board.delete_board(d, "b1", purge=True)
removed = res.get("removed") or []
chk("D1 purge 把旧固定名 .tmp 收走", "b1.json.tmp" in removed, removed)
chk("D2 purge 把唯一临时名也收走", os.path.basename(uniq) in removed, removed)
chk("D3 purge 不碰别的板的临时件", os.path.exists(other), sorted(os.listdir(bd)))
chk("D4 收完之后 boards/ 里只剩别的板那一份", sorted(os.listdir(bd)) == [os.path.basename(other)],
    sorted(os.listdir(bd)))
shutil.rmtree(d, ignore_errors=True)

# ── E. 旁证：真两个线程同时存，最后落地的那份一定解得动、不留残件 ────
print("── E. 旁证：两线程同时存 ──────────────────────────────")
HAMMER = {"画板": (board_case, "boards"), "导图": (map_case, ""),
          "笔记": (notes_case, "")}
for label, (fn, sub) in HAMMER.items():
    d = mk()
    errs = []
    N = 4
    bar = threading.Barrier(N)

    def worker(i):
        try:
            bar.wait(timeout=10)
            for k in range(3):
                # 每次的负载不一样，方便辨认最后落地的是哪一份；但都必须是合法 JSON。
                if label == "画板":
                    board.save_board(d, {"id": "b1", "canvas": {"objects": [{"i": i, "k": k}]}})
                elif label == "导图":
                    mindmap.save_map(d, {"title": "图%d-%d" % (i, k),
                                         "nodes": [{"id": "a", "text": str(i), "children": []}],
                                         "links": []})
                else:
                    book_notes.save_notes(d, {"note": "%d-%d" % (i, k), "marks": [], "entries": []})
        except Exception as e:          # noqa: BLE001
            errs.append("%s: %s" % (type(e).__name__, e))

    ts = [threading.Thread(target=worker, args=(i,)) for i in range(N)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(timeout=30)
    case_dir = os.path.join(d, sub) if sub else d
    chk("E1 %s：四个线程并发存，没有一个写手抛异常" % label, errs == [], errs[:3])
    chk("E2 %s：并发之后没有 .tmp 残件" % label, tmp_left(case_dir) == [], tmp_left(case_dir))
    target = (os.path.join(case_dir, "b1.json") if label == "画板"
              else os.path.join(case_dir, "mindmap.json") if label == "导图"
              else book_notes.notes_path(d))
    ok_parse = False
    try:
        json.load(open(target, encoding="utf-8"))
        ok_parse = True
    except Exception as e:              # noqa: BLE001
        ok_parse = "%s: %s" % (type(e).__name__, e)
    chk("E3 %s：最后落地的那一份解得动（不是两份交织）" % label, ok_parse is True, ok_parse)
    shutil.rmtree(d, ignore_errors=True)

print()
if FAIL:
    print("✗ check_write_tmp_unique：%d 条没过" % len(FAIL))
    for n in FAIL:
        print("   - " + n)
    sys.exit(1)
print("✓ check_write_tmp_unique：%d 条全过" % PASSED[0])
