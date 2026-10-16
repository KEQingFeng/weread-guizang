# -*- coding: utf-8 -*-
"""阅读计划的「章字数」越界门禁：一长串数字不许把页数折算打抛。

为什么单开一条
    `readplan` 是纯逻辑：进出都是 dict，所以它自己的几套门禁（check_plan /
    check_plan_bigint / check_plan_input / check_plan_frac）都是直接喂合成的
    「各章带 chars」目录 —— 这是这个模块被约定的输入域。`at` / `frac` / 账本里的
    `pages` / `high` / `days` 早都过了 `_int` / `_clamp01` 那把尺子，唯独**章的
    chars** 是裸 `int((c or {}).get("chars"))` + 一句宽 except：

        chars = 10 ** 400                 # 合法 JSON，读回来是 Python 大整数
        int(math.ceil(chars / float(1000)))   # -> OverflowError（不是 ValueError）

    `total_pages` 与 `pages_at` 两处都这么算，于是同一份目录在 `plan_view`（读）
    与 `plan_do`（定 / 记进度）两条路上都可能当场抛。`OverflowError` 不在
    `except Exception` 之外——它就在里面——所以这不是"旧代码没兜住"的漏，而是
    "兜住了 int()、没兜住下一步的 float()"。

收口
    新增 `_chars_of(c)`：把一章的 chars 收成 0..`MAX_CHARS` 的整数（`MAX_CHARS`
    = `PAGE_MAX` × `PAGE_CHARS`），`total_pages` / `pages_at` 都走它；两处返回值
    再按 `PAGE_MAX` 封顶。原先散在六处的 `10000000` 也收进 `PAGE_MAX` 一处。
    另把推算终点那两句（`suggest_target` / `_fill_pace`）接上 `_add_days_str`：
    一千万页按每天一页推是一千万天，`date + timedelta` 超出 9999 年照样抛，
    算不出来就回空串（与 started / target 认不出同一格）。

这一条钉五件事，全部离线、不联网、不开浏览器、不起服务：

  A  尺子本身：`_chars_of` 对超大整数 / 超长数字串 / inf / NaN / 怪形状都给得出
     0..MAX_CHARS，正常字数一个字不改；
  B  对照：裸 `chars / float(PAGE_CHARS)` 对 10**400 确实抛 OverflowError，
     证明这一条不是空转；
  C  `total_pages`：正常折算照旧，脏 chars 不抛且落在 1..PAGE_MAX；
  D  `pages_at`：正常读数照旧，脏 chars 不抛、不超过 PAGE_MAX、不超过总页数；
  E  账本那条路：`put_plan` / `record_pos` / `view` / `suggest_target` 收到超大
     total 都不抛；只报了一半的进度不会被记成"读完了"。

归静态段，`--fast` 也得带上。
"""
import math
import pathlib
import re
import sys

HERE = pathlib.Path(__file__).resolve().parent
REPO = HERE.parent
sys.path.insert(0, str(REPO))

import readplan as rp  # noqa: E402

FAIL = []
PASSED = 0
SRC = (REPO / "readplan.py").read_text(encoding="utf-8")


def chk(name, cond, extra=""):
    global PASSED
    if cond:
        PASSED += 1
    else:
        FAIL.append(name)
    print(("PASS  " if cond else "FAIL  ") + name + ("" if cond else "   " + repr(extra)[:200]))


def no_raise(fn, default=None):
    """调一下被测函数；抛了就把异常写成可读的一串 —— 门禁要的是"它抛了"。"""
    try:
        return fn()
    except Exception as e:                                    # noqa: BLE001
        return "RAISED %s: %s" % (type(e).__name__, e)


HUGE = 10 ** 400
HUGE_STR = "1" * 400
TOO_LONG_STR = "1" * 5000
DIRTY = [HUGE, HUGE_STR, TOO_LONG_STR, float("inf"), float("-inf"), float("nan"),
         "1e400", "abc", None, {}, [], -5, 1e308]
DAY = "2026-10-16"
NOW = 1700000000            # 2023-11-14 前后，与别的计划套件同一颗固定时钟

# ── A 尺子本身 ───────────────────────────────────────────────────
chk("PAGE_MAX 就是 10000000（账本各处 pages / high 的上限）", rp.PAGE_MAX == 10000000, rp.PAGE_MAX)
chk("MAX_CHARS = PAGE_MAX × PAGE_CHARS", rp.MAX_CHARS == rp.PAGE_MAX * rp.PAGE_CHARS,
    getattr(rp, "MAX_CHARS", None))
chk("_chars_of 收超大整数 / 超长数字串到 MAX_CHARS",
    rp._chars_of({"chars": HUGE}) == rp.MAX_CHARS
    and rp._chars_of({"chars": HUGE_STR}) == rp.MAX_CHARS
    and rp._chars_of({"chars": TOO_LONG_STR}) == rp.MAX_CHARS,
    [rp._chars_of({"chars": v}) for v in (HUGE, HUGE_STR, TOO_LONG_STR)])
chk("_chars_of 对 inf / NaN / 负数 / 怪形状给 0",
    all(rp._chars_of({"chars": v}) == 0
        for v in (float("inf"), float("-inf"), float("nan"), "abc", -5, None)),
    [rp._chars_of({"chars": v}) for v in (float("inf"), float("nan"), -5, None)])
chk("_chars_of 对不是对象的一章也给 0（不 AttributeError）",
    rp._chars_of("abc") == 0 and rp._chars_of(None) == 0 and rp._chars_of([]) == 0)
chk("_chars_of 正常字数一个字不改",
    rp._chars_of({"chars": 3500}) == 3500 and rp._chars_of({"chars": "1200"}) == 1200)

# ── B 对照：旧写法确实抛 ──────────────────────────────────────────
old = no_raise(lambda: int(math.ceil(HUGE / float(rp.PAGE_CHARS))))
chk("对照：裸 `chars / float(PAGE_CHARS)` 对 10**400 确实抛 OverflowError",
    isinstance(old, str) and old.startswith("RAISED OverflowError"), old)
old_str = no_raise(lambda: int(math.ceil(int(HUGE_STR) / float(rp.PAGE_CHARS))))
chk("对照：400 位纯数字串同样在下一步折算抛 OverflowError",
    isinstance(old_str, str) and old_str.startswith("RAISED OverflowError"), old_str)

# ── C total_pages ───────────────────────────────────────────────
chk("正常：3500 字 → 4 页（向上取整）", rp.total_pages([{"chars": 3500}]) == 4,
    rp.total_pages([{"chars": 3500}]))
chk("正常：两章 1200 + 1900 → 4 页", rp.total_pages([{"chars": 1200}, {"chars": 1900}]) == 4)
chk("正常：空 / 全认不出仍是 1 页", rp.total_pages([]) == 1 and rp.total_pages(None) == 1
    and rp.total_pages([{"chars": 0}]) == 1)

for v in DIRTY:
    got = no_raise(lambda v=v: rp.total_pages([{"chars": v}]))
    chk("total_pages 脏 chars 不抛且落在 1..PAGE_MAX（%s）" % str(v)[:20],
        isinstance(got, int) and 1 <= got <= rp.PAGE_MAX, got)

chk("total_pages 对超大字数额顶在 PAGE_MAX（不是 0，也不是天文数字）",
    rp.total_pages([{"chars": HUGE}]) == rp.PAGE_MAX, rp.total_pages([{"chars": HUGE}]))
chk("50 章各 10**400：求和本身也不再溢出",
    rp.total_pages([{"chars": HUGE}] * 50) == rp.PAGE_MAX,
    no_raise(lambda: rp.total_pages([{"chars": HUGE}] * 50)))

# ── D pages_at ──────────────────────────────────────────────────
CH = [{"chars": 3000}, {"chars": 1000}]         # 4 页，2 章
chk("正常：第 0 章读一半 → 2 页", rp.pages_at(CH, 0, 0.5) == 2, rp.pages_at(CH, 0, 0.5))
chk("正常：末章读到头就是总页数", rp.pages_at(CH, 1, 1.0) == rp.total_pages(CH) == 4,
    (rp.pages_at(CH, 1, 1.0), rp.total_pages(CH)))
chk("正常：还没动是 0 页", rp.pages_at(CH, 0, 0.0) == 0)
chk("正常：章序号越界当读完全书", rp.pages_at(CH, 99, 0.0) == rp.total_pages(CH))
chk("正常：空目录 0 页", rp.pages_at([], 0, 0.5) == 0)

for v in DIRTY:
    got = no_raise(lambda v=v: rp.pages_at([{"chars": v}], 0, 0.5))
    chk("pages_at 脏 chars 不抛且落在 0..PAGE_MAX（%s）" % str(v)[:20],
        isinstance(got, int) and 0 <= got <= rp.PAGE_MAX, got)

big = [{"chars": HUGE}, {"chars": HUGE}]
got = no_raise(lambda: rp.pages_at(big, 1, 1.0))
chk("pages_at 对超大目录也顶在 PAGE_MAX 且不超过总页数",
    got == rp.PAGE_MAX and got <= rp.total_pages(big), (got, rp.total_pages(big)))

# ── E 账本那条真路 ───────────────────────────────────────────────
store = rp.load({})
p = no_raise(lambda: rp.put_plan(store, "b1", "超大", HUGE, "daily", 10, now=NOW))
chk("put_plan 收到超大 total 不抛", isinstance(p, dict), p)
if isinstance(p, dict):
    chk("put_plan 落下来的 pages 顶在 PAGE_MAX", p.get("pages") == rp.PAGE_MAX, p.get("pages"))
    chk("put_plan 算出来的 target 要么空、要么是个合法日期",
        p.get("target") == "" or rp._DAY_RE.match(p.get("target") or ""), p.get("target"))

got = no_raise(lambda: rp.record_pos(store, "b1", [{"chars": HUGE}], 0, 0.5, now=NOW))
chk("record_pos 收到超大目录不抛", not (isinstance(got, str) and got.startswith("RAISED")), got)
if isinstance(got, dict):
    chk("只报一半的进度不会被记成「读完了」", got.get("done_at") in (0, None), got.get("done_at"))
    chk("高水位不超过总页数", got.get("high", 0) <= got.get("pages", 0),
        (got.get("high"), got.get("pages")))

v = no_raise(lambda: rp.view(store, "b1", now=NOW, today=DAY))
chk("view 能把这本超大字数的计划读出来", isinstance(v, dict) and v.get("has") is True, v)
if isinstance(v, dict) and v.get("has"):
    chk("view 的 read ≤ pages、pct 落在 0..100",
        0 <= v.get("read", 0) <= v.get("pages", 0) <= rp.PAGE_MAX and 0 <= v.get("pct", 0) <= 100,
        {k: v.get(k) for k in ("pages", "read", "pct")})
    chk("view 的 ETA 要么空、要么是个合法日期",
        v.get("eta") == "" or rp._DAY_RE.match(v.get("eta") or ""), v.get("eta"))

chk("suggest_target 对超大 total 不抛（超出 9999 年就回空串）",
    no_raise(lambda: rp.suggest_target(DAY, HUGE, {"period": "daily", "pages": 1})) == "",
    no_raise(lambda: rp.suggest_target(DAY, HUGE, {"period": "daily", "pages": 1})))
chk("suggest_target 正常账一个字不改",
    rp.suggest_target(DAY, 300, {"period": "daily", "pages": 20}) == "2026-10-31",
    rp.suggest_target(DAY, 300, {"period": "daily", "pages": 20}))

# ── F 上限只有一个出处 ──────────────────────────────────────────
chk("源码里 10000000 只出现一次（就是 PAGE_MAX 的定义）",
    len(re.findall(r"\b10000000\b", SRC)) == 1, re.findall(r"\b10000000\b", SRC))
chk("total_pages / pages_at 都走 _chars_of 那一把尺子",
    SRC.count("_chars_of(") >= 3, SRC.count("_chars_of("))

bad = FAIL
print("\n阅读计划章字数越界门禁：%d 项通过，失败 %d 项" % (PASSED, len(bad)))
for n in bad:
    print("  ✗ " + n)
sys.exit(1 if bad else 0)
