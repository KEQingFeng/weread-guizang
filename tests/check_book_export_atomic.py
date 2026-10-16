#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""归藏门禁：整本导出（EPUB / PDF）的成品落盘收口 —— 临时件一次一个名字。

为什么单开这一条
    `book_export` 是把书库里一本导成 EPUB / PDF 的地方，落点由调用方给：
    `ui_server.export_book` 算的是 `cache/downloads/<书号>.epub` / `.pdf`，而 `/api/epub`
    这条路由是在**请求线程里直接调**它的（不是 `start_task`），服务又是
    ThreadingHTTPServer —— 同一本书被点两下（双击 / 两个标签页）就是两条线程往同一个
    `dest` 上落。

    两个写口各有各的毛病，同属「落盘临时件」那一族（《交接说明》§2 那本账里
    `rg -F 'tmp = '` 那把尺子只量 `<目标> + ".tmp"`，`<目标> + ".part"` 从它眼皮底下漏了）：

      · `build_epub` 写的是**固定名** `dest + ".part"`。两条线程写的是同一份临时件：
        交替写会把这份 EPUB 写成混血；先收尾的那个 `os.replace` 把件搬走，后一个
        改名当场 `FileNotFoundError` —— 书其实导出来了，回给界面的却是「导出没做成」。
      · `build_pdf` 根本没有临时件：`page.pdf(path=dest)` 直写终稿。铬打到一半失败 /
        整趟被强杀，`dest` 上就留着半份 PDF，而它已经顶掉了上一份完整导出；两条线程
        同时打同一本，`dest` 是两份的交织。

    修法与仓库里其它格同一把尺子：临时件写成同目录的 `<目标>.<pid>.<八位随机>.part`，
    成功了再 `os.replace` 顶上，失败 / 中止（`BaseException`）在 `finally` 里收残件。

这一条钉住
  A. EPUB 本体：产物照旧是一份合法 EPUB（mimetype 首条且不压缩）；两个写者同时导
     同一本，一个都不扑空、第一刀改名那一刻盘上确实有**两份**不同的 `.part`；
  B. EPUB 失败路径：改名失败（ENOSPC）与中止信号（KeyboardInterrupt）都照旧往上抛，
     旧成品一个字不动、旁边不留 `.part`；
  C. PDF 本体（假铬内核，不开真浏览器）：铬拿到的 path 是 `<dest>.<pid>.<随机>.part`
     而不是 `dest`；打到一半失败时旧成品完整、不留 `.part`；两条线程同时导同一本都成；
  D. 源码口径：不再有 `tmp = dest + ".part"` 这个旧写法，两个写口都从
     `os.getpid()` + `uuid4` 拼临时名（一处真相）。

沙盒在系统临时目录里，不碰用户真实书库、不联网、不开浏览器，故归 --fast 静态段。

跑法：.venv/bin/python tests/check_book_export_atomic.py
"""
import atexit
import contextlib
import os
import pathlib
import shutil
import sys
import tempfile
import threading
import types
import zipfile

HERE = pathlib.Path(__file__).resolve().parent
REPO = HERE.parent
sys.path.insert(0, str(REPO))

import book_export as BX  # noqa: E402

# 自带沙盒：不碰用户真实书库 / cache。
SB = pathlib.Path(tempfile.mkdtemp(prefix="gz-export-atomic-"))
atexit.register(lambda: shutil.rmtree(SB, ignore_errors=True))

FAIL = []
PASSED = 0


def chk(name, cond, extra=""):
    global PASSED
    print(("PASS  " if cond else "FAIL  ") + name + ("" if cond else "   " + str(extra)[:220]))
    if cond:
        PASSED += 1
    else:
        FAIL.append(name)


def make_book(bid="bk0001", chapters=2):
    d = SB / "books" / bid
    (d / "chapters").mkdir(parents=True, exist_ok=True)
    for i in range(chapters):
        (d / "chapters" / ("%04d.md" % i)).write_text(
            "# 第 %d 章\n\n这一章有正文。\n" % (i + 1), encoding="utf-8")
    return str(d)


def parts_in(dest):
    folder = os.path.dirname(dest)
    if not os.path.isdir(folder):
        return []
    return sorted(n for n in os.listdir(folder) if n.endswith(".part"))


def good_epub(path):
    try:
        with zipfile.ZipFile(path) as z:
            if z.testzip() is not None:
                return False
            names = z.namelist()
            if not names or names[0] != "mimetype":
                return False
            return z.getinfo("mimetype").compress_type == zipfile.ZIP_STORED
    except Exception:
        return False


# ─────────────────────────── A / B：EPUB 写口 ───────────────────────────
BOOK = make_book("bk0001")
DEST_DIR = SB / "downloads"
DEST_DIR.mkdir(parents=True, exist_ok=True)

DEST = str(DEST_DIR / "bk0001.epub")
BX.build_epub(BOOK, DEST, title="正常一本")
chk("A1 正常导出照旧是一份合法 EPUB（mimetype 首条且不压缩）",
    good_epub(DEST) and parts_in(DEST) == [], (good_epub(DEST), parts_in(DEST)))

# 两个写者同时导同一本：卡在 os.replace 入口，两边都写完临时件、都还没改名。
RACE_DEST = str(DEST_DIR / "race.epub")


def run_two(dest, book=BOOK):
    res = {}
    lock = threading.Lock()
    arrive = threading.Barrier(2)
    snap = []
    orig = BX.os.replace

    def racy(src, dst):
        arrive.wait(timeout=30)                     # 两边都写完各自的临时件
        with lock:
            snap.append(parts_in(dst))
        return orig(src, dst)

    BX.os.replace = racy
    try:
        def worker(i):
            try:
                BX.build_epub(book, dest, title="并发一本")
                res[i] = "ok"
            except BaseException as e:
                res[i] = "%s: %s" % (type(e).__name__, e)
        ts = [threading.Thread(target=worker, args=(i,)) for i in (1, 2)]
        for t in ts:
            t.start()
        for t in ts:
            t.join(40)
    finally:
        BX.os.replace = orig
    return res, snap


res, snap = run_two(RACE_DEST)
chk("A2 同一个目标两个写者同时导，两个都回 ok（固定名必有一个扑空）",
    res.get(1) == "ok" and res.get(2) == "ok", res)
chk("A3 第一刀改名那一刻，盘上确实有两份不同的 .part（名字一次一个）",
    snap and max(len(s) for s in snap) == 2, (snap, parts_in(RACE_DEST)))
chk("A4 并发收尾之后不留任何 .part，落下的是一份合法 EPUB",
    parts_in(RACE_DEST) == [] and good_epub(RACE_DEST), (parts_in(RACE_DEST), good_epub(RACE_DEST)))

# 失败 / 中止：旧成品必须原样，旁边不留残件，异常照旧往上抛。
OLD = b"OLD-EPUB-CONTENT"
for marker, exc, label in (("B1", OSError(28, "No space left on device"), "改名失败"),
                           ("B2", KeyboardInterrupt(), "中止信号")):
    dest = str(DEST_DIR / ("fail-%s.epub" % marker))
    with open(dest, "wb") as f:
        f.write(OLD)
    orig = BX.os.replace

    def boom(src, dst, _exc=exc):
        raise _exc

    BX.os.replace = boom
    thrown = None
    try:
        BX.build_epub(BOOK, dest, title="失败一本")
    except BaseException as e:
        thrown = e
    finally:
        BX.os.replace = orig
    same = open(dest, "rb").read() == OLD
    chk("%s %s：异常照旧往上抛、旧成品一字不动、不留 .part" % (marker, label),
        isinstance(thrown, type(exc)) and same and parts_in(dest) == [],
        (type(thrown).__name__ if thrown else None, same, parts_in(dest)))


# ─────────────────────── C：PDF 写口（假铬内核）───────────────────────
class _FakePage:
    def __init__(self, sink):
        self.sink = sink

    def goto(self, url, timeout=None):
        return None

    def pdf(self, path=None, **kw):
        self.sink(path)


class _FakeBrowser:
    def __init__(self, sink):
        self.sink = sink

    def new_page(self):
        return _FakePage(self.sink)

    def close(self):
        return None


class _FakeChromium:
    def __init__(self, sink):
        self.sink = sink

    def launch(self):
        return _FakeBrowser(self.sink)


class _FakePW:
    def __init__(self, sink):
        self.chromium = _FakeChromium(sink)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


@contextlib.contextmanager
def fake_playwright(sink):
    """把 build_pdf 里那口 `from playwright.sync_api import sync_playwright` 换成假的。

    只记下铬被要求写到哪个 path（`sink(path)`），不开真浏览器。
    """
    saved = {k: sys.modules.get(k) for k in ("playwright", "playwright.sync_api")}
    mod = types.ModuleType("playwright.sync_api")
    mod.sync_playwright = lambda: _FakePW(sink)
    pkg = types.ModuleType("playwright")
    pkg.sync_api = mod
    sys.modules["playwright"] = pkg
    sys.modules["playwright.sync_api"] = mod
    try:
        yield
    finally:
        for k, v in saved.items():
            if v is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v


PDF_DEST = str(DEST_DIR / "bk0001.pdf")
seen = []


def write_pdf(path):
    seen.append(path)
    with open(path, "wb") as f:
        f.write(b"%PDF-1.4 fake\n%%EOF\n")


with fake_playwright(write_pdf):
    BX.build_pdf(BOOK, PDF_DEST, title="正常一本")
chk("C1 铬被要求写到同目录的随机 .part，改完名才有正式成品",
    len(seen) == 1 and seen[0] != PDF_DEST and seen[0].endswith(".part")
    and os.path.dirname(seen[0]) == os.path.dirname(PDF_DEST),
    seen)
chk("C1b 改完名 dest 是那一份 PDF，旁边不留 .part",
    os.path.isfile(PDF_DEST) and open(PDF_DEST, "rb").read().startswith(b"%PDF")
    and parts_in(PDF_DEST) == [], (parts_in(PDF_DEST),))

# 铬打到一半失败：旧成品完整、不留 .part、异常照抛。
PDF_OLD = str(DEST_DIR / "old.pdf")
with open(PDF_OLD, "wb") as f:
    f.write(b"OLD-PDF-CONTENT")


def half_pdf(path):
    with open(path, "wb") as f:
        f.write(b"%PDF-1.4 half")
    raise OSError(5, "chrome died")


thrown = None
try:
    with fake_playwright(half_pdf):
        BX.build_pdf(BOOK, PDF_OLD, title="失败一本")
except BaseException as e:
    thrown = e
chk("C2 铬打到一半失败：异常照抛、旧成品一字不动、不留 .part",
    isinstance(thrown, OSError) and open(PDF_OLD, "rb").read() == b"OLD-PDF-CONTENT"
    and parts_in(PDF_OLD) == [], (type(thrown).__name__ if thrown else None, parts_in(PDF_OLD)))

# 两条线程同时导同一本 PDF：都成，落下的是一整份。
PDF_RACE = str(DEST_DIR / "race.pdf")
pdf_res = {}
pdf_lock = threading.Lock()
pdf_arrive = threading.Barrier(2)
pdf_seen = []


def slow_pdf(path):
    with pdf_lock:
        pdf_seen.append(path)
    pdf_arrive.wait(timeout=30)
    with open(path, "wb") as f:
        f.write(b"%PDF-1.4 race\n%%EOF\n")


with fake_playwright(slow_pdf):
    def pdf_worker(i):
        try:
            BX.build_pdf(BOOK, PDF_RACE, title="并发一本")
            pdf_res[i] = "ok"
        except BaseException as e:
            pdf_res[i] = "%s: %s" % (type(e).__name__, e)
    ts = [threading.Thread(target=pdf_worker, args=(i,)) for i in (1, 2)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(40)


def one_pdf(path):
    try:
        with open(path, "rb") as f:
            blob = f.read()
        return blob.startswith(b"%PDF-1.4 race") and blob.endswith(b"%%EOF\n")
    except OSError:
        return False


chk("C3 同一个 PDF 两个写者同时导：都回 ok，落下的是一整份",
    pdf_res.get(1) == "ok" and pdf_res.get(2) == "ok"
    and len(set(pdf_seen)) == 2 and one_pdf(PDF_RACE) and parts_in(PDF_RACE) == [],
    (pdf_res, pdf_seen, parts_in(PDF_RACE)))


# ───────────────────────── D：源码口径（旧尺子量不到）─────────────────────────
SRC = (REPO / "book_export.py").read_text(encoding="utf-8")
chk("D1 源码里不再有 `tmp = dest + \".part\"` 这个固定名写法",
    'tmp = dest + ".part"' not in SRC, "旧写法还在")
chk("D2 两个写口的临时名都从 os.getpid() + uuid 拼出来（一处真相）",
    SRC.count('tmp = "%s.%d.%s.part" % (dest, os.getpid(), uuid.uuid4().hex[:8])') == 2,
    SRC.count('tmp = "%s.%d.%s.part" % (dest, os.getpid(), uuid.uuid4().hex[:8])'))

print()
print("整本导出成品落盘：%d 项通过，失败 %d 项" % (PASSED, len(FAIL)))
if FAIL:
    print("未过：" + " / ".join(FAIL))
sys.exit(1 if FAIL else 0)
