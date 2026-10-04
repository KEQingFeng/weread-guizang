"""归藏自己的两本时长账：学习时长与写作时长。

这两笔和「本机阅读时长」（cache/readstat.json）是三回事，各记各的，互不挪用：

    学习时长 —— 归藏开着跑了多久。判据是「后端进程活着 + 页面心跳没断」：
                页面每十几秒报一次，两次心跳之间的墙上时间就是这一段。
    写作时长 —— 人在写作平台那一格里待了多久。进那一格开始算，离开或关掉就停。

为什么要夹、要封顶，都是为了让热力图上的数字诚实，而不是让它好看：

  · **单次最多算 300 秒**：标签页被浏览器冻结、电脑睡过去、页面挂在那儿一整天，
    回来时前端会报一个巨大的间隔。放着不管，第二天图上就是一根通天的柱子。
  · **单日封顶 12 小时**：.app 通宵开着是真实存在的用法。学习时长的定义就是
    「应用累计运行时长」，所以不骗人说这是专注时长；但一天吹出 24 小时会让整张
    日历失去比较意义，12 小时是「今天确实用了很久」和「机器没关」的分界。
  · **只留最近 400 天**：账本是个 JSON，整读整写，不能无限长。

这里只碰账本，不碰文件路径也不碰 HTTP —— 落盘与路由都在 ui_server.py，
纯函数好单测，门禁 tests/check_activity.py 就是直接对着这些函数打。
"""

import datetime as _dt
import time

KINDS = ("study", "write")
ONE_DAY = _dt.timedelta(days=1)
KIND_NAMES = {"study": "学习时长", "write": "写作时长"}

MAX_TICK = 300            # 单次心跳最多计入的秒数
MAX_DAY = 12 * 3600       # 单日封顶（秒）
KEEP_DAYS = 400           # 日历保留最近多少天
YEAR_WEEKS = 53           # 热力图默认铺一年的格子


def blank():
    """一本空账。"""
    return {"days": {}, "total": 0}


def load(d):
    """把磁盘上读出来的东西整理成能用的形状；坏数据按空账处理，不抛。

    账本可能被手工编辑、被旧版本写成别的形状，也可能只有一半。宁可当空的重新
    开始记，也不能因为一格脏数据让个人主界面整页白屏。
    """
    out = {}
    src = d if isinstance(d, dict) else {}
    for kind in KINDS:
        v = src.get(kind)
        v = v if isinstance(v, dict) else {}
        days = v.get("days")
        days = days if isinstance(days, dict) else {}
        clean = {}
        for key, sec in days.items():
            try:
                n = int(sec)
            except Exception:
                continue
            if n > 0 and is_day(str(key)):
                clean[str(key)] = n
        try:
            total = int(v.get("total", 0))
        except Exception:
            total = 0
        out[kind] = {"days": clean, "total": max(total, 0)}
    return out


def is_day(s):
    """这一格是不是一个合法日期。手写坏过的账本（2026-13-40）不能把整页带崩，
    所以只认形状、再让 strptime 兜一层，异常一律当「不是日期」丢掉。"""
    if len(s) != 10 or s[4] != "-" or s[7] != "-":
        return False
    try:
        _dt.date(int(s[:4]), int(s[5:7]), int(s[8:10]))
        return True
    except Exception:
        return False


def tick(d, kind, seconds, day=None):
    """记一段时长进今天这一格。返回这次真正计入的秒数（0 表示没记上）。

    夹到 MAX_TICK、当天已封顶就不再累加 —— 两处都返回真实计入值，界面拿它更新
    自己的读数，于是屏幕上那个数字和账本里的数永远对得上。
    """
    if kind not in KINDS:
        return 0
    try:
        sec = int(round(float(seconds)))
    except Exception:
        return 0
    if sec <= 0:
        return 0
    sec = min(sec, MAX_TICK)
    book = d[kind]
    day = day or time.strftime("%Y-%m-%d")
    cur = int(book["days"].get(day, 0))
    room = MAX_DAY - cur
    if room <= 0:
        return 0
    add = min(sec, room)
    book["days"][day] = cur + add
    book["total"] = int(book.get("total", 0)) + add
    prune(d)
    return add


def prune(d):
    """日历只留最近 KEEP_DAYS 天（总数不动，它是历史累计）。"""
    for kind in KINDS:
        days = d[kind]["days"]
        if len(days) > KEEP_DAYS:
            for key in sorted(days.keys())[:len(days) - KEEP_DAYS]:
                days.pop(key, None)
    return d


def day_total(d, kind, day):
    return int(d.get(kind, blank())["days"].get(day, 0))


def streak(d, kind, today=None):
    """连续多少天。今天没记上不算断 —— 从今天往前数，昨天有就 +1，一路数到空。

    「连续 N 天」是这类日历里唯一会让人每天回来看的数字，所以它对「今天还没开始」
    要宽容：早上打开界面时如果显示连续 0 天，等于前一天白攒了。
    """
    today = today or time.strftime("%Y-%m-%d")
    days = d.get(kind, blank())["days"]
    t = _to_date(today)
    if _ymd(t) not in days:
        t -= ONE_DAY
    n = 0
    while _ymd(t) in days:
        n += 1
        t -= ONE_DAY
    return n


def _to_date(s):
    return _dt.date(int(s[:4]), int(s[5:7]), int(s[8:10]))


def _ymd(t):
    return "%04d-%02d-%02d" % (t.year, t.month, t.day)


def range_total(d, kind, days_back, today=None):
    """最近 N 天（含今天）一共多少秒。热力图上方那行常驻摘要用它。"""
    today = _to_date(today or time.strftime("%Y-%m-%d"))
    days = d.get(kind, blank())["days"]
    total, t = 0, today
    for _ in range(max(0, int(days_back))):
        total += int(days.get(_ymd(t), 0))
        t -= ONE_DAY
    return total


def calendar(d, kind, weeks=YEAR_WEEKS, today=None):
    """铺热力图要的格子：从今天往回推 weeks*7 天，逐日给 [日期, 秒数]。

    **缺失的日子也要返回 0**，不能跳过 —— 前端要按格子画，跳一格整张图就错位，
    而且留白会让人以为数据丢了。
    """
    today = _to_date(today or time.strftime("%Y-%m-%d"))
    span = max(1, int(weeks)) * 7
    start = today - ONE_DAY * (span - 1)
    days = d.get(kind, blank())["days"]
    out, t = [], start
    while t <= today:
        out.append([_ymd(t), int(days.get(_ymd(t), 0))])
        t += ONE_DAY
    return out


def peak(d, kind, today=None):
    """近一年最忙的那天有多少秒 —— 色阶得按每个人自己的量级来，
    不然一个每天只写二十分钟的人，整张图永远是最低一档。"""
    today = _to_date(today or time.strftime("%Y-%m-%d"))
    days = d.get(kind, blank())["days"]
    hi = 0
    for key, sec in days.items():
        if _to_date(key) >= today - ONE_DAY * (YEAR_WEEKS * 7):
            hi = max(hi, int(sec))
    return hi


def levels(d, kind, today=None):
    """四档色阶的秒数边界（升序三个数），按这个人自己的峰值算。

    档位固定成「峰值的 1/4、1/2、3/4」而不是写死小时数：学习的人和写作的人
    量级差很远，共用一套阈值会让其中一张图全是同一色。峰值太小（第一天用）时
    兜底成 15/30/60 分钟，免得第一档就宽到看不出差别。
    """
    hi = peak(d, kind, today=today)
    if hi < 900:
        return [900, 1800, 3600]
    return [max(1, int(hi * 0.25)), int(hi * 0.5), int(hi * 0.75)]


def level_of(sec, cuts):
    """这一格涂第几档（0..4）。"""
    n = 0
    for c in cuts:
        if sec > c:
            n += 1
    return n


def summary(d, today=None):
    """给界面的那份汇总：逐日、总计、今天、本周、日均、连续、色阶、近一年格子。"""
    today = today or time.strftime("%Y-%m-%d")
    out = {}
    for kind in KINDS:
        book = d[kind]
        week = range_total(d, kind, 7, today=today)
        year = range_total(d, kind, YEAR_WEEKS * 7, today=today)
        busy = len([1 for _k, v in calendar(d, kind, YEAR_WEEKS, today=today) if v > 0])
        cuts = levels(d, kind, today=today)
        out[kind] = {
            "name": KIND_NAMES[kind],
            "today": day_total(d, kind, today),
            "total": int(book.get("total", 0)),
            "week": week,
            "year": year,
            "avg_day": int(week / 7) if week else 0,
            "streak": streak(d, kind, today=today),
            "busy_days": busy,
            "max": max([v for _k, v in calendar(d, kind, YEAR_WEEKS, today=today)] or [0]),
            "levels": cuts,
            "cells": [[k, v, level_of(v, cuts)] for k, v in
                      calendar(d, kind, YEAR_WEEKS, today=today)],
        }
    out["today"] = today
    out["cap"] = {"tick": MAX_TICK, "day": MAX_DAY}
    return out


def clear(d, kind=None):
    """清零（kind 不给就两本都清）。返回人话，界面直接显示。"""
    kinds = [kind] if kind in KINDS else list(KINDS)
    for k in kinds:
        d[k] = blank()
    return d, "已清零：" + "、".join(KIND_NAMES[k] for k in kinds)
