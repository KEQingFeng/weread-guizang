# -*- coding: utf-8 -*-
"""视频转写那两个落盘口（`video_note._write_json` / `_write_text`）：临时件不许共用固定名。

为什么单开一条门禁
    `transcript.json` / `transcript.txt` / `merged.md` / `meta.json` 这几份不是一个人在写：

      · `/api/video` 的「保存转写 / 重建」跑在服务进程的线程里，后端是
        `ThreadingHTTPServer`，工作台连点两下保存、或保存与重建前后脚进来，两个
        请求就同时进 `save_transcript` / `rebuild_book`（这两条路都没走
        `ui_server._WRITE_LOCK`）；
      · 下载转写那条任务线是**另一个进程**（`ui_server.start_task` 起
        `video_note.py --task`），收尾落盘时也写同一批文件。

    旧写法把临时件写死成 `path + ".tmp"`，两个写手同时进来时有两种代价：

      · **当场抛**：先收尾的那个 `os.replace` 把共用的临时件整个搬走，后一个轮到
        自己那一句时临时件已经不在 —— 抛 `FileNotFoundError`，界面看到「保存失败」，
        可前一笔其实已经落盘；
      · **写坏**：后来者 `open(..., "w")` 把前一个人写了一半的临时件截断，两边各自
        按自己的偏移往下写，`os.replace` 把两份内容交织的那一份顶成正式文件。

    第二半同样要收口：写入这一步失败（序列化不了、落单码点这类）时旧写法直接把异常
    抛出去，临时件留在书目录里没人认领。

这一条钉四面：
  A. 正常写：每次临时件名都不一样、带进程号、以 `.tmp` 收尾，写完全目录无残件；
  B. 对照：两个写手共用同一个固定临时件名时，后一个 `os.replace` 当场
     `FileNotFoundError` —— 证明这道闸不是空转；
  C. 写失败：照旧抛、临时件收干净、旧档逐字节不动（json 与 text 两个口各一遍）；
  D. 多线程并写同一个口：不抛、最后落地的一定解得动、不留 `.tmp`；
  E. `_write_json` 照旧把解析缓存踢掉，「保存完再读还是旧字」那条鬼影没回来。

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
import video_note as vn  # noqa: E402

FAIL = []
PASSED = [0]


def chk(name, cond, extra=""):
    if cond:
        PASSED[0] += 1
        print("  ok  " + name)
    else:
        FAIL.append(name)
        print("  ✗  " + name + ("  " + str(extra) if extra else ""))


def tmp_left(d):
    return sorted(p for p in os.listdir(d) if p.endswith(".tmp"))


def capture_writes(fn):
    """跑 fn，返回它这一趟 open 出去的写路径（临时件名）。只夹这个窗口。"""
    seen = []
    real = builtins.open

    def spy(file, mode="r", *a, **k):
        if any(c in str(mode) for c in "wax"):
            seen.append(os.fspath(file) if hasattr(os, "fspath") else str(file))
        return real(file, mode, *a, **k)

    builtins.open = spy
    try:
        fn()
    finally:
        builtins.open = real
    return seen


def no_raise(fn, *a, **k):
    try:
        fn(*a, **k)
        return True, None
    except Exception as e:          # noqa: BLE001 —— 这条体检就是要看它抛不抛
        return False, "%s: %s" % (type(e).__name__, e)


ROOT = tempfile.mkdtemp(prefix="guizang-video-atomic-")
try:
    BOOK = os.path.join(ROOT, "video_bilibili_demo")
    os.makedirs(BOOK, exist_ok=True)
    JP = os.path.join(BOOK, "transcript.json")
    TP = os.path.join(BOOK, "transcript.txt")

    # ── A 正常写：一次一个名字，不留残件 ─────────────────────────────
    names = capture_writes(lambda: (vn._write_json(JP, {"segments": [{"id": "a"}]}),
                                    vn._write_json(JP, {"segments": [{"id": "b"}]}),
                                    vn._write_text(TP, "第一版\n")))
    chk("A1 三次写各有一个临时件名（没有共用固定名）",
        len(names) == 3 and len(set(names)) == 3, names)
    chk("A2 临时件名带进程号、以 .tmp 收尾",
        all((".%d." % os.getpid()) in n and n.endswith(".tmp") for n in names), names)
    chk("A3 正式文件名本身不会被当成临时件（名字确实贴出加了后缀）",
        all(n not in (JP, TP) for n in names), names)
    chk("A4 写完书目录里不留 .tmp 残件", tmp_left(BOOK) == [], tmp_left(BOOK))
    chk("A5 最后一份内容完整落盘",
        json.load(open(JP, encoding="utf-8")) == {"segments": [{"id": "b"}]}
        and open(TP, encoding="utf-8").read() == "第一版\n")

    # ── B 对照：固定名在同样交错下确实会扑空 ─────────────────────────
    ctl = os.path.join(BOOK, "control.json")
    fixed = ctl + ".tmp"
    open(fixed, "w", encoding="utf-8").write('{"who": 1}')   # 写手一
    open(fixed, "w", encoding="utf-8").write('{"who": 2}')   # 写手二把临时件截断
    os.replace(fixed, ctl)                                    # 写手一收尾，临时件被搬走
    ok, why = no_raise(os.replace, fixed, ctl)                # 写手二收尾 → 扑空
    chk("B1 对照：两个写手共用固定临时件名时，后一个 os.replace 抛 FileNotFoundError",
        ok is False and "FileNotFoundError" in str(why), why)

    # ── C 写失败：抛、收干净、旧档一个字不动 ─────────────────────────
    before_json = open(JP, "rb").read()
    ok, why = no_raise(vn._write_json, JP, {"bad": {1, 2, 3}})     # set 没法 json.dump
    chk("C1 序列化不了照旧抛", ok is False and why, why)
    chk("C2 json 写失败后不留 .tmp", tmp_left(BOOK) == [], tmp_left(BOOK))
    chk("C3 json 写失败后旧档逐字节不动", open(JP, "rb").read() == before_json)

    before_txt = open(TP, "rb").read()
    ok, why = no_raise(vn._write_text, TP, b"\xff\xfe")            # bytes 没法 write 成文本
    chk("C4 文本写不进照旧抛", ok is False and why, why)
    chk("C5 文本写失败后不留 .tmp", tmp_left(BOOK) == [], tmp_left(BOOK))
    chk("C6 文本写失败后旧档逐字节不动", open(TP, "rb").read() == before_txt)

    # ── D 多线程并写同一个口：不抛、最后解得动 ───────────────────────
    N = 40
    start = threading.Barrier(N)
    errs = []
    cpath = os.path.join(BOOK, "concurrent.json")

    def worker(i):
        try:
            start.wait(timeout=20)
            vn._write_json(cpath, {"i": i, "pad": "字" * 200})
        except Exception as e:                              # noqa: BLE001
            errs.append("%s: %s" % (type(e).__name__, e))

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(N)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    chk("D1 并写不抛（固定名这一栏会扑空）", errs == [], errs[:3])
    try:
        doc = json.load(open(cpath, encoding="utf-8"))
        parsed = isinstance(doc, dict) and 0 <= doc.get("i", -1) < N
    except Exception as e:                                  # noqa: BLE001
        parsed = False
    chk("D2 最后落地的账本一定解得动", parsed is True)
    chk("D3 并写之后不留 .tmp 残件", tmp_left(BOOK) == [], tmp_left(BOOK))

    # ── E 解析缓存照旧被踢掉（写完再读不许拿到上一版） ────────────────
    cachep = os.path.join(BOOK, "cached.json")
    vn._write_json(cachep, {"v": 1})
    first = vn._read_json_cached(cachep)
    vn._write_json(cachep, {"v": 2})
    second = vn._read_json_cached(cachep)
    chk("E1 _write_json 之后解析缓存不会回旧字",
        first == {"v": 1} and second == {"v": 2}, (first, second))
finally:
    shutil.rmtree(ROOT, ignore_errors=True)

print("\n视频转写落盘体检：%d 项通过，失败 %d 项" % (PASSED[0], len(FAIL)))
for f in FAIL:
    print("  ✗ " + f)
sys.exit(1 if FAIL else 0)
