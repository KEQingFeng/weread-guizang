#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""归藏门禁：两本时长账里的**越界数字**不许把热力图那一屏打崩（activity.py）。

一句话：`json` 认 `1e400`（解析成 inf），也认一长串数字（解析成一个大整数）；两者都是
**合法 JSON**，会原样落进 `cache/activity.json`（用户手工编辑、云同步落半截、旧版本写过
别的形状）。`load()` 里 `int(sec)` 只兜 `except Exception`，inf 那类会被丢掉，但
`10**400` 这种大整数 `int()` 收得下，于是它一路活到色阶与汇总：

  · `levels()` 的 `int(hi * 0.25)` —— 大整数 × float 当场抛 OverflowError
    （int too large to convert to float）；
  · `summary()` 的 `int(week / 7)` —— 大整数 ÷ int 同样抛。

`ui_server.activity_summary()` 就是 `activity.summary(activity.load(...))`，这一抛就是
`GET /api/activity` 连接被甩，个人主界面上两张热力图整块出不来。

这一条把「大整数照认（不改口径）＋ 每一条读路径都不抛 ＋ 正常尺度的账一个字不改 ＋
兜底回包仍是合法 JSON（没有 Infinity 字面量）」一起钉住，另附一条「旧浮点写法确实会抛」
的对照防空转。全程在临时沙盒里跑（先把 GUIZANG_DATA 指过去再 import ui_server），
不起服务、不联网、不开浏览器，不碰真实 cache/，故归 --fast 静态段。
"""
import datetime as _dt
import json
import os
import pathlib
import shutil
import sys
import tempfile
import time

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

SB = pathlib.Path(tempfile.mkdtemp(prefix="gz-activity-overflow-"))
os.environ["GUIZANG_DATA"] = str(SB)
os.environ["GUIZANG_BOOKS"] = str(SB / "books")

import activity as A  # noqa: E402
import ui_server as us  # noqa: E402

FAIL = []
PASSED = 0


def chk(name, cond, extra=""):
    global PASSED
    if cond:
        PASSED += 1
    else:
        FAIL.append(name)
    print(("PASS  " if cond else "FAIL  ") + name + ("" if cond else "   " + repr(extra)))


RAISED = object()


def val(fn):
    """跑一下；抛了就拿 RAISED 当结果 —— 门禁要逮住任何异常，而不是自己先 traceback。"""
    try:
        return fn()
    except Exception as e:  # noqa: BLE001
        print("      " + type(e).__name__ + ": " + str(e)[:120])
        return RAISED


BIG = 10 ** 400
BIG_STR = str(BIG)
INF = float("inf")
# 这一条有四处走的是产品自己的读口（`ui_server.activity_summary` / `activity_brief`
# 里那句 `time.strftime("%Y-%m-%d")`），它们认的是**真今天**：写死一个日期，第二天
# 零点一过 P2 / P3 就自己红了（2026-10-16 实测栽过一次）。所以这里一律按「今天往前
# 数几天」来摆账，哪天跑都是同一套。
DAY = time.strftime("%Y-%m-%d")


def _ago(n):
    """今天往前数 n 天，用的就是 activity 自己那把日期尺（键格式不许猜）。"""
    return A._ymd(_dt.date.today() - _dt.timedelta(days=n))


D1, D2, D3, D4 = (_ago(n) for n in (1, 2, 3, 4))

try:
    # ── 0. 前提：这些越界值真的进得来，旧写法真的会抛 ───────────────
    chk("J1 1e400 是合法 JSON，解析成 inf",
        json.loads("1e400") == INF)
    chk("J2 一长串数字是合法 JSON，解析成大整数（int() 收得下）",
        isinstance(json.loads(BIG_STR), int) and json.loads(BIG_STR) == BIG)
    chk("J3 对照：大整数 × 0.25 确实抛 OverflowError（色阶那一处的老写法）",
        val(lambda: int(BIG * 0.25)) is RAISED)
    chk("J4 对照：大整数 ÷ 7 也抛 OverflowError（日均那一处的老写法）",
        val(lambda: int(BIG / 7)) is RAISED)

    # ── 1. load：大整数照认（不改口径），越界非整数照旧丢掉 ─────────
    dirty = {
        "study": {"days": {DAY: BIG, D1: BIG_STR, D2: INF,
                           D3: 1e400, D4: -BIG,
                           "2026-13-40": BIG, "abc": BIG},
                  "total": BIG},
        "write": {"days": {DAY: 60}, "total": 60},
    }
    d = A.load(dirty)
    chk("L1 大整数原样留着（它是合法数字，不许被收口改口径）",
        d["study"]["days"].get(DAY) == BIG, d["study"]["days"].get(DAY))
    chk("L2 数字串认成同一个大整数",
        d["study"]["days"].get(D1) == BIG)
    chk("L3 +inf / 1e400 解析出的 inf / -大整数 一律丢掉（不是一天的量）",
        all(k not in d["study"]["days"] for k in
            (D2, D3, D4)))
    chk("L4 坏日期 / 非日期键带着大整数也进不来",
        "2026-13-40" not in d["study"]["days"] and "abc" not in d["study"]["days"])
    chk("L5 大整数 total 原样留着，另一本账一字未动",
        d["study"]["total"] == BIG and d["write"] == {"days": {DAY: 60}, "total": 60})

    # ── 2. 每一条读路径：喂大整数都不许抛 ───────────────────────────
    chk("R1 day_total 不抛",
        val(lambda: A.day_total(d, "study", DAY)) == BIG)
    chk("R2 range_total 不抛（近 7 天里含那一格，共两格）",
        val(lambda: A.range_total(d, "study", 7, today=DAY)) == 2 * BIG)
    chk("R3 calendar 不抛且那一格是真值",
        val(lambda: dict(A.calendar(d, "study", weeks=4, today=DAY)).get(DAY)) == BIG)
    chk("R4 streak 不抛（今天与昨天两格都在）",
        val(lambda: A.streak(d, "study", today=DAY)) == 2)
    chk("R5 peak 不抛且取到那个峰值",
        val(lambda: A.peak(d, "study", today=DAY)) == BIG)
    chk("R6 levels 不抛（老写法就是在这抛的）",
        val(lambda: A.levels(d, "study", today=DAY)) is not RAISED)
    chk("R7 summary 不抛（ui_server.activity_summary 走的就是这一句）",
        val(lambda: A.summary(d, today=DAY)) is not RAISED)

    # ── 3. 色阶口径：越界不打抛，正常尺度与老写法同值 ───────────────
    cuts = A.levels(d, "study", today=DAY)
    chk("C1 越界峰值下三档仍是升序正整数",
        isinstance(cuts, list) and len(cuts) == 3
        and all(isinstance(x, int) and x > 0 for x in cuts)
        and cuts[0] < cuts[1] < cuts[2], cuts)
    chk("C2 三档就是峰值的 1/4、1/2、3/4（大整数也算得准）",
        cuts == [max(1, BIG // 4), BIG // 2, max(1, BIG * 3 // 4)], cuts)
    chk("C3 第一天（峰值很小）仍兜底成 15 / 30 / 60 分钟",
        A.levels(A.load({}), "study") == [900, 1800, 3600])
    d0 = A.load({})
    d0["study"]["days"] = {DAY: 8000}
    chk("C4 峰值的 1/4、1/2、3/4 照旧",
        A.levels(d0, "study", today=DAY) == [2000, 4000, 6000], A.levels(d0, "study", today=DAY))
    # 与老浮点写法逐值对拍：这些尺度上的取整结果必须一模一样，口径没被偷偷改掉。
    mism = [hi for hi in (900, 901, 1000, 1001, 8000, 43200, 99999, 10 ** 15)
            if [max(1, int(hi * 0.25)), int(hi * 0.5), int(hi * 0.75)]
            != A.levels({"study": {"days": {DAY: hi}}}, "study", today=DAY)]
    chk("C5 正常尺度上与 int(hi*0.25) 老写法逐值同值（口径未改）", not mism, mism)

    # ── 4. 汇总包：数算得出、仍是合法 JSON、好账不改 ────────────────
    sm = val(lambda: A.summary(d, today=DAY))
    chk("S1 汇总给得出两本账，格子仍是 [日期, 秒数, 档位]",
        sm is not RAISED and sm["study"]["today"] == BIG
        and any(len(c) == 3 and c[0] == DAY and c[1] == BIG for c in sm["study"]["cells"]), sm)
    chk("S2 最大值 / 总计都是那个大整数，没被截成 0",
        sm["study"]["max"] == BIG and sm["study"]["total"] == BIG)
    chk("S3 日均走整数除法，不抛也不改写认得出的值",
        sm["study"]["avg_day"] == A.range_total(d, "study", 7, today=DAY) // 7,
        sm["study"]["avg_day"])
    dumped = val(lambda: json.dumps(sm, ensure_ascii=False, allow_nan=False))
    chk("S4 兜底回包是严格合法 JSON（没有 NaN / Infinity 字面量）",
        dumped is not RAISED and "Infinity" not in dumped and "NaN" not in dumped)
    chk("S5 严格 JSON 解析得回来，那格还是同一个数",
        dumped is not RAISED and json.loads(dumped)["study"]["max"] == BIG)

    clean = A.load({"study": {"days": {D2: 600, D1: 300, DAY: 1200},
                              "total": 2100},
                    "write": {"days": {DAY: 900}, "total": 900}})
    csm = A.summary(clean, today=DAY)
    chk("S6 干净账一个字不改：今天 / 周 / 年 / 总计 / 峰值 / 色阶 / 连续",
        csm["study"]["today"] == 1200
        and csm["study"]["total"] == 2100 and csm["study"]["max"] == 1200
        and csm["study"]["week"] == 2100 and csm["study"]["year"] == 2100
        and csm["study"]["levels"] == [300, 600, 900]
        and csm["study"]["streak"] == 3 and csm["study"]["avg_day"] == 300, csm["study"])
    chk("S7 干净账再过一遍 load 仍是同一份（幂等，不因收口漂移）",
        A.load(clean) == clean)

    # ── 5. 真产品路径：两本账的读口都不许被越界数字甩掉 ──────────────
    os.makedirs(os.path.dirname(us.ACTIVITY_PATH), exist_ok=True)
    with open(us.ACTIVITY_PATH, "w", encoding="utf-8") as f:
        f.write('{"study":{"days":{"' + DAY + '":' + BIG_STR + '},"total":' + BIG_STR + '},'
                '"write":{"days":{},"total":0}}')
    summ = val(lambda: us.activity_summary())
    chk("P1 GET /api/activity 的整包（ui_server.activity_summary）不抛",
        summ is not RAISED and set(summ) >= {"study", "write", "today", "cap"}, summ)
    chk("P2 那一格与总计照旧是大整数，没被悄悄抹平",
        summ is not RAISED and summ["study"]["today"] == BIG
        and summ["study"]["total"] == BIG)
    brief = val(lambda: us.activity_brief())
    chk("P3 /api/state 那一小撮（activity_brief）也不抛、今天照读得出",
        brief is not RAISED and brief["study"]["today"] == BIG
        and brief["study"]["total"] == BIG, brief)
    # 坏字符 / 半截 JSON 的老口径别被这次改动带坏：整份读不成时当空账。
    with open(us.ACTIVITY_PATH, "w", encoding="utf-8") as f:
        f.write('{"study": {"days": {"' + DAY + '": ')
    chk("P4 半截 JSON 仍当空账（不是这次要改的东西，别被带坏）",
        val(lambda: us.activity_summary()) is not RAISED)
finally:
    shutil.rmtree(SB, ignore_errors=True)

print("\n时长账越界数字门禁：%d 项通过，失败 %d 项" % (PASSED, len(FAIL)))
for f in FAIL:
    print("  ✗ " + f)
sys.exit(1 if FAIL else 0)
