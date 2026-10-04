#!/usr/bin/env python3
"""归藏门禁：两本时长账的纯逻辑（activity.py）。

不起服务、不开浏览器：activity 这一层只算数，不碰盘也不碰 HTTP，所以直接对着函数打，
比挂在一个活服务上验得快，也验得准。

它守的是一句话：**热力图上的数字要诚实，不许为了好看而虚高。**

    · 单次心跳夹 300 秒 —— 标签页被浏览器冻结、电脑睡一觉回来，前端会报一个巨大的
      间隔；不夹的话第二天图上就是一根通天的柱子。
    · 单日封顶 12 小时 —— 通宵开着是真实用法，但一天吹出 24 小时会让整张日历失去
      比较意义。
    · 缺失的日子补 0 而不是跳过 —— 前端按格子画，跳一格整张图就错位。
    · 连续天数对「今天还没开始」要宽容 —— 早上打开就显示 0 天，等于前一天白攒了。

落盘与路由在 ui_server.py，那两样由真机套件（tests/check_nav.py）去验；这一份只管
纯函数这一层。
"""
import datetime as _dt
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import activity  # noqa: E402

FAILED = []
PASSED = 0


def chk(name, cond, extra=""):
    global PASSED
    if cond:
        PASSED += 1
    else:
        FAILED.append(name)
    print(("PASS  " if cond else "FAIL  ") + name + ("" if cond else "   " + repr(extra)))


def fresh():
    return activity.load({})


# ── 1. 常量口径 ─────────────────────────────────────────────────
chk("常量：单次最多计 300 秒", activity.MAX_TICK == 300, activity.MAX_TICK)
chk("常量：单日封顶 12 小时", activity.MAX_DAY == 12 * 3600, activity.MAX_DAY)
chk("常量：日历留最近 400 天", activity.KEEP_DAYS == 400, activity.KEEP_DAYS)
chk("常量：两本账就叫学习和写作", activity.KINDS == ("study", "write"), activity.KINDS)

# ── 2. tick：夹、封顶、返回真实计入值 ────────────────────────────
d = fresh()
chk("空账：两本都在，都是 0", set(d) == set(activity.KINDS) and all(d[k]["total"] == 0 for k in activity.KINDS))
chk("记一笔：120 秒原样计入，返回 120", activity.tick(d, "study", 120) == 120
    and activity.day_total(d, "study", activity._ymd(_dt.date.today())) == 120)
chk("夹：一次报 9999 秒也最多计 300", activity.tick(d, "study", 9999) == 300
    and activity.day_total(d, "study", activity._ymd(_dt.date.today())) == 420)

d2 = fresh()
day = "2026-01-20"
for _ in range(143):                     # 143 * 300 = 42900，还差 300 到顶
    activity.tick(d2, "study", 300, day=day)
chk("封顶：一天记到 42900 秒（11.9 小时）还没满", activity.day_total(d2, "study", day) == 42900,
    activity.day_total(d2, "study", day))
chk("封顶：再记一笔只补到 12 小时整（43200），不是整笔 300 全收",
    activity.tick(d2, "study", 300, day=day) == 300 and activity.day_total(d2, "study", day) == 43200,
    activity.day_total(d2, "study", day))
chk("封顶：满了之后一分一秒都不再记", activity.tick(d2, "study", 300, day=day) == 0)

chk("坏输入：不认识的那本账不记", activity.tick(fresh(), "nope", 60) == 0)
chk("坏输入：0 秒不记", activity.tick(fresh(), "study", 0) == 0)
chk("坏输入：负秒不记", activity.tick(fresh(), "study", -120) == 0)
chk("坏输入：不是数字的不记", activity.tick(fresh(), "study", "abc") == 0)
chk("两本账各记各的：记学习不动写作",
    (lambda x: (activity.tick(x, "study", 60), x["write"]["total"] == 0)[1])(fresh()))

# ── 3. 连续天数：今天没记不算断 ─────────────────────────────────
d3 = fresh()
d3["study"]["days"] = {"2026-03-01": 60, "2026-03-02": 60, "2026-03-03": 60}
chk("连续：今天没记时，从昨天往前数（早上打开不该显示 0 天）",
    activity.streak(d3, "study", today="2026-03-04") == 3, activity.streak(d3, "study", today="2026-03-04"))
chk("连续：今天记了就含今天", activity.streak(d3, "study", today="2026-03-03") == 3)
chk("连续：中间断一天就归零重数",
    (lambda: (d3["study"]["days"].pop("2026-03-02", None),
              activity.streak(d3, "study", today="2026-03-03"))[1])() == 1)

# ── 4. 日历：逐日铺满、缺的补 0、按周数 ─────────────────────────
d4 = fresh()
d4["study"]["days"] = {"2026-03-03": 600}
cal = activity.calendar(d4, "study", weeks=4, today="2026-03-05")
chk("日历：4 周就是 28 格", len(cal) == 28, len(cal))
chk("日历：最后一格是今天", cal[-1][0] == "2026-03-05", cal[-1])
chk("日历：没有记录的日子也在，值是 0（跳一格整张图就错位）",
    [k for k, v in cal if v == 0] and ("2026-03-02" in [k for k, _v in cal]))
chk("日历：有记录那天给的是真值", dict(cal)["2026-03-03"] == 600, dict(cal)["2026-03-03"])
chk("日历：日期升序、不重不乱", [k for k, _v in cal] == sorted(k for k, _v in cal))

# ── 5. 色阶：按自己的峰值来，不是写死小时数 ─────────────────────
d5 = fresh()
chk("色阶：第一天（峰值很小）兜底成 15 / 30 / 60 分钟", activity.levels(d5, "study") == [900, 1800, 3600],
    activity.levels(d5, "study"))
d5["study"]["days"] = {"2026-03-03": 8000}
chk("色阶：有峰值时按峰值的 1/4、1/2、3/4 分档",
    activity.levels(d5, "study", today="2026-03-05") == [2000, 4000, 6000],
    activity.levels(d5, "study", today="2026-03-05"))
chk("档位：0 秒最低档，越过三个边界逐级加一",
    [activity.level_of(v, [10, 20, 30]) for v in (0, 10, 20, 30, 40)] == [0, 0, 1, 2, 3])

# ── 6. 归档：只留最近 400 天 ────────────────────────────────────
d6 = fresh()
base = _dt.date(2025, 1, 1)
for i in range(450):
    d6["study"]["days"][activity._ymd(base + _dt.timedelta(days=i))] = 60
d6["study"]["total"] = 450 * 60      # 这四百多笔都是真实记进来的，总数就是它们之和
activity.prune(d6)
chk("归档：账本再长也只留最近 400 天", len(d6["study"]["days"]) == 400, len(d6["study"]["days"]))
chk("归档：留的是最近的那 400 天（最老的那批被削掉）",
    activity._ymd(base) not in d6["study"]["days"]
    and activity._ymd(base + _dt.timedelta(days=449)) in d6["study"]["days"])
chk("归档：总数不跟着削（它是历史累计）", d6["study"]["total"] == 450 * 60, d6["study"]["total"])

# ── 7. 脏数据不许把整页带崩 ─────────────────────────────────────
messy = activity.load({
    "study": {"days": {"2026-13-40": 100, "abc": 5, "2026-03-03": "180", "2026-03-04": -9,
                       "2026-03-05": 60}, "total": "x"},
    "write": "不是字典",
    "junk": {"whatever": 1},
})
chk("脏账：假日期（2026-13-40）丢掉", "2026-13-40" not in messy["study"]["days"])
chk("脏账：非日期键丢掉", "abc" not in messy["study"]["days"])
chk("脏账：负秒丢掉", "2026-03-04" not in messy["study"]["days"])
chk("脏账：字符串数字认成数字", messy["study"]["days"].get("2026-03-05") == 60)
chk("脏账：坏掉的整本账当空账，不抛", messy["write"] == {"days": {}, "total": 0}, messy["write"])
chk("脏账：不认识的键不进账", "junk" not in messy)
chk("is_day：形状对但日子不存在要挡下", not activity.is_day("2026-13-40") and not activity.is_day("abc")
    and not activity.is_day("2026-1-1") and activity.is_day("2026-03-05"))

# ── 8. 汇总包：界面那一屏吃的形状 ───────────────────────────────
d8 = fresh()
d8["study"]["days"] = {"2026-03-03": 3600, "2026-03-04": 1800}
sm = activity.summary(d8, today="2026-03-05")
chk("汇总：两本账都在，各带名字", sm["study"]["name"] == "学习时长" and sm["write"]["name"] == "写作时长")
chk("汇总：一格是 [日期, 秒数, 档位] 三样", all(len(c) == 3 and isinstance(c[0], str)
    and isinstance(c[1], int) and isinstance(c[2], int) for c in sm["study"]["cells"]))
chk("汇总：格子数就是 53 周", len(sm["study"]["cells"]) == 53 * 7, len(sm["study"]["cells"]))
chk("汇总：带着两道夹的参数给界面（读数要对得上）",
    sm["cap"] == {"tick": 300, "day": 43200}, sm["cap"])
chk("汇总：今天 / 总计 / 连续三个数都在", all(k in sm["study"] for k in ("today", "total", "streak", "week", "year")))

# ── 9. 清零 ─────────────────────────────────────────────────────
d9 = fresh()
activity.tick(d9, "study", 600, day="2026-03-05")
activity.tick(d9, "write", 300, day="2026-03-05")
d9, msg1 = activity.clear(d9, "study")
chk("清零：只清一本时另一本留着", d9["study"]["total"] == 0 and d9["write"]["total"] == 300)
chk("清零：回话说清清了哪一本", "学习时长" in msg1 and "写作" not in msg1, msg1)
d9, msg2 = activity.clear(d9)
chk("清零：不给参数就两本都清，回话点名两本", d9["study"]["total"] == 0 and d9["write"]["total"] == 0
    and "学习时长" in msg2 and "写作时长" in msg2)

print("\n时长账门禁：%d 项通过，失败 %d 项" % (PASSED, len(FAILED)))
for f in FAILED:
    print("  ✗ " + f)
sys.exit(1 if FAILED else 0)
