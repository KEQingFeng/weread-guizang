# -*- coding: utf-8 -*-
"""阅读计划的「数字串口径」门禁：同一份读数的两种写法必须读成同一个数。

`cache/plan.json` 是手能改的文本文件，也可能被云同步写回一份旧版本落过的形状。
同一份读数可以写成数字，也可以写成带引号的字符串，两种都是合法 JSON：

    "high": 300        # JSON 数字
    "high": "300"      # JSON 字符串，`json.load` 收成一个字符串

`_int()` 里旧写法是裸 `int(v)`。普通写法它本来就两边都认（`int("300")` 收得下），
差在**超大数字串**那一格：数字写法被 hi 夹成大数，字符串写法却撞上 CPython 3.11+
给「十进制串转 int」设的 4300 位默认上限而 ValueError，旧的 `except Exception`
只有 `n = lo` 一条路 —— 同一句话的两种写法读成两个数（10000000 与 0），界面上
分辨不出来。这一条把「字符串那一支必须与它的数字孪生同值、且越线的按 hi 收口」
钉住。旧的 except 很宽，所以这不是崩溃，症状只是读数不一致。

同样要钉住的是**另一头**：收口只许认「纯数字串」，不许顺手把 `"3.5"` / `"1e5"`
这类写法也认下来。`pages_at()` 的章序号、`load()` 的每天页数、色阶读数共用这一把
尺子，而章序号出现小数本身就是「认不出」（半章不是一章）—— check_plan_input.py
正是拿 `"3.5"` 当坏输入钉着的。宽容只会把一个认不出的值折成一个看起来正常的数，
那比退回 lo 更难查。

这一条钉四件事，全部离线、不联网、不开浏览器、不起服务：

  1. 纯数字串与它的数字孪生同值（含超大数字串：走 Python 大整数解析，不丢精度）；
  2. 指数 / 小数写法（`"1e5"` / `"3.5"`）与坏形状一样退回 lo，口径不外扩；
  3. 越过 4300 位默认上限的超长纯数字串按 hi 收口（与数字支同终点，不落成 lo）；
  4. 走真账本那两条读口（`load` / `view`）：字符串大数字读回来认得对、全趟不抛。

归静态段，`--fast` 也得带上。
"""
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent
REPO = HERE.parent
sys.path.insert(0, str(REPO))

import readplan as rp  # noqa: E402

FAIL = []
PASSED = 0


def chk(name, cond, extra=""):
    global PASSED
    if cond:
        PASSED += 1
    else:
        FAIL.append(name)
    print(("PASS  " if cond else "FAIL  ") + name + ("" if cond else "   " + repr(extra)[:160]))


BIG = 10 ** 400
DAY = "2026-10-16"


# ── 0. 对照：旧的「只认数字」写法确实会在字符串上掉口径 ──────────────
def old_int(v, lo=0, hi=None):
    try:
        n = int(v)
    except Exception:
        n = lo
    n = max(lo, n)
    if hi is not None:
        n = min(hi, n)
    return n


chk("对照：JSON 数字 1e400 读成 inf，旧写法 int(inf) 抛 OverflowError 回 lo",
    old_int(float("inf"), 0, 10000000) == 0)
chk("对照：普通数字串 int() 收得下（所以旧写法在普通串上是对的）",
    old_int("300", 0, 10000000) == 300)
# 旧写法在「超大数字串」这一格上会与它的数字孪生分家，具体是哪一种分家取决于
# 解释器给「十进制串转 int」设的上限（CPython 3.11+ 默认 4300 位）：旧写法要么
# 掉回 lo、要么被 hi 夹住，与它数字孪生那一支的值分家。两种分家都算「同值的两种
# 写法读成两个数」，用它当对照，免得把这条门禁钉死在某台机器的
# sys.set_int_max_str_digits() 上。
Huge = 10 ** 5000
try:
    old_str_side = old_int(str(Huge), 0, 10000000)
except Exception as e:  # noqa: BLE001
    old_str_side = type(e).__name__
old_num_side = old_int(Huge, 0, 10000000)
chk("对照：旧写法在超大数字串上与数字写法分家（抛异常或读成不同的数）",
    old_str_side != old_num_side, (old_str_side, old_num_side))
chk("对照：数字孪生被 hi 夹成大数、字符串那一支落回 lo / 抛（同一句话两种读数）",
    old_num_side == 10000000 and old_str_side != 10000000,
    (old_num_side, old_str_side))

# ── 1. 数字孪生：同值的两种写法必须读成同一个数 ─────────────────────
chk("普通数字串：_int('300') 与 _int(300) 同值",
    rp._int("300") == rp._int(300) == 300)
chk("首尾空白照认（' 42 ' -> 42）", rp._int(" 42 ") == 42)
chk("超长数字串与它的数字孪生同值（都被 hi 夹住，不是一边夹一边归零）",
    rp._int(str(BIG), 0, 10000000) == rp._int(BIG, 0, 10000000) == 10000000,
    (rp._int(str(BIG), 0, 10000000), rp._int(BIG, 0, 10000000)))
chk("不给 hi 时超长数字串保留完整大整数（不是被 float 截掉精度）",
    rp._int(str(10 ** 20)) == 10 ** 20)
# Python 3.11+ 对「十进制串转 int」有默认上限（4300 位），越线会 ValueError；
# 这条只是断言越长的纯数字串照样按「大数」收口（被 hi 夹住），与数字支同终点。
chk("超长纯数字串（5000 位）按 hi 收口、不落成 lo",
    rp._int("1" + "0" * 5000, 0, 10000000) == 10000000,
    rp._int("1" + "0" * 5000, 0, 10000000))

# ── 2. 口径不许外扩：小数 / 指数写法仍算认不出 ─────────────────────
for label, text in (('"3.5"（半章不是一章）', "3.5"), ('"1e5"', "1e5"),
                    ('"1.5e3"', "1.5e3"), ('"1e400"', "1e400"),
                    ('"0x10"', "0x10"), ('空串', ""), ('"  "', "  ")):
    chk("_int(%s, 7, 10000000) 仍退回 lo=7" % label,
        rp._int(text, 7, 10000000) == 7, rp._int(text, 7, 10000000))

# ── 3. 坏形状不许被越收越宽 ────────────────────────────────────────
for label, v in (("None", None), ("对象", {"a": 1}), ("列表", [1]),
                 ("坏字符 abc", "abc"), ("nan 串", "nan"), ("inf 串", "inf"),
                 ("float inf", float("inf")), ("float nan", float("nan")),
                 ("0x10", "0x10")):
    chk("坏形状 %s 仍退回 lo（不被当成数）" % label,
        rp._int(v, 7, 10000000) == 7, rp._int(v, 7, 10000000))
chk("负数仍被 lo 夹回（口径未改）", rp._int("-5", 0, 100) == 0)
chk("真布尔仍认成 1（口径未改）", rp._int(True, 0, 100) == 1)

# ── 4. 真账本两条读口：认得对、不抛 ────────────────────────────────
dirty = {"version": 1, "plans": {"b1": {
    "title": "脏账", "pages": "300",
    "goal": {"period": "daily", "pages": "20"},
    "started": DAY, "target": DAY, "at": "2", "frac": 0.5,
    "high": str(BIG), "created_at": str(BIG), "last_at": 0, "done_at": 0,
    "days": {DAY: str(BIG)}, "history": [], "junk": str(BIG)}}}
try:
    st = rp.load(dirty)
    p = st["plans"]["b1"]
    chk("load：字符串大数字写成大整数再被 hi 夹住（不是 0）",
        p["high"] == 10000000, p.get("high"))
    chk("load：普通数字串照认（pages=300 / goal.pages=20 / at=2）",
        p["pages"] == 300 and p["goal"]["pages"] == 20 and p["at"] == 2)
    chk("load：days 那一格也认出量级（不是 0）", p["days"].get(DAY) == 100000000)
    v = rp.view(st, "b1", total=300, now=1750000000, today=DAY)
    chk("view：整趟不抛且读数带出来（read 由 high 现算，仍被 pages 夹住）",
        isinstance(v, dict) and v.get("has") is True and 0 <= v.get("read", -1) <= 300, v)
except Exception as e:  # noqa: BLE001
    chk("load / view 收到字符串大数字不许抛", False, "%s: %s" % (type(e).__name__, e))

# ── 5. record_pos：高水位是大数字时也不许被甩掉 ────────────────────
try:
    st2 = rp.load(dirty)
    out = rp.record_pos(st2, "b1", [{"chars": 300000}], 0, 0.0,
                        now=1750000000)
    chk("record_pos：脏高水位下照跑、不抛", isinstance(out, dict), out)
except Exception as e:  # noqa: BLE001
    chk("record_pos：脏高水位下照跑、不抛", False, "%s: %s" % (type(e).__name__, e))

print("\n阅读计划大数字口径：%d 项通过，失败 %d 项" % (PASSED, len(FAIL)))
for f in FAIL:
    print("  ✗ " + f)
sys.exit(1 if FAIL else 0)
