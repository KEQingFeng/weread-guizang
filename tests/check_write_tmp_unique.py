# -*- coding: utf-8 -*-
"""导图落盘口（`mindmap._write_json`）：临时件不许共用固定名。

为什么这一条只管导图
    PR 原稿把「笔记 / 画板 / 导图」三格写在同一条门禁里，而那三格如今各自已经有更严的
    一份，同一把尺子不该量三遍：

      · 画板 —— `check_board_atomic.py`（合并的 `#155`）：六个写者并发、`.prev` 里也
        是完整内容、删板按形状把随机名残件一并收走且不碰别家板；
      · 笔记 —— `check_notes_atomic.py`（合并的 `#157`）：除了临时名，还钉「落盘失败要
        把已经挪去 `.prev` 的旧档搬回来」——`notes.json` 是这本书划线的唯一凭据，
        不能因为一次写崩就整个不见；
      · 视频转写 —— `check_video_atomic.py`（合并的 `#158`）。

    导图这一格原本只有《思维导图存盘收尾门禁》管「存进去的形状对不对」，临时名这一档
    没人钉，所以留在这里。

为什么要钉这一档
    `save_map` 有两个调用侧：界面的 `/api/mindmap`（`ui_server.py` 里 `save_map` 的
    同步那条）与 AI 画图那条任务线（同一个文件里另一处）。后端是 `ThreadingHTTPServer`，
    同一本书被两处同时存时，旧写法把临时件写死成 `path + ".tmp"`，先收尾的那个
    `os.replace` 把共用的临时件整个搬走，后一个轮到自己那一句时件已经不在 ——
    抛 `FileNotFoundError`，界面「保存失败」，可前一笔明明落盘了；反过来后开的那个
    也可能把前一个人写了一半的件截断，两份内容交织着顶成正式 `mindmap.json`。
    口径与 `ui_server._write_json` / `board._write_json` 一致：临时名带进程号 + 一小串
    随机，写失败先把残件收干净再抛，旧档始终完整。

这一条钉五面：
  A. 正常写：临时件名两次不一样、带进程号、以 `.tmp` 收尾，写完全目录无残件；
  B. 对照：两个写手共用同一个固定临时件名时，后一个 `os.replace` 当场
     `FileNotFoundError` —— 证明这道闸不是空转；
  C. 写失败：照旧抛、临时件收干净、旧档逐字节不动；
  D. 旁证：四个线程同时存同一本书的导图，没有一个写手抛异常、最后落地的那一份解得动、
     不留 `.tmp`；
  E. 接线：源码里那句固定名真的没了，两个调用侧真的都在（并发不是假想）。

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
    return tempfile.mkdtemp(prefix="guizang-mwtmp-")


def tmp_left(d):
    return sorted(n for n in os.listdir(d) if n.endswith(".tmp")) if os.path.isdir(d) else []


def parses(raw):
    """这份东西是不是解得动的 JSON —— 对照段用它证明「顶上去的是半截」。"""
    try:
        json.loads(raw)
        return True
    except Exception:               # noqa: BLE001
        return False


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


MAP_DOC = {"title": "图一", "nodes": [{"id": "a", "text": "根", "children": []}],
           "links": []}


def map_case(d):
    r = mindmap.save_map(d, MAP_DOC)
    return r.get("ok") is True


# ── A. 正常写：临时件一次一个名字 ────────────────────────────────────
print("── A. 正常写：临时件一次一个名字 ──────────────────────")
d = mk()
t1 = capture_tmps(lambda: map_case(d))
t2 = capture_tmps(lambda: map_case(d))
chk("A1 导图：写成功", bool(t1), t1)
chk("A2 导图：临时件与正式文件分开，以 .tmp 收尾",
    bool(t1) and not t1[0].endswith(".json") and t1[0].endswith(".tmp"), t1)
chk("A3 导图：两次写的临时件名不一样，且带着自己的进程号",
    bool(t1) and bool(t2) and t1[0] != t2[0] and str(os.getpid()) in t1[0], (t1, t2))
chk("A4 导图：进程号后面还跟着一串随机（同一进程里两次也不撞）",
    bool(re.search(r"\.%d\.[0-9a-f]{6,}\.tmp$" % os.getpid(), t1[0] if t1 else "")), t1)
chk("A5 导图：写完之后没有 .tmp 残件", tmp_left(d) == [], tmp_left(d))
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
with open(old, "w", encoding="utf-8") as f3:
    json.dump({"who": "甲", "pad": "A" * 40}, f3, ensure_ascii=False)
with open(old, "w", encoding="utf-8") as f4:      # 后来者一进去就把前一个人写好的截断
    f4.write('{"who": "乙"')                        # 只写到半截就被抢先收尾
os.replace(old, target)
landed = open(target, encoding="utf-8").read()
chk("B2 对照：共用 .tmp 时顶上去的是半截那一份，正式账解不动（旧写法真会把图写坏）",
    not parses(landed), landed[:80])
shutil.rmtree(d, ignore_errors=True)

# ── C. 写失败：照旧抛、临时件收干净、旧档不动 ───────────────────────
print("── C. 写失败：收干净、旧档不动 ────────────────────────")
d = mk()
p = os.path.join(d, "mindmap.json")
with open(p, "w", encoding="utf-8") as f:
    f.write('{"good": 1}')
before = open(p, "rb").read()
raised = None
try:
    mindmap._write_json(p, "坏" + HI)
except Exception as e:              # noqa: BLE001 —— 任何异常都算「照旧抛」
    raised = type(e).__name__
chk("C1 导图：真写不进去时照旧抛（不吞）", raised is not None, raised)
chk("C2 导图：写崩后临时件收干净了", tmp_left(d) == [], tmp_left(d))
chk("C3 导图：写崩不毁旧档（逐字节还在）",
    open(p, "rb").read() == before, open(p, "rb").read()[:60])
chk("C4 导图：旧档还在原地，没有被挪成 .prev 就丢", os.path.exists(p), sorted(os.listdir(d)))
shutil.rmtree(d, ignore_errors=True)

# ── D. 旁证：四个线程同时存同一本书的导图 ───────────────────────────
print("── D. 旁证：四线程并发存 ──────────────────────────────")
d = mk()
errs = []
N = 4
bar = threading.Barrier(N)


def worker(i):
    try:
        bar.wait(timeout=10)
        for k in range(3):
            # 每次的负载不一样，方便辨认最后落地的是哪一份；但都必须是合法 JSON。
            mindmap.save_map(d, {"title": "图%d-%d" % (i, k),
                                 "nodes": [{"id": "a", "text": str(i), "children": []}],
                                 "links": []})
    except Exception as e:          # noqa: BLE001
        errs.append("%s: %s" % (type(e).__name__, e))


ts = [threading.Thread(target=worker, args=(i,)) for i in range(N)]
for t in ts:
    t.start()
for t in ts:
    t.join(timeout=30)
chk("D1 导图：四个线程并发存，没有一个写手抛异常", errs == [], errs[:3])
chk("D2 导图：并发之后没有 .tmp 残件", tmp_left(d) == [], tmp_left(d))
target = os.path.join(d, "mindmap.json")
ok_parse = False
try:
    doc = json.load(open(target, encoding="utf-8"))
    # 落地的一定是某一个完整版本：标题带着「图i-k」的形状，不是两份字符搅在一起
    ok_parse = bool(re.match(r"^图\d+-\d+$", str(doc.get("title"))))
except Exception as e:              # noqa: BLE001
    ok_parse = "%s: %s" % (type(e).__name__, e)
chk("D3 导图：最后落地的那一份解得动、且是某一版的完整标题（不是两份交织）",
    ok_parse is True, ok_parse)
shutil.rmtree(d, ignore_errors=True)

# ── E. 接线：固定名那句真没了，两个调用侧真都在 ─────────────────────
print("── E. 接线 ────────────────────────────────────────────")
src = (REPO / "mindmap.py").read_text(encoding="utf-8")
body = src.split("def _write_json", 1)[-1].split("\ndef ", 1)[0]
chk("E1 接线：_write_json 里不再有 `tmp = path + \".tmp\"` 那句固定名",
    'tmp = path + ".tmp"' not in body, [l for l in body.splitlines() if ".tmp" in l][:3])
chk("E2 接线：临时名走进程号 + uuid（与 board / ui_server 同一把尺子）",
    "os.getpid()" in body and "uuid" in body, body[:200])
u_src = (REPO / "ui_server.py").read_text(encoding="utf-8")
callers = len(re.findall(r"mindmap\.save_map\(", u_src))
chk("E3 接线：save_map 确实有两个调用侧（界面那条与 AI 画图任务线），并发不是假想",
    callers >= 2, callers)

print()
if FAIL:
    print("✗ check_write_tmp_unique：%d 条没过" % len(FAIL))
    for n in FAIL:
        print("   - " + n)
    sys.exit(1)
print("✓ check_write_tmp_unique：%d 条全过" % PASSED[0])
