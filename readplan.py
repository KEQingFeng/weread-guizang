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
# 一本书的页数上限。账本里每个 pages / high 读数都按这一把尺子收口
# （`_int(..., 1, PAGE_MAX)`）—— 原先这个数在六处各写一遍，而"章字数"那一档
# 一个上界都没有，见 `_chars_of`。
PAGE_MAX = 10000000
# 单章字数上限 = 页数上限 × 每页字数。章的 chars 是从书目录读回来的数（手改、
# 云同步写回、别的调用方自己拼 chapter dict 都可能给），一个 `1` 后面跟四百个 0
# 是合法 JSON、读回来是 Python 大整数，而 `chars / float(PAGE_CHARS)` 对这种
# "大到 float 装不下" 的整数当场 OverflowError —— 与 activity / login / mindmap
# 那几处越界数字同一条口径：越界的按上限收口，正常字数一个字不改。
MAX_CHARS = PAGE_MAX * PAGE_CHARS
KINDS = ("daily", "weekly")
_DAY_RE = re.compile(r"\d{4}-\d{2}-\d{2}\Z")
MAX_HISTORY = 40


# ─────────────────────────── 页数与位置 ───────────────────────────

def _chars_of(c):
    """一章的字数收成 0..MAX_CHARS 的整数。

    形状只认"对象 + 能认成非负整数的 chars"，与 `_int` 同一把尺子：一长串数字
    （合法 JSON、读回来是 Python 大整数）、纯数字串、inf / NaN / 负数、半个对象
    都在这里收口。原先 `total_pages` / `pages_at` 是裸 `int((c or {}).get("chars"))`
    加一句宽 except —— 大整数本身收得下、宽 except 也不接，可下一步
    `chars / float(PAGE_CHARS)` 就抛 OverflowError，而那不是 ValueError 能兜的。
    """
    v = c.get("chars") if isinstance(c, dict) else None
    return _int(v, 0, MAX_CHARS)


def total_pages(chapters):
    """一本书的总页数：各章字符数之和折算，至少 1 页、至多 PAGE_MAX 页。"""
    chars = sum(_chars_of(c) for c in chapters or [])
    pages = max(1, int(math.ceil(chars / float(PAGE_CHARS))))
    return min(pages, PAGE_MAX)


def pages_at(chapters, at, frac=0.0):
    """「读到第 at 章（0 基）其中 frac 处」折成第几页（四舍五入）。

    还没动（第一章、章内 0）算 0 页 —— 否则「翻开一本书」就记了 1 页，今日的账会虚。
    at 越界（读到尾声时前端可能报越界值）一律当读完全书，返回总页数。
    at 认不出（不是数、NaN、半个对象）当第 0 章 —— 见下面收口那几行的说明。
    """
    chapters = chapters or []
    if not chapters:
        return 0
    # 章序号先按同一把尺子收口再比大小：它来自界面按心跳上报的读数，可能是
    # 字符串、NaN、半个对象。这里若直接 int() 会抛，路由那一层没兜（`frac`
    # 早就用 try 包住了，`record_pos` 存 `at` 时也用的是 `_int`），于是一发脏
    # 读数的表现是「连接被甩、计划停在原地」，而不是进度不退。认不出的值一律
    # 当第 0 章：宁可把这一跳当成没前进，也不能当成「读完了」。
    at = _int(at, 0, 1000000)
    if at >= len(chapters):
        return total_pages(chapters)
    try:
        frac = float(frac or 0.0)
    except Exception:
        frac = 0.0
    # `json` 认 `NaN` / `Infinity`（都是合法 JSON 字面量），`float("1e999")` 也回 inf。
    # 这类值过不了下面那句 min：`min(1.0, nan)` 在 Python 里回的是 1.0（nan 的比较
    # 一律为假，min 留着先到的那一个），于是「认不出的比例」被读成「读到章尾」——
    # 与上面 `at` 那条口径正好相反。最坏的一种：心跳报在读到的最后一章，NaN 一夹到
    # 章尾就凑满总页数，`record_pos` 当场把书判成读完、留档封存，用户只是翻着书，
    # 计划却自己结束了。与 login / mindmap / video_note 同一把尺子：不有限当没读到。
    if not math.isfinite(frac):
        frac = 0.0
    frac = max(0.0, min(1.0, frac))
    if at == 0 and frac <= 0:
        return 0
    # 末章章末就是全书末尾：这一处的口径必须与 total_pages 的向上取整对齐。总字数
    # 不是整千时（如 3100 字 → 4 页），一般位置用四舍五入，读到最后一个字只算
    # round(3.1)=3 页 < 4 页，于是「已读完」永远触发不了，底栏永远写着还剩 1 页。
    if at == len(chapters) - 1 and frac >= 1.0:
        return total_pages(chapters)
    before = sum(_chars_of(chapters[i]) for i in range(at))
    here = _chars_of(chapters[at])
    pages = (before + frac * here) / float(PAGE_CHARS)
    return min(max(1, int(round(pages))), PAGE_MAX)


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
    """把一个账本里的数收成整数：能认的照认，认不出的退回 lo。

    形状只认「整数」和「纯数字串」两种。裸 `int(v)` 本来就把普通数字串收得下
    （`int("300")` 是对的），真正的口子在**超大数字串**那一格：账本是手能改的文本
    文件（也可能被云同步写回旧版本落过的形状），同一份读数写成数字还是带引号的
    字符串都是合法 JSON；`1000…0` 与 `"1000…0"` 本该读成同一个数，可 Python
    3.11+ 给「十进制串转 int」设了默认上限（4300 位），越线时 `int(v)` 抛
    ValueError，旧的 `except Exception` 只有 `n = lo` 一条路 —— 于是数字写法被
    hi 夹成大数、字符串写法读成 lo，同值的两种写法读成两个数。

    所以这里把纯数字串的长度先量一遍：越线的本该是个大数，直接按 hi 收口（没有
    hi 就给 lo 与其它认不出的值同命），与数字支落在同一个终点。

    这里只收「读数不一致」，不是修一个崩：旧的 `except Exception` 很宽，抛不出来，
    症状就是盘上两种等价写法读成两个数。

    只认纯数字串、不认 `"3.5"` / `"1e5"` 这类写法，是刻意保持的窄口径：
    `pages_at()` 的章序号、`load()` 的每天页数、色阶那类读数都走这一把尺子，而
    章序号出现小数本身就是「认不出」（半章不是一章）。宽容只会把一个认不出的值
    折成一个看起来正常的数 —— 那比退回 lo 更难查。
    """
    try:
        n = int(v)
    except Exception:
        # 认不出的形状（None / 半个对象 / 坏字符）也走这里：文本化还是认不出来，退回 lo。
        try:
            text = str(v).strip()
            if not text.isdigit():
                n = lo
            elif len(text) > 4000:
                n = hi if hi is not None else lo
            else:
                n = int(text)
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
                "pages": max(1, _int(h.get("pages"), 1, PAGE_MAX)),
                "goal": _norm_goal(h.get("goal")),
                "days": _int(h.get("days"), 0, 100000),
            })
    return {
        "title": str(v.get("title") or "")[:120],
        "pages": max(1, _int(v.get("pages"), 1, PAGE_MAX)),
        "goal": _norm_goal(v.get("goal")),
        "started": started,
        "target": str(v.get("target") or "") if _DAY_RE.match(str(v.get("target") or "")) else "",
        "at": _int(v.get("at"), 0, 1000000),
        "frac": _clamp01(v.get("frac")),
        "high": _int(v.get("high"), 0, PAGE_MAX),
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
    # 与 pages_at 同一个坑：盘上那份账本可能被手改 / 云同步写回一个 `NaN`，而
    # `min(1.0, nan)` 回的是 1.0 —— 一条脏读数落进 `frac` 就等于「这一章读到头了」，
    # 高水位与今日的账一起虚增。认不出的当 0（没读到），不拿它冒充进度。
    if not math.isfinite(f):
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


def _add_days_str(d, n):
    """d 往后 n 天的日期串；超出日期能表示的范围（`date.max` 是 9999 年）就回空串。

    推算出来的终点（`suggest_target` / `_fill_pace` 的 ETA）只是界面上一句参考。
    `pages` 已被 `PAGE_MAX` 收口，但「只读了 1 页、还剩一千万页」这种账推出来仍是
    几千万天，`date + timedelta` 当场 `OverflowError` —— 它不在 ValueError 里。
    与 started / target 认不出同一格：算不出来就不写终点，不把整条读计划带崩。
    """
    try:
        return _add_days(d, n).strftime("%Y-%m-%d")
    except (OverflowError, ValueError):
        return ""


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
        "pages": max(1, _int(total, 1, PAGE_MAX)),
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
    periods = max(1, int(math.ceil(max(1, _int(total, 1, PAGE_MAX)) / float(amount))))
    days = periods if (goal or {}).get("period") == "daily" else periods * 7
    return _add_days_str(d, days)


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
    pages = max(1, _int(total if total is not None else p.get("pages"), 1, PAGE_MAX))
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
        out["eta"] = _add_days_str(td, int(math.ceil(left / out["pace"])))
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
