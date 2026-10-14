#!/usr/bin/env python3
"""归藏门禁：本机阅读时长账本（cache/readstat.json）的脏数据兜底。

一句话：**账本里有脏值，个人主界面不许白屏。**

这份 json 不只归藏这个进程在写 —— 云同步（sync.py）会把远端那一份原样落盘，用户也
可能手工编辑，旧版本写下的形状更没人保证。`activity.py` 那本「学习 / 写作时长」账
早就按「认不出的当空账」洗过一遍（见 tests/check_activity.py 第 7 节），本机阅读时长
这本更老的账却一直直读直用：`days` 里一个 "abc"、`books` 里一个字符串 seconds，都会
让 `/api/readstat` 与阅读时每十几秒一次的 `/api/readstat/tick` 抛异常（读数取不到，
心跳还一直报错），界面上就是「阅读统计」那一屏一直转圈。

所以这一条把坏形状逐样喂进去，要求 load_readstat / readstat_summary / readstat_tick
都不抛，而且能认的数字仍然认得出、脏的才丢掉（不是一刀切成空账）。

全程在临时沙盒里跑（先把 GUIZANG_DATA 指过去再 import ui_server），不碰真实 cache/。
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

SB = pathlib.Path(tempfile.mkdtemp(prefix="gz-readstat-"))
os.environ["GUIZANG_DATA"] = str(SB)
os.environ["GUIZANG_BOOKS"] = str(SB / "books")

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


def put(text):
    os.makedirs(os.path.dirname(us.READSTAT_PATH), exist_ok=True)
    with open(us.READSTAT_PATH, "w", encoding="utf-8") as f:
        f.write(text)


def fresh():
    us.save_readstat({"total": 0, "days": {}, "books": {}})
    return us.load_readstat()


# ── 1. 脏账：认得出的留下、认不出的丢掉，全程不抛 ──────────────
put(json.dumps({
    "total": "oops",
    "days": {"2026-13-40": 100, "abc": 5, "2026-03-05": "60",
             "2026-03-04": -9, "2026-03-06": 0, "2026-03-07": None},
    "books": {"good": {"title": " 书名 ", "seconds": "42", "last": None},
              "bad": "不是字典",
              "": {"title": "空 id", "seconds": 1},
              "space": {"title": 123, "seconds": "no", "last": -7}},
}, ensure_ascii=False))
d = us.load_readstat()
chk("脏账：坏掉的 total 归 0", d["total"] == 0, d["total"])
chk("脏账：假日期 2026-13-40 丢掉", "2026-13-40" not in d["days"])
chk("脏账：非日期键 abc 丢掉", "abc" not in d["days"])
chk("脏账：负秒那天丢掉", "2026-03-04" not in d["days"])
chk("脏账：0 秒那天丢掉（不携带信息）", "2026-03-06" not in d["days"])
chk("脏账：null 那天丢掉", "2026-03-07" not in d["days"])
chk("脏账：字符串数字认成数字", d["days"].get("2026-03-05") == 60, d["days"])
chk("脏账：不是字典的那本书丢掉", "bad" not in d["books"])
chk("脏账：空书名 id 丢掉", "" not in d["books"])
chk("脏账：好书的秒数认字符串、时间戳坏值归 0",
    d["books"]["good"] == {"title": "书名", "seconds": 42, "last": 0}, d["books"]["good"])
chk("脏账：书名不是字符串给空串、负时间戳归 0",
    d["books"]["space"] == {"title": "", "seconds": 0, "last": 0}, d["books"]["space"])
chk("脏账：整份不是字典当空账", us._ledger_int("x") == 0 and us._ledger_int(None) == 0)

# 老代码在这里抛 TypeError / ValueError（books 的 seconds 是字符串时排序取负）
try:
    sm = us.readstat_summary()
    chk("脏账：readstat_summary 不抛，回一份能用的形状",
        sm["total"] == 0 and isinstance(sm["days"], list) and isinstance(sm["books"], list), sm)
except Exception as e:
    chk("脏账：readstat_summary 不抛，回一份能用的形状", False, "%s: %s" % (type(e).__name__, e))

try:
    ok = us.readstat_tick("good", "书名", 10)
    chk("脏账：readstat_tick 也不抛，照常记一笔", ok is True)
except Exception as e:
    chk("脏账：readstat_tick 也不抛，照常记一笔", False, "%s: %s" % (type(e).__name__, e))

# ── 2. 整份坏掉的 json / 不是对象的 json：当空账，不抛 ─────────
for raw in ("{不是 json", "[1, 2, 3]", "null"):
    put(raw)
    try:
        d = us.load_readstat()
        chk("坏文件 %r：当空账" % raw, d == {"total": 0, "days": {}, "books": {}}, d)
    except Exception as e:
        chk("坏文件 %r：当空账" % raw, False, "%s: %s" % (type(e).__name__, e))

# ── 3. 好数据一个字不改地认下来（兜底不是「一刀切空」）──────────
us.save_readstat({
    "total": 3600,
    "days": {"2026-03-05": 600, "2026-03-06": 300},
    "books": {"b1": {"title": "一本", "seconds": 900, "last": 1770000000}},
})
d = us.load_readstat()
chk("好账：总数原样", d["total"] == 3600, d["total"])
chk("好账：逐日两笔都在", d["days"] == {"2026-03-05": 600, "2026-03-06": 300}, d["days"])
chk("好账：每本书原样",
    d["books"]["b1"] == {"title": "一本", "seconds": 900, "last": 1770000000}, d["books"])

# ── 4. tick → summary 数字对得上（屏幕与账本同一份）───────────
fresh()
chk("记账：42 秒计入并回 True", us.readstat_tick("b1", "书甲", 42) is True)
today = time.strftime("%Y-%m-%d")
sm = us.readstat_summary()
chk("记账：总数与那一格都对得上", sm["total"] == 42 and [today, 42] in sm["days"], sm)
chk("记账：每本书也记上了", sm["books"] and sm["books"][0]["id"] == "b1"
    and sm["books"][0]["seconds"] == 42 and sm["books"][0]["title"] == "书甲", sm["books"])

chk("夹：一次报 99999 秒最多记 120", us.readstat_tick("b1", "书甲", 99999) is True
    and us.readstat_summary()["total"] == 162, us.readstat_summary()["total"])
chk("坏输入：0 秒不记", us.readstat_tick("b1", "书甲", 0) is False)
chk("坏输入：负秒不记", us.readstat_tick("b1", "书甲", -30) is False)
chk("坏输入：不是数字的不记", us.readstat_tick("b1", "书甲", "abc") is False)
chk("坏输入：全都拒了之后总数没动", us.readstat_summary()["total"] == 162)

# 没给书号也照记（前端可能还没拿到书的 id）
before = us.readstat_summary()["total"]
chk("没有书号：时长照样进总账", us.readstat_tick("", "", 10) is True
    and us.readstat_summary()["total"] == before + 10)

# ── 5. 一本一本排序 + 书名夹到 120 ─────────────────────────────
us.save_readstat({"total": 0, "days": {}, "books": {
    "small": {"title": "少", "seconds": 10, "last": 1},
    "big": {"title": "多", "seconds": 99, "last": 2},
}})
sm = us.readstat_summary()
chk("排序：秒数多的排前面", [b["id"] for b in sm["books"]] == ["big", "small"], sm["books"])
us.save_readstat({"total": 0, "days": {}, "books": {
    "x": {"title": "长" * 500, "seconds": 1, "last": 1}}})
chk("书名：超长的夹到 120 个字", len(us.load_readstat()["books"]["x"]["title"]) == 120)

# ── 6. 归档：只留最近 400 天 ───────────────────────────────────
days = {}
base = _dt.date(2024, 1, 1)
for i in range(450):
    days[(base + _dt.timedelta(days=i)).strftime("%Y-%m-%d")] = 60
us.save_readstat({"total": 450 * 60, "days": days, "books": {}})
d = us.load_readstat()
first_kept = (base + _dt.timedelta(days=50)).strftime("%Y-%m-%d")
chk("归档：账本再长也只留 400 天", len(d["days"]) == 400, len(d["days"]))
chk("归档：削掉的是最老的那 50 天、留的是最近 400 天",
    "2024-01-01" not in d["days"] and first_kept in d["days"], len(d["days"]))
chk("归档：总数不跟着削（它是历史累计）", d["total"] == 450 * 60, d["total"])

# ── 7. 清零 ───────────────────────────────────────────────────
us.save_readstat({"total": 100, "days": {time.strftime("%Y-%m-%d"): 100}, "books": {}})
ok, msg = us.readstat_clear()
chk("清零：回 True 且带人话", ok is True and "清零" in msg, msg)
chk("清零：账本真的空了", us.load_readstat() == {"total": 0, "days": {}, "books": {}})

shutil.rmtree(SB, ignore_errors=True)
print("\n本机阅读时长门禁：%d 项通过，失败 %d 项" % (PASSED, len(FAIL)))
for f in FAIL:
    print("  ✗ " + f)
sys.exit(1 if FAIL else 0)
