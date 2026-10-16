# -*- coding: utf-8 -*-
"""视频转写那两个落盘口（`video_note._write_json` / `_write_text`）：中止信号也不许留残件。

为什么单开一条门禁
    上一条 `check_video_atomic.py` 收的是「两个写手抢同一个临时件」与「写失败时把临时件
    收干净」。那两半兜的都是 `except Exception` —— 异常这条退出路径盖住了，**中止信号
    这条没盖**：`KeyboardInterrupt` / `SystemExit` 不继承 `Exception`，Ctrl-C、关窗口、
    活动监视器里强杀，恰好都在写 / 改名那一步上穿出去，书目录里就多一个随机名 `.tmp`
    （`transcript.json.<pid>.<hex>.tmp`）。正常路径本该自己收掉，`ui_server` 那条按 `.tmp`
    尾巴兜底的清残稿是**强杀之后**的补救，不是正常路径的替代。

    同一把尺子已经在别处收过：`book_notes._write_json`（`#157`）、`writer._write_text_atomic`
    与 `flomo_notes._write_text_atomic`（`#164`）都兜 `BaseException`、失败时先 `_discard(tmp)`
    再抛。视频这两个口是那一族里最后两个只兜 `Exception` 的写口（这一族至此只剩
    `mindmap._write_json` 一处，导图那格的取舍另记在《交接说明》§2），这一条把它们对齐。

    顺带收一个窄口子：老写法把 `os.replace(tmp, path)` 放在 `try` **之外**，改名那一步
    自己抛（磁盘满 / 只读 / 杀毒钳住目标，或恰好在改名前后撞上中止信号）时临时件没人收。

这一条钉四面
  A. 中止信号落在**序列化 / 写正文**那一步：照旧往上传（不吞 KeyboardInterrupt）、
     随机名临时件收干净、旧档逐字节不动；
  B. 中止信号落在 **os.replace** 那一步：照旧上传、临时件收干净、旧档仍在原位；
  C. 源码接线：两个写口都兜 `BaseException`，且不再写死 `path + ".tmp"`；
  D. 对照：把老写法（只兜 `Exception`）原样跑一遍，同一个 `KeyboardInterrupt` 下
     临时件**确实留了下来** —— 证明这道收口不是空转。

跑法：纯函数 + 临时目录沙盒，不联网、不起服务、不开浏览器，归 --fast。
"""
import contextlib
import json
import os
import pathlib
import shutil
import sys
import tempfile

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


@contextlib.contextmanager
def patched(obj, name, value):
    """临时换掉 obj.name，退出时无论怎样都还原。"""
    old = getattr(obj, name)
    setattr(obj, name, value)
    try:
        yield old
    finally:
        setattr(obj, name, old)


def raises_base(fn, *a, **k):
    """跑 fn，回它抛出的异常类名（含 BaseException）；没抛回 None。"""
    try:
        fn(*a, **k)
    except BaseException as e:          # noqa: BLE001 —— 这条体检就是要看它吐什么
        return type(e).__name__
    return None


def boom(*a, **k):
    raise KeyboardInterrupt("模拟 Ctrl-C")


def old_write_json(path, obj):
    """`_write_json` 的老写法：只兜 Exception，os.replace 在 try 之外。"""
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(vn._drop_surrogates(obj), f, ensure_ascii=False, indent=2)
    except Exception:                   # noqa: BLE001 —— 老写法就是这么兜的
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise
    os.replace(tmp, path)


def old_write_text(path, text):
    """`_write_text` 的老写法：只兜 Exception，os.replace 在 try 之外。"""
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(vn._drop_surrogates(text))
    except Exception:                   # noqa: BLE001
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise
    os.replace(tmp, path)


ROOT = tempfile.mkdtemp(prefix="guizang-video-catch-")
try:
    BOOK = os.path.join(ROOT, "video_bilibili_demo")
    os.makedirs(BOOK, exist_ok=True)
    JP = os.path.join(BOOK, "transcript.json")
    TP = os.path.join(BOOK, "transcript.txt")

    # ── 0 正常写：先铺一份旧档，后面每一步都对照它 ─────────────────────
    vn._write_json(JP, {"segments": [{"id": "old"}]})
    vn._write_text(TP, "旧正文\n")
    before_json = open(JP, "rb").read()
    before_txt = open(TP, "rb").read()
    chk("0 正常写落得下、不留 .tmp",
        json.load(open(JP, encoding="utf-8")) == {"segments": [{"id": "old"}]}
        and open(TP, encoding="utf-8").read() == "旧正文\n"
        and tmp_left(BOOK) == [], tmp_left(BOOK))

    # ── A 中止信号落在写 / 序列化那一步 ──────────────────────────────
    with patched(vn, "_drop_surrogates", boom):
        got = raises_base(vn._write_json, JP, {"segments": [{"id": "new"}]})
    chk("A1 json：KeyboardInterrupt 照旧往上传（不吞成 ok）", got == "KeyboardInterrupt", got)
    chk("A2 json：中止后不留随机名 .tmp", tmp_left(BOOK) == [], tmp_left(BOOK))
    chk("A3 json：中止后旧档逐字节不动", open(JP, "rb").read() == before_json)

    with patched(vn, "_drop_surrogates", boom):
        got = raises_base(vn._write_text, TP, "新正文\n")
    chk("A4 text：KeyboardInterrupt 照旧往上传", got == "KeyboardInterrupt", got)
    chk("A5 text：中止后不留随机名 .tmp", tmp_left(BOOK) == [], tmp_left(BOOK))
    chk("A6 text：中止后旧档逐字节不动", open(TP, "rb").read() == before_txt)

    # ── B 中止信号落在 os.replace 那一步（老写法把这一步放在 try 之外）──
    with patched(vn.os, "replace", boom):
        got = raises_base(vn._write_json, JP, {"segments": [{"id": "new"}]})
    chk("B1 json：改名那一步中止照旧往上传", got == "KeyboardInterrupt", got)
    chk("B2 json：改名中止后临时件收干净", tmp_left(BOOK) == [], tmp_left(BOOK))
    chk("B3 json：改名中止后旧档仍在原位", open(JP, "rb").read() == before_json)

    with patched(vn.os, "replace", boom):
        got = raises_base(vn._write_text, TP, "新正文\n")
    chk("B4 text：改名那一步中止照旧往上传", got == "KeyboardInterrupt", got)
    chk("B5 text：改名中止后临时件收干净", tmp_left(BOOK) == [], tmp_left(BOOK))
    chk("B6 text：改名中止后旧档仍在原位", open(TP, "rb").read() == before_txt)

    # ── C 源码接线：两个写口都兜 BaseException，且不再写死固定名 ───────
    src = (REPO / "video_note.py").read_text(encoding="utf-8")
    seg_json = src[src.index("def _write_json"):src.index("def _read_json")]
    seg_text = src[src.index("def _write_text"):src.index("def _save_notes")]
    chk("C1 json 口兜的是 BaseException（不是 Exception）",
        "except BaseException" in seg_json and "except Exception" not in seg_json)
    chk("C2 text 口兜的是 BaseException（不是 Exception）",
        "except BaseException" in seg_text and "except Exception" not in seg_text)
    chk("C3 两个口都不再写死 path + \".tmp\"",
        'path + ".tmp"' not in seg_json and 'path + ".tmp"' not in seg_text)

    # ── D 对照：老写法在同一个 KeyboardInterrupt 下确实留下临时件 ──────
    ctl_json = os.path.join(BOOK, "control_old.json")
    vn._write_json(ctl_json, {"v": "old"})
    with patched(vn, "_drop_surrogates", boom):
        got = raises_base(old_write_json, ctl_json, {"v": "new"})
    chk("D1 对照：老写法也抛 KeyboardInterrupt", got == "KeyboardInterrupt", got)
    chk("D2 对照：老写法把随机前的固定名 .tmp 留在了书目录里"
        "（老路径的洞就是这一条收的）",
        os.path.exists(ctl_json + ".tmp"), tmp_left(BOOK))
    if os.path.exists(ctl_json + ".tmp"):
        os.remove(ctl_json + ".tmp")

    ctl_txt = os.path.join(BOOK, "control_old.txt")
    vn._write_text(ctl_txt, "old")
    with patched(vn, "_drop_surrogates", boom):
        got = raises_base(old_write_text, ctl_txt, "new")
    chk("D3 对照：文本口老写法同样把 .tmp 留下了",
        got == "KeyboardInterrupt" and os.path.exists(ctl_txt + ".tmp"), tmp_left(BOOK))
    if os.path.exists(ctl_txt + ".tmp"):
        os.remove(ctl_txt + ".tmp")

    chk("D4 对照做完，沙盒里照旧没有 .tmp", tmp_left(BOOK) == [], tmp_left(BOOK))
finally:
    shutil.rmtree(ROOT, ignore_errors=True)

print("\n视频转写中止信号体检：%d 项通过，失败 %d 项" % (PASSED[0], len(FAIL)))
for f in FAIL:
    print("  ✗ " + f)
sys.exit(1 if FAIL else 0)
