# -*- coding: utf-8 -*-
"""写作平台三个落盘口（`writer._write_text` / `writer._write_json`）：临时件不许共用固定名。

为什么单开一条门禁
    写作这一格有三个目标文件会被两个请求线程同时写，而后端是 `ThreadingHTTPServer`：

      · 正文 `draft.md` —— 编辑器的自动存盘与「手动保存」并着打 `/api/writer` 的
        `save`，两次各带自己的 rev 进来，落的是同一个 `draft_path`；
      · `meta.json` —— 同一次 `save` 与「让 AI 给几个方向」回包后的
        `remember_sparks`（`ui_server.writer_ai_after`）都写它，中间隔着一趟模型调用，
        最容易撞上；
      · 册账 `books.json` —— 建书 / 归章 / 排序 / 删章各是一条 POST，两次点击就是
        两个请求线程。

    旧写法把临时件写死成 `path + ".tmp"`，撞上时有两种代价：先收尾的那个 `os.replace`
    把共用的临时件整个搬走，后一个写手轮到自己那一句时件已经不在，抛
    `FileNotFoundError`（界面上是「存不下」，可前一笔明明落盘了）；更坏的是后来者
    `open(..., "w")` 把前一个人写了一半的件截断，两边各按自己的偏移往下写，
    `os.replace` 顶成正式文件的是**两份内容交织**的那一份 —— `draft.md` 的读口认它
    就是全部正文，册账解不动就整份退回空账（写作屏的书树跟着一起空）。

收口
    临时件一次一个名字（进程号 + 一小串随机），与 `book_notes._tmp_name` /
    `ui_server._write_json` 同一把尺子；写失败（磁盘满、序列化不了、半个代理码点）
    先把残件收干净再抛，旧档逐字节不动。正文走 `_write_text`、账本走 `_write_json`，
    `save` / `_write_meta` / `save_books` 三处都改走这两个口子。

钉五面：
  A. 正常写：临时件与正式文件分开、以 `.tmp` 收尾、两次名字不一样且带进程号 + 随机、
     写完全目录无残件，正文读得回；
  B. 对照：两个写手共用同一个固定临时件名时，后一个 `os.replace` 当场
     `FileNotFoundError`、顶上去的是半截 —— 证明这道闸不是空转；
  C. 写失败：照旧抛、临时件收干净、旧档逐字节不动（正文与账本各钉一遍）；
  D. 旁证：多个线程并发存同一篇正文 / 同一份 meta / 同一本册账，没有一个写手抛异常，
     最后落地的那一份解得动、不留 `.tmp`；
  E. 接线：三处固定名那句真没了，两个 meta 写口与多个册账动作真的都在（并发不是假想）。

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

import writer        # noqa: E402

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
    return tempfile.mkdtemp(prefix="guizang-watomic-")


def tmps(root):
    """整个沙盒里所有以 .tmp 收尾的件（正文 / meta / 册账 / 快照都要算）。"""
    out = []
    for dp, _dn, fn in os.walk(root):
        out += [os.path.relpath(os.path.join(dp, n), root) for n in fn if n.endswith(".tmp")]
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


def parses(raw):
    try:
        json.loads(raw)
        return True
    except Exception:               # noqa: BLE001
        return False


SB = mk()
bid, _meta = writer.create(SB, "题一", "甲的初稿")
D = writer.dir_of(SB, bid)
BOOKS_ROOT = SB

# ── A. 正常写：临时件一次一个名字 ────────────────────────────────────
print("── A. 正常写：临时件一次一个名字 ──────────────────────")
t1 = capture_tmps(lambda: writer.save(SB, bid, "第一稿"))
t2 = capture_tmps(lambda: writer.save(SB, bid, "第二稿"))
chk("A1 正文：两次都写得下", bool(t1) and bool(t2), (t1, t2))
chk("A2 正文：临时件与正式文件分开，以 .tmp 收尾",
    bool(t1) and not t1[-1].endswith(".md") and t1[-1].endswith(".tmp"), t1)
chk("A3 正文：两次写的临时件名不一样，且带着自己的进程号",
    bool(t1) and bool(t2) and t1[0] != t2[0] and str(os.getpid()) in t1[0], (t1, t2))
chk("A4 正文：进程号后面还跟着一串随机（同一进程里两次也不撞）",
    bool(re.search(r"\.%d\.[0-9a-f]{6,}\.tmp$" % os.getpid(), t1[0] if t1 else "")), t1)
chk("A5 正文：写的是最后那一份（改名顶替真的生效）",
    writer.read_text(D) == "第二稿", writer.read_text(D)[:40])
chk("A6 正常写完：沙盒里没有 .tmp 残件", tmps(SB) == [], tmps(SB))
# meta 与册账也走同一个口子：抓一次各自的临时件名，证明它们不再固定同名。
mt = capture_tmps(lambda: writer.remember_sparks(SB, bid, ["角度一"]))
bt = capture_tmps(lambda: writer.save_books(SB, [{"id": "bk_x", "title": "册", "chapters": []}]))
chk("A7 meta：临时名带进程号 + 随机，不是 meta.json + .tmp",
    bool(mt) and str(os.getpid()) in mt[0] and re.search(r"\.%d\.[0-9a-f]{6,}\.tmp$" % os.getpid(), mt[0]),
    mt)
chk("A8 册账：临时名带进程号 + 随机，不是 books.json + .tmp",
    bool(bt) and str(os.getpid()) in bt[0] and re.search(r"\.%d\.[0-9a-f]{6,}\.tmp$" % os.getpid(), bt[0]),
    bt)
chk("A9 沙盒里仍没有 .tmp 残件", tmps(SB) == [], tmps(SB))

# ── B. 对照：共用固定临时件名，后一个 replace 当场抛 ─────────────────
print("── B. 对照：共用固定临时件名会当场抛 ──────────────────")
d = mk()
target = os.path.join(d, "shared.md")
old = target + ".tmp"
with open(old, "w", encoding="utf-8") as f1:
    f1.write("甲的那一份完整正文")
with open(old, "w", encoding="utf-8") as f2:
    f2.write("乙的那一份")
    os.replace(old, target)            # 乙先收尾，把两人共用的临时件搬走
lost = None
try:
    os.replace(old, target)            # 甲轮到自己那一句
except FileNotFoundError:
    lost = "FileNotFoundError"
chk("B1 对照：共用 .tmp 时后一个写手的 os.replace 会当场 FileNotFoundError（旧写法真会丢这一笔）",
    lost == "FileNotFoundError", lost)
with open(old, "w", encoding="utf-8") as f3:
    f3.write("甲写了一长段" + "A" * 40)
with open(old, "w", encoding="utf-8") as f4:      # 后来者一进去就把前一个人写好的截断
    f4.write("乙的半截")                            # 只写到半截就被抢先收尾
os.replace(old, target)
landed = open(target, encoding="utf-8").read()
chk("B2 对照：共用 .tmp 时顶上去的是后一个人那半截，前一份整段没了",
    landed == "乙的半截", landed[:60])
shutil.rmtree(d, ignore_errors=True)

# ── C. 写失败：照旧抛、临时件收干净、旧档不动 ───────────────────────
print("── C. 写失败：收干净、旧档不动 ────────────────────────")
d = mk()
p = os.path.join(d, "draft.md")
with open(p, "w", encoding="utf-8") as f:
    f.write("上一次那一份完整正文")
before = open(p, "rb").read()
raised = None
try:
    writer._write_text(p, "坏" + HI)
except Exception as e:              # noqa: BLE001 —— 任何异常都算「照旧抛」
    raised = type(e).__name__
chk("C1 正文：真写不进去时照旧抛（不吞）", raised is not None, raised)
chk("C2 正文：写崩后临时件收干净了", tmps(d) == [], tmps(d))
chk("C3 正文：写崩不毁旧档（逐字节还在）",
    open(p, "rb").read() == before, open(p, "rb").read()[:60])

q = os.path.join(d, "meta.json")
with open(q, "w", encoding="utf-8") as f:
    f.write('{"good": 1}')
qbefore = open(q, "rb").read()
qraised = None
try:
    writer._write_json(q, {"x": "坏" + HI})
except Exception as e:              # noqa: BLE001
    qraised = type(e).__name__
chk("C4 账本：真写不进去时照旧抛（不吞）", qraised is not None, qraised)
chk("C5 账本：写崩后临时件收干净了", tmps(d) == [], tmps(d))
chk("C6 账本：写崩不毁旧档（逐字节还在）", open(q, "rb").read() == qbefore, open(q, "rb").read()[:60])
shutil.rmtree(d, ignore_errors=True)

# ── D. 旁证：多线程并发写同一个目标 ────────────────────────────────
print("── D. 旁证：多线程并发写 ──────────────────────────────")
d = mk()
bid2, _m = writer.create(d, "题二", "起点")
D2 = writer.dir_of(d, bid2)
N = 4
errs = []
bar = threading.Barrier(N)


def save_worker(i):
    try:
        bar.wait(timeout=10)
        for k in range(3):
            ok, _out = writer.save(d, bid2, "第%d-%d稿" % (i, k))
            if not ok:
                errs.append("save(%d,%d) 回了 not ok" % (i, k))
    except Exception as e:          # noqa: BLE001
        errs.append("%s: %s" % (type(e).__name__, e))


ts = [threading.Thread(target=save_worker, args=(i,)) for i in range(N)]
for t in ts:
    t.start()
for t in ts:
    t.join(timeout=30)
chk("D1 正文：四个线程并发存，没有一个写手抛异常 / 回 not ok", errs == [], errs[:3])
chk("D2 正文：并发之后没有 .tmp 残件", tmps(d) == [], tmps(d))
landed = writer.read_text(D2)
chk("D3 正文：最后落地的是某一版的完整正文（不是两份交织）",
    bool(re.match(r"^第\d+-\d+稿$", landed)), landed[:60])

merrs = []


def meta_worker(i):
    try:
        bar.wait(timeout=10)
        for k in range(3):
            writer.remember_sparks(d, bid2, ["角度%d-%d" % (i, k)])
    except Exception as e:          # noqa: BLE001
        merrs.append("%s: %s" % (type(e).__name__, e))


ts = [threading.Thread(target=meta_worker, args=(i,)) for i in range(N)]
for t in ts:
    t.start()
for t in ts:
    t.join(timeout=30)
chk("D4 meta：四个线程并发写同一份 meta.json，没有一个写手抛异常", merrs == [], merrs[:3])
try:
    meta_doc = json.load(open(writer.meta_path(D2), encoding="utf-8"))
    meta_ok = isinstance(meta_doc, dict) and meta_doc.get("id") == bid2
except Exception as e:              # noqa: BLE001
    meta_ok = "%s: %s" % (type(e).__name__, e)
chk("D5 meta：最后落地的那一份解得动、还是这本书的那份账", meta_ok is True, meta_ok)
chk("D6 meta：并发之后没有 .tmp 残件", tmps(d) == [], tmps(d))

berrs = []


def book_worker(i):
    try:
        bar.wait(timeout=10)
        writer.create_book(d, "书%d" % i)
    except Exception as e:          # noqa: BLE001
        berrs.append("%s: %s" % (type(e).__name__, e))


ts = [threading.Thread(target=book_worker, args=(i,)) for i in range(N)]
for t in ts:
    t.start()
for t in ts:
    t.join(timeout=30)
chk("D7 册账：四个线程并发建书（都写 books.json），没有一个写手抛异常", berrs == [], berrs[:3])
try:
    books = writer.load_books(d)
    books_ok = isinstance(books, list)
except Exception as e:              # noqa: BLE001
    books_ok = "%s: %s" % (type(e).__name__, e)
chk("D8 册账：最后落地的那一份解得动、是个书目表", books_ok is True, books_ok)
chk("D9 册账：并发之后没有 .tmp 残件", tmps(d) == [], tmps(d))
shutil.rmtree(d, ignore_errors=True)
shutil.rmtree(SB, ignore_errors=True)

# ── E. 接线：三处固定名真没了，写口与动作真都在 ─────────────────────
print("── E. 接线 ────────────────────────────────────────────")
src = (REPO / "writer.py").read_text(encoding="utf-8")


def body_of(fn):
    return src.split("def %s(" % fn, 1)[-1].split("\ndef ", 1)[0]


chk("E1 接线：save 里不再有 `tmp = draft_path(d) + \".tmp\"` 那句固定名",
    'tmp = draft_path(d) + ".tmp"' not in body_of("save")
    and "_write_text(draft_path(d)" in body_of("save"),
    [l for l in body_of("save").splitlines() if ".tmp" in l][:3])
chk("E2 接线：_write_meta 走 _write_json，不再拼 `meta_path(...) + \".tmp\"`",
    'meta_path(draft_dir) + ".tmp"' not in body_of("_write_meta")
    and "_write_json(meta_path(draft_dir)" in body_of("_write_meta"),
    [l for l in body_of("_write_meta").splitlines() if ".tmp" in l][:3])
chk("E3 接线：save_books 走 _write_json，不再拼 `p + \".tmp\"`",
    'p + ".tmp"' not in body_of("save_books")
    and "_write_json(books_path(books_root)" in body_of("save_books"),
    [l for l in body_of("save_books").splitlines() if ".tmp" in l][:3])
chk("E4 接线：临时名走进程号 + uuid（与 book_notes / ui_server 同一把尺子）",
    "os.getpid()" in body_of("_tmp_name") and "uuid" in body_of("_tmp_name"),
    body_of("_tmp_name")[:200])
chk("E5 接线：失败那条路兜的是 BaseException（中止信号也不漏件）",
    body_of("_write_text").count("except BaseException") == 1
    and body_of("_write_json").count("except BaseException") == 1,
    (body_of("_write_text")[-160:], body_of("_write_json")[-160:]))
u_src = (REPO / "ui_server.py").read_text(encoding="utf-8")
chk("E6 接线：meta 确实有两个写口（界面存盘那次与 AI 灵感回包那次），并发不是假想",
    "writer_mod.save(" in u_src and "writer_mod.remember_sparks(" in u_src,
    (u_src.count("writer_mod.save("), u_src.count("writer_mod.remember_sparks(")))
acts = sum(u_src.count("writer_mod.%s(" % n)
           for n in ("create_book", "rename_book", "delete_book",
                     "add_chapter", "remove_chapter", "order_chapter"))
chk("E7 接线：册账动作真的有好几个请求入口（建 / 改名 / 删 / 归章 / 排序）", acts >= 4, acts)

print()
if FAIL:
    print("✗ check_writer_atomic：%d 条没过" % len(FAIL))
    for n in FAIL:
        print("   - " + n)
    sys.exit(1)
print("✓ check_writer_atomic：%d 条全过" % PASSED[0])
