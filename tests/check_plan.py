# -*- coding: utf-8 -*-
"""阅读计划（readplan.py）的离线自测。

这一路的规矩全在纯逻辑里：页数怎么由字符数折算、进度为什么只增不减、今日 / 本周的账
记在哪一刻、读完那一刻有没有留档、改目标会不会把读过的部分清零。它们都不该靠 "翻书
看一眼" 来验 —— 把时钟和日期交给调用方传进来，同一段账本在任何一天跑出来的数都一样。

所以这一套不连服务、不读磁盘、不看真实时间：全程喂合成的书目录（各章带 chars）和
写死的 now / today，逐条钉成可失败的检查。
"""
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent
REPO = HERE.parent
sys.path.insert(0, str(REPO))
import readplan as rp  # noqa: E402

FAIL = []
PASSED = [0]


def chk(name, cond, extra=""):
    if cond:
        PASSED[0] += 1
    else:
        FAIL.append(name)
    print(("PASS  " if cond else "FAIL  ") + name + ("" if cond else "   " + str(extra)[:240]))


# 五章、每章 1000 字 → 全书 5 页。章边界正好落在整页上，好算也好读。
CH = [{"chars": 1000} for _ in range(5)]

# 一个固定时刻：2026-03-05 12:00（本机时区）。所有 now 都从它派生，结果可复现。
DAY = "2026-03-05"
NOW = int(rp.datetime.strptime(DAY + " 12:00", "%Y-%m-%d %H:%M").timestamp())


def day(n):
    return (rp.datetime.strptime(DAY, "%Y-%m-%d") + rp.timedelta(days=n)).strftime("%Y-%m-%d")


def at(n):
    return int(rp.datetime.strptime(day(n) + " 12:00", "%Y-%m-%d %H:%M").timestamp())


# ── 1 页数折算 ────────────────────────────────────────────────
chk("总页数 = 字符数 / 1000 向上取整", rp.total_pages(CH) == 5, rp.total_pages(CH))
chk("空目录按 1 页算（不出现除以零）", rp.total_pages([]) == 1)
chk("3500 字 → 4 页（向上取整）", rp.total_pages([{"chars": 3500}]) == 4)
chk("还没动（第 0 章、章内 0）：0 页", rp.pages_at(CH, 0, 0) == 0)
chk("读到第 1 章章首：1 页", rp.pages_at(CH, 1, 0) == 1)
chk("读到第 2 章章首：2 页", rp.pages_at(CH, 2, 0) == 2)
chk("章内一半也算前进（第 0 章、50%）：1 页", rp.pages_at(CH, 0, 0.5) == 1)
chk("负的章序号夹到第 0 章", rp.pages_at(CH, -3, 0) == 0)
chk("章序号越界 = 读完全书", rp.pages_at(CH, 99, 0) == 5)
chk("章内比例越界（>1）夹到章尾", rp.pages_at(CH, 3, 5) == 4)

# ── 2 定计划：默认终点日按节奏推算 ────────────────────────────
store = rp.blank()
p = rp.put_plan(store, "B1", "甲书", 300, "daily", 20, now=NOW)
chk("建计划：记下书名与总页数", p["title"] == "甲书" and p["pages"] == 300, p)
chk("建计划：起点是当天", p["started"] == DAY, p["started"])
chk("每天 20 页读 300 页 → 15 天后读完", p["target"] == day(15), p["target"])
chk("建计划：初始进度为 0", p["high"] == 0 and p["done_at"] == 0, p)
chk("建计划：历史是空的", p["history"] == [])

p2 = rp.put_plan(store, "B2", "乙书", 300, "weekly", 100, now=NOW)
chk("每周 100 页读 300 页 → 3 周后读完", p2["target"] == day(21), p2["target"])
chk("每周计划的周期字段正确", p2["goal"] == {"period": "weekly", "pages": 100}, p2["goal"])

# 用户自己指定终点日就不推算
p3 = rp.put_plan(store, "B3", "丙书", 300, "daily", 20, now=NOW, target="2026-04-01")
chk("指定终点日就照用", p3["target"] == "2026-04-01", p3["target"])

# ── 3 记进度：只增不减，今日的账记在跨新高那一刻 ──────────────
rp.record_pos(store, "B1", CH, 1, 0, now=NOW)
p = rp.get(store, "B1")
chk("读到第 1 章章首 → 高水位 1 页", p["high"] == 1, p["high"])
chk("今日账加了 1 页", p["days"][DAY] == 1, p["days"])
rp.record_pos(store, "B1", CH, 0, 0, now=NOW)
chk("往回翻不动高水位", rp.get(store, "B1")["high"] == 1)
chk("往回翻也不减今日的账", rp.get(store, "B1")["days"][DAY] == 1)
rp.record_pos(store, "B1", CH, 3, 0, now=at(1))
p = rp.get(store, "B1")
chk("第二天读到第 3 章 → 高水位 3 页", p["high"] == 3, p["high"])
chk("只在跨新高的那一刻记账（次日记 2 页）", p["days"][day(1)] == 2, p["days"])
chk("前一天的账不被改写", p["days"][DAY] == 1, p["days"])

# ── 4 读完那一刻自动留档 ──────────────────────────────────────
rp.record_pos(store, "B1", CH, 99, 0, now=at(2))
p = rp.get(store, "B1")
chk("读到超出末章 = 读完全书，标记完成", bool(p["done_at"]), p)
chk("完成时高水位顶到总页数", p["high"] == 5, p["high"])
chk("完成留下一笔档案", len(p["history"]) == 1, p["history"])
h = p["history"][0]
chk("档案记下起点、页数、目标", h["started"] == DAY and h["pages"] == 5
    and h["goal"] == {"period": "daily", "pages": 20}, h)
chk("档案记下共读了几天（3 天）", h["days"] == 3, h["days"])
rp.record_pos(store, "B1", CH, 1, 0, now=at(9))
chk("完成后再翻不再改动档案", len(rp.get(store, "B1")["history"]) == 1)

# ── 5 读出样子：进度、还剩多少、达标没有 ──────────────────────
v = rp.view(store, "B3", total=300, now=NOW, today=DAY)
chk("没有进度时 read=0、left=全书", v["read"] == 0 and v["left"] == 300, v)
chk("没有进度不算达标", v["hit"] is False)
rp.record_pos(store, "B3", CH, 2, 0, now=NOW)
rp.put_plan(store, "B3", "丙书", 5, "daily", 2, now=NOW, target="2026-04-01")   # 保进度地改目标
v = rp.view(store, "B3", total=5, now=NOW, today=DAY)
chk("改目标后读过的部分没被清零", v["read"] == 2, v)
chk("改目标后目标变了", v["goal"] == {"period": "daily", "pages": 2}, v["goal"])
chk("读 2 / 共 5 → 40%", v["pct"] == 40, v["pct"])
chk("还剩 3 页", v["left"] == 3, v["left"])
chk("今天读了 2 页，等于目标 → 达标", v["today"] == 2 and v["hit"] is True, v)
chk("已过 1 天、读了 2 页 → 节奏 2.0", v["pace"] == 2.0, v["pace"])
chk("起点 + 已读 / 节奏能推出读完日子", bool(v["eta"]), v["eta"])
chk("有终点日就按终点日判在不在轨", isinstance(v["on_pace"], bool), v)

vc = rp.view(store, "B1", total=5, now=NOW, today=DAY)
chk("已完成的计划：done_at 有值", bool(vc["done_at"]))
chk("已完成的计划：在轨恒为真", vc["on_pace"] is True)
chk("已完成的计划：记下共读几天", vc["took"] >= 1, vc.get("took"))
chk("已完成的计划：带出历史档案", len(vc["history"]) == 1)

# ── 6 本周的账（每周模式）────────────────────────────────────
w = rp.blank()
rp.put_plan(w, "W1", "周书", 100, "weekly", 50, now=NOW)
rp.record_pos(w, "W1", [{"chars": 100000}], 0, 0.2, now=NOW)      # 20 页
rp.record_pos(w, "W1", [{"chars": 100000}], 0, 0.5, now=at(1))    # 累计 50 页 → 今天再 30
vw = rp.view(w, "W1", total=100, now=at(1), today=day(1))
chk("每周模式：本周累计跨天相加", vw["period"] == 50, vw)
chk("每周模式：今天只算今天那 30 页", vw["today"] == 30, vw["today"])
chk("每周模式：本周达标", vw["hit"] is True)

# ── 7 账本规范化：坏数据不许带崩 ──────────────────────────────
dirty = {"plans": {
    "A": {"pages": "abc", "goal": {"period": "monthly", "pages": 0}, "high": -5,
          "days": {"bad": 3, "2026-01-02": "4", "2026-01-03": -9},
          "history": [{"pages": 0, "days": "x"}, "junk"]},
    "B": "整条坏了",
    "": {"pages": 3},
}}
clean = rp.load(dirty)
chk("坏页数被夹到 1", clean["plans"]["A"]["pages"] == 1, clean["plans"].get("A"))
chk("认不出的周期退回 daily", clean["plans"]["A"]["goal"]["period"] == "daily")
chk("目标页数 0 夹到 1", clean["plans"]["A"]["goal"]["pages"] == 1)
chk("负的高水位夹到 0", clean["plans"]["A"]["high"] == 0)
chk("坏日期键被丢掉、好的留下", set(clean["plans"]["A"]["days"]) == {"2026-01-02"},
    clean["plans"]["A"]["days"])
chk("坏历史条目被丢掉、好的留下", len(clean["plans"]["A"]["history"]) == 1)
chk("整条坏的计划被丢掉", "B" not in clean["plans"])
chk("空书号被丢掉", "" not in clean["plans"])
chk("不是 dict 的账本也能兜住", rp.load(None)["plans"] == {})
chk("读一本没计划的书：has=false", rp.view(clean, "没有这本")["has"] is False)

# ── 8 列出在进行的计划 + 清掉一本 ────────────────────────────
act = rp.list_active(store)
chk("在进行的计划里没有被做完的那本", all(x["book"] != "B1" for x in act),
    [x["book"] for x in act])
chk("在进行的计划里有 B3", any(x["book"] == "B3" for x in act))
chk("清掉一本计划后查不到它", rp.clear_plan(store, "B3") and not rp.get(store, "B3"))
chk("清一本不影响别人", bool(rp.get(store, "B2")))

print()
if FAIL:
    print("阅读计划离线自查有 %d 处不对：%s" % (len(FAIL), "、".join(FAIL)))
else:
    print("阅读计划这一路：页数按字数折算、进度只增不减、跨天记账、读完留档、"
          "改目标不清零、坏账本兜得住。")
sys.exit(len(FAIL))
