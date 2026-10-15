# -*- coding: utf-8 -*-
"""flomo 另外两个落盘口（`to_shelf` 那份 meta.json / `write_portrait` 两份画像）。

为什么单开这一条（`check_flomo_atomic.py` 只管账本 `save()`）
    `check_flomo_atomic.py` 收的是 `cache/flomo/notes.json` 这一本账。flomo 这条路上
    还有两处文件写口，临时件都写的是**固定名**：

      · `to_shelf(flomo_dir, memo_id, out_root)` —— 把一条便签收成书架里的一本书。同一本
        书目录里的 `meta.json` 用 `mp + ".tmp"`；两条笔记被收进**同一个书目录**（同一个
        假 import 返回同一本书，或旧版本留下的同名书）时，两个写手落到同一个临时件上，
        后果与账本那一格一模一样：先收尾的 `os.replace` 把件整个搬走，后一个轮到自己那句
        时件已经不在，抛 `FileNotFoundError` —— 书进了书架，回包却是「收进书架失败」；
        或者两份内容交织顶成正式 meta。
      · `write_portrait(flomo_dir)` —— 画像 JSON 与 Markdown 用 `name + ".tmp"`。画像缺失
        时 `read_portrait` 会现算一份落盘，而 Agent 的读画像请求可以两条同时进来，两条都
        走 `write_portrait`，同一个固定名同样会有一个 rename 扑空。

收口
    两处都改走 `_write_text_atomic`（与账本 `save()` 共用 `_tmp_for`）：临时名 = 正式名 +
    pid + 一小串随机，写崩先把件收干净再抛，旧档在整个过程里始终完整。`save()` 保持用
    `json.dump` 往临时件里流式写（`check_flomo_atomic.py` 拿 `json.dump` 当探针），只把
    命名交给 `_tmp_for`，不重复第二份名字格式。

钉六面：
  A. `_write_text_atomic` 本身：写得下、读得回、不留 `.tmp`；两次写的临时名不一样且带
     pid；替换发生之前目标文件一直是完整的那一份；写崩收干净、旧档不动。
  B. `to_shelf` 接线：走 `_write_text_atomic`，源码里不再有 `mp + ".tmp"`；两次收书给
     出的临时名不同（拿旧固定名跑这一条必红）。`_tmp_for` 是这一族唯一的名字来源：带
     pid + 随机串，`save()` 也用它 —— 落在 B3/B4/B5。
  C. `to_shelf` 并写：两个真线程把两条笔记收进同一个书目录，meta.json 一定解得动、不留
     `.tmp`、都不抛。
  D. `write_portrait` 接线与并写：两个真线程各写几十趟，画像两份都解得动、不留 `.tmp`。
  E. 画像写到一半被中止信号打断：JSON 已经顶替、Markdown 还没顶替时照旧抛，半截临时件
     收干净，旧的那份画像解得动 —— `_write_text_atomic` 兜的是 BaseException，只兜
     Exception 会在 Ctrl-C / 「停止」那条退出路径上漏件。
  F. 接线：界面点「生成画像」真的走 `write_portrait`，`read_portrait` 在文件缺失时现算
     并落盘 —— D 节那两只同时进来的手不是假想出来的。

跑法：纯函数 + 系统临时目录沙盒，不联网、不开浏览器、不起服务，归 --fast 静态段。
"""
import atexit
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
import flomo_notes as flomo  # noqa: E402

SB = pathlib.Path(tempfile.mkdtemp(prefix="guizang-flomo-text-atomic-"))
atexit.register(lambda: shutil.rmtree(SB, ignore_errors=True))
FD = str(SB / "flomo")
BOOKS = str(SB / "books")
FAIL = []
PASSED = [0]


def chk(name, ok, extra=""):
    if ok:
        PASSED[0] += 1
        print("  ok  " + name)
    else:
        FAIL.append(name)
        print(" FAIL " + name + (("  <- " + esc(extra)[:220]) if extra else ""))


def esc(s):
    return str(s).encode("unicode_escape").decode("ascii")


def read_repo(fname):
    with open(os.path.join(str(REPO), fname), encoding="utf-8") as f:
        return f.read()


def body_of(src, func):
    """截出一个模块级函数的函数体（到下一个顶层 `def` 或文件末为止）。"""
    m = re.search(r"(?m)^def %s\(" % re.escape(func), src)
    if not m:
        return ""
    n = re.search(r"(?m)^def ", src[m.end():])
    return src[m.end():m.end() + n.start()] if n else src[m.end():]


def tmps_in(d):
    return sorted(n for n in os.listdir(d) if n.endswith(".tmp")) if os.path.isdir(d) else []


class ReplaceSpy:
    """记录 `os.replace(src, dst)` 的 src 与替换前 dst 的原始内容。

    临时件名只有进到 replace 这一刻才认得出来；写成固定名的旧代码，两次写会记到同一个
    src —— 这就是本条的回归探测器。`flomo` 与测试同处一个 `os` 模块，退出时务必还原。
    """

    def __init__(self):
        self.real = os.replace
        self.calls = []

    def __enter__(self):
        spy = self

        def wrapper(src, dst, *a, **kw):
            old = None
            try:
                with open(dst, "rb") as f:
                    old = f.read()
            except OSError:
                old = None
            spy.calls.append({"src": os.path.basename(str(src)), "dst": str(dst), "old": old})
            return spy.real(src, dst, *a, **kw)

        os.replace = wrapper
        return self

    def __exit__(self, *exc):
        os.replace = self.real
        return False


print("── A. `_write_text_atomic` 本身：独名、原子、写崩收干净 ──────────")
T = str(SB / "plain.txt")
before = "上一份完整的内容"
with open(T, "w", encoding="utf-8") as f:
    f.write(before)

with ReplaceSpy() as sp1:
    flomo._write_text_atomic(T, "第一份")
with open(T, encoding="utf-8") as f:
    got1 = f.read()
chk("A1 写得下、读得回、正常写完之后不留 .tmp",
    got1 == "第一份" and tmps_in(str(SB)) == [], (got1, tmps_in(str(SB))))
chk("A2 临时件与目标分开、名字带 pid 与随机串",
    len(sp1.calls) == 1 and sp1.calls[0]["src"] != os.path.basename(T)
    and sp1.calls[0]["src"].endswith(".tmp") and str(os.getpid()) in sp1.calls[0]["src"],
    sp1.calls)
chk("A3 替换之前目标一直是完整的那一份（原子替换那一半没坏）",
    len(sp1.calls) == 1 and sp1.calls[0]["old"] == before.encode("utf-8"),
    sp1.calls)

with ReplaceSpy() as sp2:
    flomo._write_text_atomic(T, "第二份")
with ReplaceSpy() as sp3:
    flomo._write_text_atomic(T, "第三份")
chk("A4 每次写的临时件名都不一样（固定名正是扑空的根）",
    len(sp2.calls) == 1 and len(sp3.calls) == 1
    and sp2.calls[0]["src"] != sp3.calls[0]["src"],
    (sp2.calls, sp3.calls))

with open(T, encoding="utf-8") as f:
    keep = f.read()
raised = None
try:
    flomo._write_text_atomic(T, object())     # 写口只收 str，必然写不进去
except TypeError:
    raised = "TypeError"
except Exception as e:                        # noqa: BLE001 —— 别的异常同样算没接住
    raised = type(e).__name__
with open(T, encoding="utf-8") as f:
    after = f.read()
chk("A5 真写不进去时照旧抛（不吞异常、不静默丢这一趟）", raised == "TypeError", raised)
chk("A6 写崩后临时件收干净了", tmps_in(str(SB)) == [], tmps_in(str(SB)))
chk("A7 写崩不毁旧档（旧的一份逐字节还在）", after == keep, after[:80])

print("── B. to_shelf 接线：不再用固定临时名 ─────────────────────")
SRC = read_repo("flomo_notes.py")
ts_body = body_of(SRC, "to_shelf")
wp_body = body_of(SRC, "write_portrait")
sv_body = body_of(SRC, "save")
tf_body = body_of(SRC, "_tmp_for")
chk("B1 to_shelf 走 _write_text_atomic，函数体里再没有 `tmp = … + \".tmp\"` 赋值",
    "_write_text_atomic(mp," in ts_body
    and not re.search(r'tmp\s*=\s*[^\n]*\+\s*"\.tmp"', ts_body),
    ts_body[-260:])
chk("B2 write_portrait 走 _write_text_atomic，函数体里再没有 `tmp = … + \".tmp\"` 赋值",
    "_write_text_atomic(os.path.join(d, name), text)" in wp_body
    and not re.search(r'tmp\s*=\s*[^\n]*\+\s*"\.tmp"', wp_body), wp_body[-260:])
chk("B3 save 的临时名也来自 _tmp_for（名字格式只有这一处）",
    "_tmp_for(dest)" in sv_body and not re.search(r'"%s\.%d\.%s\.tmp"', sv_body),
    sv_body[:120])
chk("B4 _tmp_for 带 pid + 随机串，且仍是「正式名 + .tmp」形状",
    "os.getpid()" in tf_body and "uuid.uuid4().hex" in tf_body
    and 'return "%s.%d.%s.tmp" % (path, os.getpid(), uuid.uuid4().hex[:8])' in tf_body,
    tf_body[:200])
chk("B5 flomo_notes.py 源码里再没有 `tmp = … + \".tmp\"` 这种固定临时名赋值",
    not re.search(r'tmp\s*=\s*[^\n]*\+\s*"\.tmp"', SRC),
    re.findall(r'(?m)^.*tmp\s*=.*\.tmp.*$', SRC)[:4])


def memo(mid):
    return {"id": mid, "md": "#读书 收成书这条 %s" % mid, "tags": ["读书"],
            "date": "2026-08-30", "clock": "06:30:10", "words": 12, "chars": 12,
            "atts": [], "imgs": [], "ts": 1792000000}


BOOK_ID = "flomo_samebook"


def fake_import(out_root, filename, blob, title=None, author=None,
                book_id_prefix="", source=""):
    """两条笔记都收进**同一本书目录** —— 这正是固定临时名会撞的那一格。"""
    d = os.path.join(out_root, BOOK_ID)
    os.makedirs(d, exist_ok=True)
    return {"id": BOOK_ID, "dir": d, "title": title}


flomo.save(FD, {"memos": [memo("m1"), memo("m2")]})
with ReplaceSpy() as s1:
    i1 = flomo.to_shelf(FD, "m1", BOOKS, import_fn=fake_import)
with ReplaceSpy() as s2:
    i2 = flomo.to_shelf(FD, "m2", BOOKS, import_fn=fake_import)
mp = os.path.join(i1["dir"], "meta.json")
metas = [c for c in s1.calls + s2.calls if c["dst"] == mp]
chk("B6 两次收书都写的是同一本 metadata（探针落在点上）",
    i1["dir"] == i2["dir"] == os.path.join(BOOKS, BOOK_ID) and len(metas) == 2, metas)
chk("B7 两次收书给出的临时件名不一样（拿旧的固定名跑这一条必红）",
    len(metas) == 2 and metas[0]["src"] != metas[1]["src"]
    and metas[0]["src"].endswith(".tmp") and str(os.getpid()) in metas[0]["src"],
    metas)
with open(mp, encoding="utf-8") as f:
    meta_now = json.load(f)
chk("B8 收完之后 meta.json 是完整合法的一份，书目录里不留 .tmp",
    meta_now.get("source") == "flomo" and meta_now.get("memo_id") == "m2"
    and tmps_in(i1["dir"]) == [], (meta_now, tmps_in(i1["dir"])))

print("── C. to_shelf 并写：两条笔记收进同一本书目录 ────────────")
FD2 = str(SB / "flomo2")
BOOKS2 = str(SB / "books2")
flomo.save(FD2, {"memos": [memo("n1"), memo("n2"), memo("n3"), memo("n4")]})
cerr = []


def clip(mid):
    try:
        flomo.to_shelf(FD2, mid, BOOKS2, import_fn=fake_import)
    except Exception as e:                    # noqa: BLE001
        cerr.append("%s: %s: %s" % (mid, type(e).__name__, e))


ths = [threading.Thread(target=clip, args=(m,)) for m in ("n1", "n2", "n3", "n4")]
for t in ths:
    t.start()
for t in ths:
    t.join(timeout=60)
mdir = os.path.join(BOOKS2, BOOK_ID)
ok, why = True, ""
try:
    with open(os.path.join(mdir, "meta.json"), encoding="utf-8") as f:
        json.load(f)
except Exception as e:                        # noqa: BLE001
    ok, why = False, "%s: %s" % (type(e).__name__, e)
chk("C1 四条笔记两两并发收进同一本书，meta.json 一定解得动", ok, why)
chk("C2 并写之后书目录里不留 .tmp 残件", tmps_in(mdir) == [], tmps_in(mdir))
chk("C3 并写的四个线程都正常收工（没有把异常咽进临时件里）", cerr == [], cerr[:2])

print("── D. write_portrait：两个线程各写几十趟 ────────────────")
FD3 = str(SB / "flomo3")
flomo.save(FD3, {"memos": [memo("p1"), memo("p2")]})
pdir = flomo.portrait_dir(FD3)
perr = []


def port(tag):
    try:
        for _ in range(40):
            flomo.write_portrait(FD3)
    except Exception as e:                    # noqa: BLE001
        perr.append("%s: %s: %s" % (tag, type(e).__name__, e))


pths = [threading.Thread(target=port, args=(w,)) for w in ("甲", "乙")]
for t in pths:
    t.start()
for t in pths:
    t.join(timeout=90)
parsed, pwhy = True, ""
for name in (flomo.PORTABLE_JSON, flomo.PORTABLE_MD):
    try:
        with open(os.path.join(pdir, name), encoding="utf-8") as f:
            txt = f.read()
        if name == flomo.PORTABLE_JSON:
            json.loads(txt)
        elif not txt.strip():
            raise ValueError("空的 Markdown")
    except Exception as e:                    # noqa: BLE001
        parsed, pwhy = False, "%s: %s: %s" % (name, type(e).__name__, e)
chk("D1 两个线程各写 40 趟，画像两份都解得动", parsed, pwhy)
chk("D2 并写之后画像目录里不留 .tmp 残件", tmps_in(pdir) == [], tmps_in(pdir))
chk("D3 两个线程都正常收工", perr == [], perr[:2])

print("── E. 画像写到一半被打断：中止信号也不漏件 ──────────────")
# 画像一次写两份文件（JSON 给机器、Markdown 给人和 Agent），所以它是这一族里唯一
# 能造出「前一份已经顶替、后一份改名失败」那种半路的格子。`_write_text_atomic` 兜的是
# `except BaseException`：Ctrl-C 与界面上的「停止」走的是 KeyboardInterrupt / SystemExit，
# 它们不继承 Exception，只兜 Exception 会在退出路径上把半截临时件留在盘上。
FD4 = str(SB / "flomo4")
flomo.save(FD4, {"memos": [memo("q1"), memo("q2")]})
pdir4 = flomo.portrait_dir(FD4)
flomo.write_portrait(FD4)
JP4 = os.path.join(pdir4, flomo.PORTABLE_JSON)
MP4 = os.path.join(pdir4, flomo.PORTABLE_MD)
with open(JP4, "rb") as f:
    before_j = f.read()

real_replace = os.replace
boom = {"n": 0}


def replace_that_stops(src, dst, *a, **kw):
    boom["n"] += 1
    if boom["n"] == 2:                       # 第一份（JSON）照常顶替，第二份（MD）改名时被打断
        raise KeyboardInterrupt()
    return real_replace(src, dst, *a, **kw)


os.replace = replace_that_stops
raised = None
try:
    flomo.write_portrait(FD4)
except KeyboardInterrupt:
    raised = "KeyboardInterrupt"
except BaseException as e:                   # noqa: BLE001 —— 别的异常也算没按原样抛出去
    raised = type(e).__name__
finally:
    os.replace = real_replace
chk("E1 第二份改名时收到中止信号（KeyboardInterrupt），照旧抛不吞",
    raised == "KeyboardInterrupt", raised)
chk("E2 JSON 已顶替、Markdown 没顶替时，半截 .tmp 也收干净了",
    tmps_in(pdir4) == [], tmps_in(pdir4))
e3, e3why = True, ""
try:
    with open(JP4, encoding="utf-8") as f:
        e3 = isinstance(json.load(f), dict)
except Exception as e:                       # noqa: BLE001
    e3, e3why = False, "%s: %s" % (type(e).__name__, e)
chk("E3 打断之后落地的画像 JSON 逐字节没动、仍是解得动的一份",
    e3 is True and open(JP4, "rb").read() == before_j, e3why)

print("── F. 接线：同时写画像的两只手都真的在 ──────────────────")
u_src = read_repo("ui_server.py")
chk("F1 接线：界面那次「生成画像」（mode=portrait）真的调 write_portrait 落盘",
    'mode == "portrait"' in u_src and "flomo_notes.write_portrait(" in u_src,
    (u_src.count('mode == "portrait"'), u_src.count("flomo_notes.write_portrait(")))
chk("F2 接线：画像的两份都在同一个循环里，改名出口只有 `_write_text_atomic` 这一处",
    wp_body.count("_write_text_atomic(") == 1
    and "for name, text in" in wp_body
    and "PORTABLE_JSON" in wp_body.split("for name, text in", 1)[-1]
    and "PORTABLE_MD" in wp_body.split("for name, text in", 1)[-1],
    wp_body.split("for name, text in", 1)[-1][:200])
chk("F3 接线：`_write_text_atomic` 失败路径兜的是 BaseException（中止信号也要收残件）",
    "except BaseException" in body_of(SRC, "_write_text_atomic"),
    body_of(SRC, "_write_text_atomic")[-160:])
for name in (flomo.PORTABLE_JSON, flomo.PORTABLE_MD):
    os.remove(os.path.join(pdir4, name))
got = flomo.read_portrait(FD4)
chk("F4 接线：read_portrait 在画像缺失时现算并落盘（删掉两份再读，两份都回来了）",
    isinstance(got, dict) and os.path.isfile(JP4) and os.path.isfile(MP4)
    and tmps_in(pdir4) == [], (type(got).__name__, tmps_in(pdir4)))

print("\nflomo 收书 meta / 画像落盘体检：%d 项通过，失败 %d 项" % (PASSED[0], len(FAIL)))
for f in FAIL:
    print("  ✗ " + f)
sys.exit(1 if FAIL else 0)
