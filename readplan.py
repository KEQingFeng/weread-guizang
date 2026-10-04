# -*- coding: utf-8 -*-
"""每本书的阅读计划：定一段每日或每周的节奏，读到哪、还剩多少、何时读完，一笔笔记着。

为什么页数由字符数折算
    阅读器是连着滚的，没有一个「一页」的物理单位；微信读书那边给的是百分比，也不是页。
    若按「当前渲染高度」算页，换个字号、换个窗口宽度，同一本书的页数就变了 —— 那样
    「还剩多少页」就成了每天在变的空话。所以这里锚在正文本身：每 PAGE_CHARS 个字符记作
    一页。它跟显示设置无关，同一本书今天明天是同一个数，谈「稳定的阅读节奏」才立得住。

为什么进度取「只增不减」的高水位
    翻回上一章查一个词，位置会往回走。若照单全收，「今天读了 30 页」会缩成 8 页，打卡就
    假了。所以位置只用来算高水位，今日 / 本周的账记在跨过新高水位的那一刻。

为什么完成即留档
    「完成计划有完整记录」是这一条的正题：读完的那一刻把当时的目标、起止日期、共读了几天
    存进 history，之后再定新计划也不会把它冲掉 —— 给每本书一个交代。

这一层是纯逻辑：进出都是 dict，不碰磁盘、不看时钟（now / today 由调用方传），
所以能脱开服务单测。落盘与加锁在 ui_server.py。
"""

import math
import re
import time
from datetime import date, datetime, timedelta

# 一页 = 多少字符。取整千，好算也好解释（界面上也是这么写的）。
PAGE_CHARS = 1000
KINDS = ("daily", "weekly")
_DAY_RE = re.compile(r"\d{4}-\d{2}-\d{2}\Z")
MAX_HISTORY = 40


# ─────────────────────────── 页数与位置 ───────────────────────────

def total_pages(chapters):
    """一本书的总页数：各章字符数之和折算，至少 1 页。"""
    chars = 0
    for c in chapters or []:
        try:
            chars += int((c or {}).get("chars") or 0)
        except Exception:
            pass
    return max(1, int(math.ceil(chars / float(PAGE_CHARS))))


def pages_at(chapters, at, frac=0.0):
    """「读到第 at 章（0 基）其中 frac 处」折成第几页（四舍五入）。

    还没动（第一章、章内 0）算 0 页 —— 否则「翻开一本书」就记了 1 页，今日的账会虚。
    at 越界（读到尾声时前端可能报越界值）一律当读完全书，返回总页数。
    """
    chapters = chapters or []
    if not chapters:
        return 0
    at = int(at or 0)
    if at >= len(chapters):
        return total_pages(chapters)
    if at < 0:
        at = 0
    try:
        frac = float(frac or 0.0)
    except Exception:
        frac = 0.0
    frac = max(0.0, min(1.0, frac))
    if at == 0 and frac <= 0:
        return 0
    before = 0
    for i in range(at):
        try:
            before += int((chapters[i] or {}).get("chars") or 0)
        except Exception:
            pass
    try:
        here = int((chapters[at] or {}).get("chars") or 0)
    except Exception:
        here = 0
    pages = (before + frac * here) / float(PAGE_CHARS)
    return max(1, int(round(pages)))


# ─────────────────────────── 账本规范化 ───────────────────────────

def blank():
    return {"version": 1, "plans": {}}


def get(store, bid):
    """取一本书的计划 dict（没有给 None）。给调用方和测试一个共同入口。"""
    p = (store or {}).get("plans", {}).get(bid)
    return p if isinstance(p, dict) else None


def load(raw):
    """把一个可能残缺 / 被改坏的账本收拾成能用的样子。认不出的条目直接丢。"""
    src = raw if isinstance(raw, dict) else {}
    plans = src.get("plans")
    out = blank()
    if not isinstance(plans, dict):
        return out
    for bid, v in plans.items():
        p = _norm_plan(v)
        if p is not None and isinstance(bid, str) and bid:
            out["plans"][bid] = p
    return out


def _int(v, lo=0, hi=None):
    try:
        n = int(v)
    except Exception:
        n = lo
    n = max(lo, n)
    if hi is not None:
        n = min(hi, n)
    return n


def _norm_goal(g):
    g = g if isinstance(g, dict) else {}
    period = g.get("period") if g.get("period") in KINDS else "daily"
    return {"period": period, "pages": max(1, _int(g.get("pages"), 1, 100000))}


def _norm_plan(v):
    if not isinstance(v, dict):
        return None
    started = str(v.get("started") or "")
    if not _DAY_RE.match(started):
        started = _day_from_epoch(_int(v.get("created_at") or v.get("last_at"), 0))
    days = {}
    raw_days = v.get("days")
    if isinstance(raw_days, dict):
        for k, n in raw_days.items():
            if isinstance(k, str) and _DAY_RE.match(k):
                n = _int(n, 0, 100000000)
                if n > 0:               # 0 页的那天不记：它不携带任何信息，留着只是噪声
                    days[k] = n
    hist = []
    raw_hist = v.get("history")
    if isinstance(raw_hist, list):
        for h in raw_hist[-MAX_HISTORY:]:
            if not isinstance(h, dict):
                continue
            hist.append({
                "started": str(h.get("started") or ""),
                "done_at": _int(h.get("done_at"), 0),
                "pages": max(1, _int(h.get("pages"), 1, 10000000)),
                "goal": _norm_goal(h.get("goal")),
                "days": _int(h.get("days"), 0, 100000),
            })
    return {
        "title": str(v.get("title") or "")[:120],
        "pages": max(1, _int(v.get("pages"), 1, 10000000)),
        "goal": _norm_goal(v.get("goal")),
        "started": started,
        "target": str(v.get("target") or "") if _DAY_RE.match(str(v.get("target") or "")) else "",
        "at": _int(v.get("at"), 0, 1000000),
        "frac": _clamp01(v.get("frac")),
        "high": _int(v.get("high"), 0, 10000000),
        "created_at": _int(v.get("created_at"), 0),
        "last_at": _int(v.get("last_at"), 0),
        "done_at": _int(v.get("done_at"), 0),
        "days": days,
        "history": hist,
    }


def _clamp01(v):
    try:
        f = float(v)
    except Exception:
        return 0.0
    return max(0.0, min(1.0, round(f, 4)))


# ─────────────────────────── 日期小工具 ───────────────────────────

def today_str(now=None):
    return _day_from_epoch(now if now is not None else time.time())


def _day_from_epoch(ts):
    try:
        return datetime.fromtimestamp(int(ts)).strftime("%Y-%m-%d")
    except Exception:
        return time.strftime("%Y-%m-%d")


def _parse_day(s):
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except Exception:
        return None


def _week_start(d):
    """这一周的周一（ISO 周）。"""
    return d - timedelta(days=d.weekday())


def _add_days(d, n):
    return d + timedelta(days=int(n))


# ─────────────────────────── 定计划 / 改计划 ───────────────────────────

def put_plan(store, bid, title, total, period, amount, now=None, target=""):
    """建或改一本书的计划。total 是当前总页数（由书本目录现算）。

    改计划保留已读高水位与已有的每日账：换个目标不该把读过的部分清零，否则
    「改个目标」就等于自己给自己记了个「今天白读」。
    """
    store.setdefault("plans", {})
    now = int(now if now is not None else time.time())
    g = _norm_goal({"period": period, "pages": amount})
    old = store["plans"].get(bid)
    base = old if (old and not old.get("done_at")) else None
    started = base["started"] if base else _day_from_epoch(now)
    p = {
        "title": str(title or (base or {}).get("title") or "")[:120],
        "pages": max(1, _int(total, 1, 10000000)),
        "goal": g,
        "started": started,
        "target": target if _DAY_RE.match(str(target or "")) else (
            base["target"] if base else suggest_target(started, total, g)),
        "at": base["at"] if base else 0,
        "frac": base["frac"] if base else 0.0,
        "high": base["high"] if base else 0,
        "created_at": base["created_at"] if base else now,
        "last_at": base["last_at"] if base else now,
        "done_at": 0,
        "days": dict(base["days"]) if base else {},
        "history": list(base["history"]) if base else (list(old["history"]) if old else []),
    }
    store["plans"][bid] = p
    return p


def suggest_target(started, total, goal):
    """按目标节奏推算「照这个速度该哪天读完」。定计划时用它当默认的终点。"""
    d = _parse_day(started)
    if not d:
        return ""
    amount = max(1, _int((goal or {}).get("pages"), 1))
    periods = max(1, int(math.ceil(max(1, _int(total, 1)) / float(amount))))
    days = periods if (goal or {}).get("period") == "daily" else periods * 7
    return _add_days(d, days).strftime("%Y-%m-%d")


def record_pos(store, bid, chapters, at, frac=0.0, now=None):
    """记一次读到哪。chapters 是这本书的目录（各章带 chars），页数由它现算。

    只增不减：位置往回了不动高水位，于是今日 / 本周的账只会在真的往前读时增长。
    """
    p = store.get("plans", {}).get(bid)
    if not p or p.get("done_at"):
        return p or None
    now = int(now if now is not None else time.time())
    total = total_pages(chapters)
    # 书在磁盘上就是页数的真源：补了章、重取过、目录变了，都以这一次读到的目录为准。
    # 存下来的 pages 只是「书不在手边时」的兜底，不该拿它去顶真实的目录。
    p["pages"] = total
    read = pages_at(chapters, at, frac)
    p["at"] = _int(at, 0, 1000000)
    p["frac"] = _clamp01(frac)
    p["last_at"] = now
    high = int(p.get("high") or 0)
    day = _day_from_epoch(now)
    if read > high:
        p["days"][day] = int(p["days"].get(day, 0)) + (read - high)
        p["high"] = read
    p["days"] = _prune_days(p["days"])
    if int(p["high"] or 0) >= total:
        p["done_at"] = now
        p["high"] = total
        p.setdefault("history", []).append(_snapshot(p, now))
        p["history"] = p["history"][-MAX_HISTORY:]
    return p


def clear_plan(store, bid):
    return store.get("plans", {}).pop(bid, None) is not None


def _prune_days(days):
    if len(days) <= 400:
        return dict(days)
    keep = sorted(days.keys())[-400:]
    return {k: days[k] for k in keep}


def _snapshot(p, now):
    d0 = _parse_day(p.get("started") or "")
    d1 = _day_from_epoch(now)
    try:
        took = max(1, (_parse_day(d1) - d0).days + 1) if d0 else 1
    except Exception:
        took = 1
    return {"started": p.get("started") or "", "done_at": now,
            "pages": int(p.get("pages") or 1), "goal": dict(p.get("goal") or {}),
            "days": took}


# ─────────────────────────── 读出一条计划的样子 ───────────────────────────

def view(store, bid, total=None, now=None, today=None):
    """把一本书的计划算成界面直接能显示的样子；没有计划返回 {"has": False}。"""
    p = store.get("plans", {}).get(bid)
    if not p:
        return {"has": False}
    now = int(now if now is not None else time.time())
    today = today or _day_from_epoch(now)
    pages = max(1, _int(total if total is not None else p.get("pages"), 1, 10000000))
    read = max(0, min(pages, int(p.get("high") or 0)))
    g = p.get("goal") or {"period": "daily", "pages": 1}
    td = _parse_day(today) or date.today()
    # 今日 / 本周的账
    today_pages = int(p.get("days", {}).get(today, 0))
    if g["period"] == "weekly":
        ws = _week_start(td)
        period_pages = sum(int(v) for k, v in p.get("days", {}).items()
                           if (d := _parse_day(k)) and _week_start(d) == ws)
    else:
        period_pages = today_pages
    out = {
        "has": True,
        "title": p.get("title") or "",
        "pages": pages,
        "read": read,
        "left": max(0, pages - read),
        "pct": int(round(read * 100.0 / pages)) if pages else 0,
        "goal": dict(g),
        "started": p.get("started") or "",
        "target": p.get("target") or "",
        "today": today_pages,
        "period": period_pages,
        "hit": period_pages >= int(g["pages"]),
        "last_at": int(p.get("last_at") or 0),
        "done_at": int(p.get("done_at") or 0),
        "history": [dict(h) for h in (p.get("history") or [])][-MAX_HISTORY:],
    }
    _fill_pace(out, p, td, read, pages)
    return out


def _fill_pace(out, p, td, read, pages):
    """按「已经读的 ÷ 已经过的天数」推读完的日子，和终点日比一比在不在轨上。"""
    d0 = _parse_day(p.get("started") or "")
    out["elapsed"] = max(1, (td - d0).days + 1) if d0 else 1
    if out["done_at"]:
        out["pace"] = None
        out["eta"] = ""
        out["on_pace"] = True
        d1 = _parse_day(_day_from_epoch(out["done_at"]))
        out["took"] = max(1, (d1 - d0).days + 1) if (d0 and d1) else 1
        return
    out["took"] = 0
    if read <= 0:
        out["pace"] = None
    else:
        out["pace"] = round(read / float(out["elapsed"]), 1)
    left = out["left"]
    target = _parse_day(p.get("target") or "")
    if out["pace"] and out["pace"] > 0 and left > 0:
        out["eta"] = _add_days(td, int(math.ceil(left / out["pace"]))).strftime("%Y-%m-%d")
    else:
        out["eta"] = ""
    # 在不在轨上：有终点日就和终点日比，没有就只看「今天 / 本周达标没有」
    if left <= 0:
        out["on_pace"] = True
    elif target:
        out["days_left"] = max(0, (target - td).days)
        need = left / float(out["days_left"]) if out["days_left"] > 0 else None
        out["need_pace"] = round(need, 1) if need is not None else None
        out["on_pace"] = bool(need is not None and out["pace"] is not None and out["pace"] >= need)
    else:
        out["days_left"] = None
        out["need_pace"] = None
        out["on_pace"] = out["hit"]


def list_active(store, now=None):
    """所有还没读完的计划（个人页 / 侧栏要用得着时调）。"""
    out = []
    for bid in list(store.get("plans", {}).keys()):
        v = view(store, bid, now=now)
        if v.get("has") and not v.get("done_at"):
            out.append(dict(v, book=bid))
    out.sort(key=lambda x: x.get("last_at") or 0, reverse=True)
    return out
