#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""归藏门禁：写作落盘的三个写口（正文 / meta / 册账）临时件不许共用。

为什么单开这一条
    写作这一格是**同一个路径会被不止一个写手写**的那一种，而这条路上没有任何锁：

      · 后端是 `ThreadingHTTPServer`（`ui_server.py`），界面的自动存盘、手点「保存」、
        另一个标签页、MCP 那几个工具可以同时进 `writer.save()`；
      · `ui_server._WRITE_LOCK` 只盖配置 / 书架 / 时长 / 计划那几处，`/api/writer/save`
        （`writer_mod.save`）不在里面 —— writer.py 自己也没有锁。

    旧写法把临时件写死成 `path + ".tmp"`（`save` 的正文、`_write_meta` 的 meta、
    `save_books` 的册账三处都是），两个写手同时进来时有两种代价，与 `anna_state` /
    `flomo_notes` / `board` 当初那一格完全一样：

      · **写坏**：后来者 `open(..., "w")` 把前一个人写了一半的临时件截断，两边各自按
        自己的文件偏移往下写，`os.replace` 把两份交织在一起的那一份顶成正式稿子 ——
        用户看到的是「保存成功了、稿子却成了两段话拼在一起」；
      · **当场抛**：先收尾的那一个 `os.replace` 把共用的临时件整个搬走，后一个写手轮到
        自己那一句时临时件已经不在，抛 `FileNotFoundError`；这条异常从 `/api/writer/save`
        直接穿出去，界面拿到 500，而它明明刚提示过「已保存」。

收口
    三处（含 `create()` 建稿那一下）都换成同一个口子 `writer._write_text_atomic`：临时名
    带 pid + 一小串随机，与 `ui_server._write_bytes`（封面那一格）同一把尺子；写失败先把
    临时件收干净再抛，旧档始终完整。

钉七面
  A. 正常写：正文与 meta 各一次替换、源不是正式文件、两次写的临时件名不一样且带进程号；
  B. 对照：共用固定 `.tmp` 时写出的是什么（顶上去的不是任何一份完整正文）＋ 后一个写手
     的 replace 当场 FileNotFoundError —— 这道闸不是空转；
  C. 写失败：照旧抛，临时件收干净，旧档逐字节不动；
  D. 崩过之后同一个口子还能用；册账（`save_books`）走的是同一把尺子；
  E. 两个真进程同时写同一篇稿：最后落地的正文一定是**某一份完整的稿**、meta 仍是一份
     解得动的完整账、不留 `.tmp`（A3 / C2 是回归探测器；这一节是实机旁证）。
  F. 另两只手：meta（`remember_sparks`）与册账（`create_book`）各四个真线程同时进来 ——
     这一节钉的是**不写坏、不漏件、不抛**，不是「四次更新都在」：读-改-写没有锁，最后
     落地谁那一版本来就由调度决定，所以断言只看「解得动、是这本书的账、不留 `.tmp`」。
  G. 接线：`writer.py` 里再没有 `tmp = … + ".tmp"` 那种固定名赋值、`_write_text_atomic`
     兜的是 BaseException；meta 与册账在界面上确实有好几个请求入口（并发不是假想）。

跑法：纯函数 + 系统临时目录沙盒书库，不联网、不开浏览器、不起服务，归 --fast 静态段。
"""
import atexit
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import writer  # noqa: E402

FAIL = []
PASSED = [0]


def chk(name, cond, extra=""):
    if cond:
        PASSED[0] += 1
        print("  ok  " + name)
    else:
        FAIL.append(name)
        print(" FAIL " + name + (("  <- " + esc(extra)[:220]) if extra else ""))


def esc(s):
    """extra 里可能带着落单的半个代理码点：直接 print 会自己抛，把一条断言失败
    伪装成整个套件崩。转义成 \\ud800 再打。"""
    return str(s).encode("unicode_escape").decode("ascii")


def read_repo(fname):
    with open(os.path.join(ROOT, fname), encoding="utf-8") as f:
        return f.read()


TMP = tempfile.mkdtemp(prefix="guizang-writer-atomic-")
atexit.register(lambda: shutil.rmtree(TMP, ignore_errors=True))
LIB = os.path.join(TMP, "书库")
os.makedirs(LIB, exist_ok=True)

HI = chr(0xD800)          # 落单的半个代理码点，用 chr() 现造，源码里不出现字面量


def make(text="旧正文"):
    bid, _meta = writer.create(LIB, "原子落盘", text)
    return bid, writer.dir_of(LIB, bid)


def tmps(d):
    """沙盒里所有的 .tmp 残件（写作这一格正文 / meta / versions / 册账都在书库下）。"""
    out = []
    if not os.path.isdir(d):
        return out
    for root, _dirs, files in os.walk(d):
        out += [os.path.join(root, n) for n in files if n.endswith(".tmp")]
    return sorted(out)


def capture_replaces(fn):
    """跑一趟 fn，把它实际发起的替换记下来（源 / 目标各自的文件名）。

    `writer` 用的是 `import os`，与这里同一份模块对象；临时窗口里把它换成记账版，
    就能看见「先写临时件再顶替」这件事真的发生了、以及临时件叫什么名字。
    """
    seen = []
    real = os.replace

    def spy(src, dst, *a, **kw):
        seen.append((os.path.basename(src), os.path.basename(dst)))
        return real(src, dst, *a, **kw)

    os.replace = spy
    try:
        fn()
    finally:
        os.replace = real
    return seen


print("── A. 正常写：正文与 meta 分开落、临时件一次一个名字 ──")
bid, d = make("旧正文")
ok, meta = writer.save(LIB, bid, "新正文甲")
chk("A1 写得下、读得回，rev 往前走了",
    bool(ok) and writer.read_text(d) == "新正文甲" and int(meta.get("rev", 0)) >= 2,
    (ok, writer.read_text(d), meta))
r1 = capture_replaces(lambda: writer.save(LIB, bid, "新正文乙"))
r2 = capture_replaces(lambda: writer.save(LIB, bid, "新正文丙"))
chk("A2 正文与 meta 各走一次替换，源都不是正式文件（先写临时件再顶替）",
    len(r1) == 2 and all(src != dst and src.endswith(".tmp") for src, dst in r1), r1)
chk("A3 两次写的临时件名不一样，且带着自己的进程号（旧写法是固定同名）",
    len(r1) == 2 and len(r2) == 2
    and [s for s, _ in r1] != [s for s, _ in r2]
    and all(str(os.getpid()) in s for s, _ in r1 + r2), (r1, r2))
chk("A4 正常写完之后沙盒里没有 .tmp 残件", tmps(LIB) == [], tmps(LIB))

print("── B. 对照：共用同一个固定 .tmp 会写出什么 ──")
body_a = "甲" * 200
body_b = "乙" * 200
shared = writer.draft_path(d) + ".tmp"          # 旧写法：两个写手共用的那一个名字
fa = open(shared, "w", encoding="utf-8")
fb = open(shared, "w", encoding="utf-8")
cut = len(body_a) // 2                          # 甲写到一半；两份都必须够长，这条对照才作数
fa.write(body_a[:cut]); fa.flush()
fb.write(body_b); fb.flush()
fa.write(body_a[cut:]); fa.close()
fb.close()
os.replace(shared, writer.draft_path(d))
raw = open(writer.draft_path(d), encoding="utf-8").read()
chk("B1 对照：共用 .tmp 时顶上去的不是任何一份完整正文（旧的写法真会把稿子写坏）",
    raw not in (body_a, body_b), esc(raw[:60]))

# 共用同一个临时件名的第二种代价，而且是**当场抛异常**的那一种：先收尾的那一个
# `os.replace` 会把两人共用的临时件整个搬走，后一个写手轮到自己那一句时临时件已经
# 不在 —— 抛 FileNotFoundError，而不是安静地写坏。
with open(shared, "w", encoding="utf-8") as f1:
    f1.write(body_a)
with open(shared, "w", encoding="utf-8") as f2:
    f2.write(body_b)                            # 乙也开工，还抢先把临时件顶替了
    os.replace(shared, writer.draft_path(d))
lost = None
try:
    os.replace(shared, writer.draft_path(d))    # 甲轮到自己那一句
except FileNotFoundError:
    lost = "FileNotFoundError"
chk("B2 对照：共用 .tmp 时后一个写手的 replace 会当场 FileNotFoundError",
    lost == "FileNotFoundError", lost)

print("── C. 写失败：照旧抛、收干净、旧档不动 ────────────────")
writer._write_text_atomic(writer.draft_path(d), "上一份好的")
before = open(writer.draft_path(d), "rb").read()
raised = None
try:
    writer._write_text_atomic(writer.draft_path(d), "坏" + HI + "字")   # 编不进 utf-8，必抛
except UnicodeEncodeError:
    raised = "UnicodeEncodeError"
except Exception as e:                          # noqa: BLE001 —— 别的异常也算没接住
    raised = type(e).__name__
chk("C1 真写不进去时照旧抛（不吞异常、不静默丢这一趟）", raised == "UnicodeEncodeError", raised)
chk("C2 写崩后临时件收干净了", tmps(LIB) == [], tmps(LIB))
chk("C3 写崩不毁旧档（旧的一份逐字节还在）",
    open(writer.draft_path(d), "rb").read() == before,
    open(writer.draft_path(d), "rb").read()[:60])

print("── D. 崩过之后还能用；册账走同一个口子 ────────────────")
ok, _m = writer.save(LIB, bid, "崩过之后照旧存")
chk("D1 崩过之后同一个口子照旧可用",
    bool(ok) and writer.read_text(d) == "崩过之后照旧存", (ok, writer.read_text(d)))
writer.save_books(LIB, [{"id": "bk_000000000001", "title": "一本书",
                         "chapters": [], "created": 1, "updated": 1}])
back = writer.load_books(LIB)
chk("D2 册账写得下、读得回",
    [b.get("id") for b in back] == ["bk_000000000001"], back)
rb = capture_replaces(lambda: writer.save_books(LIB, []))
chk("D3 册账的临时件也一次一个名字、带进程号",
    len(rb) == 1 and rb[0][0].endswith(".tmp") and rb[0][0] != rb[0][1]
    and str(os.getpid()) in rb[0][0], rb)
chk("D4 三个写口跑完都没有 .tmp 残件", tmps(LIB) == [], tmps(LIB))

print("── E. 两个真进程同时写同一篇稿：落地的必须是完整一份 ──")
# A3 / C2 才是这一题的**回归探测器**（拿旧写法跑一定红）；这一节是「就这样也照常」
# 那一半：两个真进程各写 60 次，最后落地的正文必须是**某一份完整的稿**、不是两份交织。
# 旧写法在这一节**可能**侥幸不红（谁先谁后由调度决定），所以不当探测器用，只当实机旁证。
PARENT = "父" * 400
CHILD = "子" * 400
CHILD_SRC = (
    "import sys\n"
    "sys.path.insert(0, sys.argv[1])\n"
    "import writer\n"
    "root, bid, body = sys.argv[2], sys.argv[3], sys.argv[4]\n"
    "for _ in range(60):\n"
    "    ok, m = writer.save(root, bid, body, rev=None)\n"
    "    if not ok:\n"
    "        raise SystemExit('save failed: %r' % (m,))\n"
)
procs = [subprocess.Popen([sys.executable, "-c", CHILD_SRC, ROOT, LIB, bid, CHILD],
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
         for _ in range(2)]
for _ in range(60):
    ok, _m = writer.save(LIB, bid, PARENT, rev=None)
    if not ok:
        break
kids = [(pr.wait(timeout=120), pr.communicate()[0][-300:]) for pr in procs]
final = writer.read_text(d)
chk("E1 两个进程并写之后，正文一定是某一份完整稿（不是两份交织）",
    final in (PARENT, CHILD), esc(final[:80]))
chk("E2 并写之后也不留 .tmp 残件", tmps(LIB) == [], tmps(LIB))
chk("E3 两个子进程都正常收工（没有把异常咽进临时件里）",
    all(rc == 0 for rc, _ in kids), kids)
meta_ok, meta_err = True, ""
try:
    json.load(open(writer.meta_path(d), encoding="utf-8"))
except Exception as e:                          # noqa: BLE001
    meta_ok, meta_err = False, "%s: %s" % (type(e).__name__, e)
chk("E4 并写之后 meta.json 仍是一份解得动的完整账", meta_ok, meta_err)

print("── F. 另两只手：meta 与册账各四个线程同时进来 ──────────")
# E 节是「两个真进程写同一篇正文」，这一节补的是另外两只手：AI 灵感回包走
# `remember_sparks`（写 meta）、界面上建书改名删书归章排序都写 `books.json`。
# 说清楚这一节钉的是什么：读-改-写没有锁，四次更新最后落地谁那一版由调度决定，
# 所以这里**不**断言「四本都在」，只断言「不写坏、不漏件、没有人把异常咽下去」。
N = 4
merrs = []
bar_m = threading.Barrier(N)


def meta_worker(i):
    try:
        bar_m.wait(timeout=10)
        for k in range(3):
            writer.remember_sparks(LIB, bid, ["角度%d-%d" % (i, k)])
    except Exception as e:                    # noqa: BLE001
        merrs.append("%s: %s" % (type(e).__name__, e))


mts = [threading.Thread(target=meta_worker, args=(i,)) for i in range(N)]
for t in mts:
    t.start()
for t in mts:
    t.join(timeout=30)
chk("F1 meta：四个线程并发写同一份 meta.json，没有一个写手抛异常", merrs == [], merrs[:3])
meta_shape, mwhy = True, ""
try:
    mdoc = json.load(open(writer.meta_path(d), encoding="utf-8"))
    meta_shape = isinstance(mdoc, dict) and mdoc.get("id") == bid
except Exception as e:                        # noqa: BLE001
    meta_shape, mwhy = False, "%s: %s" % (type(e).__name__, e)
chk("F2 meta：最后落地的那一份解得动、还是这本书的那份账", meta_shape is True, mwhy)
chk("F3 meta：并发之后沙盒里不留 .tmp 残件", tmps(LIB) == [], tmps(LIB))

berrs = []
bar_b = threading.Barrier(N)


def book_worker(i):
    try:
        bar_b.wait(timeout=10)
        writer.create_book(LIB, "并发的一本书%d" % i)
    except Exception as e:                    # noqa: BLE001
        berrs.append("%s: %s" % (type(e).__name__, e))


bts = [threading.Thread(target=book_worker, args=(i,)) for i in range(N)]
for t in bts:
    t.start()
for t in bts:
    t.join(timeout=30)
chk("F4 册账：四个线程并发建书（都写 books.json），没有一个写手抛异常", berrs == [], berrs[:3])
books_shape, bwhy = True, ""
try:
    books = writer.load_books(LIB)
    books_shape = isinstance(books, list) and all(b.get("id") for b in books)
except Exception as e:                        # noqa: BLE001
    books_shape, bwhy = False, "%s: %s" % (type(e).__name__, e)
chk("F5 册账：最后落地的那一份解得动、每本都有号", books_shape is True, bwhy)
chk("F6 册账：并发之后不留 .tmp 残件", tmps(LIB) == [], tmps(LIB))

print("── G. 接线：固定名真没了，写口真有好几个 ────────────────")
WSRC = read_repo("writer.py")
u_src = read_repo("ui_server.py")


def body_of(func):
    """截出一个模块级函数的函数体（到下一个顶层 `def` 或文件末为止）。"""
    m = re.search(r"(?m)^def %s\(" % re.escape(func), WSRC)
    if not m:
        return ""
    n = re.search(r"(?m)^def ", WSRC[m.end():])
    return WSRC[m.end():m.end() + n.start()] if n else WSRC[m.end():]


chk("G1 接线：writer.py 源码里再没有 `tmp = … + \".tmp\"` 这种固定临时名赋值",
    not re.search(r'tmp\s*=\s*[^\n]*\+\s*"\.tmp"', WSRC),
    re.findall(r'(?m)^.*tmp\s*=.*\.tmp.*$', WSRC)[:4])
wta_body = body_of("_write_text_atomic")
chk("G2 接线：三个写口都从 `_write_text_atomic` 出去，名字格式只有这一处（带 pid + 随机串）",
    "os.getpid()" in wta_body and "uuid.uuid4().hex" in wta_body
    and '"%s.%d.%s.tmp"' in wta_body
    and all('_write_text_atomic(' in body_of(f) for f in ("save", "_write_meta", "save_books")),
    wta_body[-200:])
chk("G3 接线：失败那条路兜的是 BaseException（中止信号也不漏件）",
    "except BaseException" in wta_body, wta_body[-160:])
chk("G4 接线：meta 确实有两个写口（界面存盘那次与 AI 灵感回包那次），并发不是假想",
    "writer_mod.save(" in u_src and "writer_mod.remember_sparks(" in u_src,
    (u_src.count("writer_mod.save("), u_src.count("writer_mod.remember_sparks(")))
acts = sum(u_src.count("writer_mod.%s(" % n)
           for n in ("create_book", "rename_book", "delete_book",
                     "add_chapter", "remove_chapter", "order_chapter"))
chk("G5 接线：册账动作真的有好几个请求入口（建 / 改名 / 删 / 归章 / 排序）", acts >= 4, acts)

print("\n写作落盘原子体检：%d 项通过，失败 %d 项" % (PASSED[0], len(FAIL)))
for f in FAIL:
    print("  ✗ " + f)
sys.exit(1 if FAIL else 0)
