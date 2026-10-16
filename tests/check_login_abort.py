#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""归藏门禁：登录态账本收残件那一句要兜住中止信号（`login.write_state`）。

为什么要单开一条
    `login.write_state` 是全仓「先写同目录 `.tmp`、再 `os.replace`」这一族里
    **唯一**一处临时件名还固定成 `path + ".tmp"` 的写口（其余各格第七～十七轮
    逐格换成 pid + 随机串）。名字那一半本轮**没有动**（`tests/check_login.py`
    把这条名字形状钉成了源码断言，改名会动到既有门禁），这一条收的是另一半：

        tmp = path + ".tmp"
        try:
            # …写…
            os.replace(tmp, path)
        except Exception:          # ← KeyboardInterrupt / SystemExit 不继承它
            os.remove(tmp)

    `login.py` 自带 `__main__`（终端里手动跑一趟、或应用之外再跑一趟），
    用户按 Ctrl-C 中止时，若中断恰好落在这两句之间，旧的 `except Exception`
    接不住 —— `cache/` 里留下一个没人认领的半截 `login_state.json.tmp`。
    与 `flomo_notes` / `mindmap` / `writer` / `ai_sum` 那几格同一条口径：
    收残件要兜 `BaseException`，收完再把中止信号原样抛出去。

这一条钉住
  · 正常写：账本完整落盘、`read_state` 读得回来、不留 `.tmp`；
  · 中止信号（`KeyboardInterrupt` / `SystemExit`）照旧抛出去，且退出路径上
    的临时件收干净、旧档逐字节不动；
  · 写崩（`json.dump` 抛 `OSError`）同样收干净再抛；
  · 对照：旧的 `except Exception` 写法在 `KeyboardInterrupt` 下确实会把
    `.tmp` 留在盘上 —— 门禁不空转；
  · 与「清除本地数据」的接口：固定名残件仍被 `cleanup._residue_re` 认得出
    （`cache/login_state.json` 本就在那张白名单里），换实现时别把它的射程弄丢；
  · 源码：`write_state` 的收残件分支是 `except BaseException` + `_discard`，
    且**仍然**是固定名那一套（把已知的那条限制显式钉住，改名字时要连
    `check_login.py` 一起改）。

全程系统临时目录里的沙盒，不联网、不开浏览器、不碰用户真实 `cache/`，故归
`--fast` 静态段。
"""
import atexit
import io
import json
import os
import shutil
import sys
import tempfile
import unittest.mock as mock
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

SB = tempfile.mkdtemp(prefix="gz-login-abort-")
atexit.register(lambda: shutil.rmtree(SB, ignore_errors=True))
os.environ["GUIZANG_DATA"] = os.path.join(SB, "cache")
os.environ["GUIZANG_BOOKS"] = os.path.join(SB, "books")
os.makedirs(os.environ["GUIZANG_DATA"], exist_ok=True)
os.makedirs(os.environ["GUIZANG_BOOKS"], exist_ok=True)

import login as L  # noqa: E402  —— 顶层不带 playwright，importable
import cleanup as C  # noqa: E402  —— 只为 `_residue_re` 那把尺子

FAIL = []
N = [0]


def chk(name, cond, extra=""):
    N[0] += 1
    print(("  ok  " if cond else " FAIL ") + name + ("" if cond else "  <- " + repr(extra)))
    if not cond:
        FAIL.append(name)


WORK = os.path.join(SB, "work")
os.makedirs(WORK, exist_ok=True)
P = os.path.join(WORK, "login_state.json")
TMP = P + ".tmp"                       # 固定名，本轮有意保留


def tmp_left(d):
    return sorted(n for _r, _ds, fs in os.walk(d) for n in fs if n.endswith(".tmp"))


# ── A. 正常写 ────────────────────────────────────────────────────────
L.write_state(True, "https://weread.qq.com/web/shelf", "扫码登录成功", path=P)
doc = json.loads(io.open(P, encoding="utf-8").read())
chk("A1 正常写：账本内容完整落盘",
    doc.get("logged_in") is True and doc.get("url") == "https://weread.qq.com/web/shelf"
    and doc.get("note") == "扫码登录成功", doc)
chk("A2 正常写：目录里不留 .tmp", tmp_left(WORK) == [], tmp_left(WORK))
back = L.read_state(P)
chk("A3 写出来的账本 read_state 读得回来（写读同一把尺子）",
    back is not None and back.get("logged_in") is True
    and isinstance(back.get("checked_at"), float) and back["checked_at"] > 0, back)

BEFORE = io.open(P, "rb").read()

# ── B. 中止信号：抛出去、旧档不动、临时件收干净 ─────────────────────
# 中断落在「写完临时件、还没改名」这一句上：装一只 json.dump 的间谍抛中止信号。
for name, exc in (("KeyboardInterrupt", KeyboardInterrupt), ("SystemExit", SystemExit)):
    with mock.patch.object(L.json, "dump", side_effect=exc):
        raised = False
        try:
            L.write_state(False, "https://weread.qq.com/web/shelf", "扫码超时", path=P)
        except BaseException as e:                       # noqa: BLE001
            raised = isinstance(e, exc)
    chk("B %s 照旧抛出去（没被吞成返回值）" % name, raised)
    chk("B %s 退出路径上的 .tmp 收干净" % name, not os.path.exists(TMP), tmp_left(WORK))
    chk("B %s 中止时旧档逐字节不动" % name, io.open(P, "rb").read() == BEFORE)

# ── C. 写崩（OSError）：同样收干净再抛 ──────────────────────────────
with mock.patch.object(L.json, "dump", side_effect=OSError(28, "No space left on device")):
    try:
        L.write_state(False, "u", "n", path=P)
        raised = False
    except OSError:
        raised = True
chk("C1 写崩照旧抛出去", raised)
chk("C2 写崩不留 .tmp", not os.path.exists(TMP) and tmp_left(WORK) == [], tmp_left(WORK))
chk("C3 写崩旧档逐字节不动", io.open(P, "rb").read() == BEFORE)

# ── D. 对照：旧的 `except Exception` 写法确实会把 .tmp 留下来 ───────
CTL = os.path.join(WORK, "old-writer")
os.makedirs(CTL, exist_ok=True)
OP = os.path.join(CTL, "login_state.json")
OT = OP + ".tmp"


def old_write(payload, path, opsys=os):
    """复刻收口前那一版：兜 Exception，且不认中止信号。"""
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
        opsys.replace(tmp, path)
    except Exception:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


with mock.patch.object(os, "replace", side_effect=KeyboardInterrupt):
    try:
        old_write({"logged_in": False}, OP)
        old_raised = False
    except BaseException:
        old_raised = True
chk("对照：旧写法在 KeyboardInterrupt 下照旧抛（Exception 那半段与本次同一形状）",
    old_raised)
chk("对照：旧写法的 .tmp 确实留在盘上（这一条不是空转）",
    os.path.exists(OT), sorted(os.listdir(CTL)))

# ── E. 与「清除本地数据」的接口 ─────────────────────────────────────
residue = os.path.join(os.environ["GUIZANG_DATA"], "login_state.json.tmp")
chk("E1 cleanup 仍认得出这份账本的固定名残件",
    C._residue_re("login_state.json").match(os.path.basename(residue)) is not None, residue)
chk("E2 cleanup 的白名单里确实有 login_state.json（残件按账本名派生）",
    any(n == "login_state.json" for n, _ in C.CACHE_FILES))

# ── F. 源码：收残件分支兜 BaseException；固定名这一条限制显式钉住 ──
SRC = io.open(os.path.join(REPO, "login.py"), encoding="utf-8").read()
seg = SRC[SRC.find("def write_state("):SRC.find("async def is_logged_in")]
chk("F1 write_state 的收残件分支兜 BaseException + _discard",
    "except BaseException" in seg and "_discard(tmp)" in seg, seg[:200])
chk("F2 `_discard` 只有一个定义（一把尺子）",
    SRC.count("def _discard(") == 1, SRC.count("def _discard("))
# 这一条是**已知限制**的显式记录，不是遗漏：改临时件名必须同时改 check_login.py。
chk("F3 临时件名仍是固定名那一套（check_login.py 的源码断言钉着，已知限制）",
    'tmp = path + ".tmp"' in SRC and "os.replace(tmp, path)" in SRC)

print()
print("登录态账本中止信号收口门禁：%d 项通过，失败 %d 项" % (N[0] - len(FAIL), len(FAIL)))
if FAIL:
    print("未过：" + " / ".join(FAIL))
sys.exit(1 if FAIL else 0)
