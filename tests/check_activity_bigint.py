# -*- coding: utf-8 -*-
"""activity 的越界整数：账本里落回一长串数字，两张热力图不许整屏白掉。

背景（改之前实测）
    .venv/bin/python -c "import activity; d=activity.load({'study':{'days':{'2026-03-03':10**400},'total':10**400}}); activity.summary(d, today='2026-03-05')"
      -> OverflowError: int too large to convert to float

两处都在拿「大整数 / 小整数」：
  · levels() 用 `int(hi * 0.25)` 算色阶分档 —— hi 先被转成 float；
  · summary() 的 avg_day 用 `week / 7` —— week 也先被转成 float。

`10**400` 是合法 JSON 整数（json.load 按 Python 大整数原样读回），转 float 当场抛。
activity.load 不设上界是刻意的：它只洗形状（负数、坏日期丢掉），数字多大都收，
这一层不能假设用户账本有个「金额感」的上限。它也不是同一种病第一次犯 —— 隔壁
web_parse 的推文编号（`int(tid) / 1e15`）犯过一模一样的错，见 check_web_parse_bigid.py。

这一条钉三件事，全程离线（纯函数，不起服务、不碰 HTTP，归 --fast）：
  1. 越界整数走 levels() / summary() 不抛，且算出来的是精确整数（不是 inf / 0）；
  2. 正常量级一个字不改：levels() 仍等于按峰值 1/4、1/2、3/4 分档；
  3. 对照证明旧的浮点算式确实会抛 OverflowError，免得门禁空转。

跑法：.venv/bin/python tests/check_activity_bigint.py
"""
import pathlib
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
import activity  # noqa: E402

FAIL = []
PASSED = 0


def chk(name, cond, extra=""):
    global PASSED
    if cond:
        PASSED += 1
    else:
        FAIL.append(name)
    print(("PASS  " if cond else "FAIL  ") + name + ("" if cond else "   " + repr(extra)[:200]))


BIG = 10 ** 400          # 合法 JSON 整数：json 能原样读回，转 float 会 OverflowError
TODAY = "2026-03-05"


def big_book():
    return activity.load({"study": {"days": {"2026-03-03": BIG}, "total": BIG}})


# ── 0. 对照：旧算式确实是会抛的那一个（门禁不许空转）────────────
def old_levels(hi):
    return [max(1, int(hi * 0.25)), int(hi * 0.5), int(hi * 0.75)]


def old_avg(week):
    return int(week / 7)


def raised(fn):
    try:
        fn()
    except OverflowError:
        return True
    except Exception:
        return False
    return False


chk("对照：旧的 levels 算式对着 10**400 确实抛 OverflowError", raised(lambda: old_levels(BIG)))
chk("对照：旧的 avg_day 算式对着 10**400 确实抛 OverflowError", raised(lambda: old_avg(BIG)))

# ── 1. load 收下大整数，不偷偷夹掉 ──────────────────────────────
d = big_book()
chk("账本：load 原样收下 10**400（不夹上界，只洗形状）",
    d["study"]["days"]["2026-03-03"] == BIG and d["study"]["total"] == BIG)
chk("账本：peak 原样取回 10**400", activity.peak(d, "study", today=TODAY) == BIG)

# ── 2. levels：越界峰值不抛，且是精确整数分档 ───────────────────
cuts = activity.levels(d, "study", today=TODAY)
chk("色阶：10**400 峰值不抛，等于 1/4、1/2、3/4 的精确整数",
    cuts == [BIG // 4, BIG // 2, (3 * BIG) // 4], cuts)
chk("色阶：三档都是 int（不是 float / inf），前端 JSON 序列化收得下",
    all(isinstance(c, int) for c in cuts))

# ── 3. summary：整包不抛，avg_day 是整数除法 ────────────────────
s = activity.summary(d, today=TODAY)["study"]
chk("汇总：越界账不抛，week 就是那一笔大数", s["week"] == BIG, s["week"])
chk("汇总：avg_day 用整数除法算出来，等于 week // 7", s["avg_day"] == BIG // 7, s["avg_day"])
chk("汇总：avg_day 是 int，且和 week 对得上（不是 0、不是 inf）",
    isinstance(s["avg_day"], int) and s["avg_day"] * 7 <= s["week"])
chk("汇总：格子里的秒数与档位照样给得出来",
    any(c[0] == "2026-03-03" and c[1] == BIG for c in s["cells"]))

# ── 4. 正常量级一字不改 ────────────────────────────────────────
n = activity.load({"study": {"days": {"2026-03-03": 8000}, "total": 8000}})
chk("正常值：峰值 8000 仍是 2000 / 4000 / 6000（与旧算式一致）",
    activity.levels(n, "study", today=TODAY) == old_levels(8000)
    == [2000, 4000, 6000], activity.levels(n, "study", today=TODAY))
sn = activity.summary(n, today=TODAY)["study"]
chk("正常值：avg_day 与旧算式一致（8000 那一周 / 7 向下取整）",
    sn["avg_day"] == old_avg(sn["week"]) == 8000 // 7, sn["avg_day"])

# ── 5. 边界：刚好到「兜底 15/30/60 分钟」的那一档 ────────────────
b = activity.load({"study": {"days": {"2026-03-03": 899}, "total": 899}})
chk("边界：峰值 899（不足 15 分钟）仍兜底 900 / 1800 / 3600",
    activity.levels(b, "study", today=TODAY) == [900, 1800, 3600])
b = activity.load({"study": {"days": {"2026-03-03": 900}, "total": 900}})
chk("边界：峰值 900 起按 1/4、1/2、3/4 分档", activity.levels(b, "study", today=TODAY) == [225, 450, 675])

print("\n时长账越界整数门禁：%d 项通过，失败 %d 项" % (PASSED, len(FAIL)))
for f in FAIL:
    print("  ✗ " + f)
sys.exit(1 if FAIL else 0)
