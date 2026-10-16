#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""油管静态目录里那两个「外站给的数」不许把整屏打崩（时长 / 缩略图宽）。

`_yt_len` 读 `ytInitialPlayerResponse.videoDetails.lengthSeconds`，`_yt_img` 读缩略图
`sources[].width`。这两个数都是**外站页面自带 JSON** 里的，浏览器那边怎么写我们管不着：
`json.loads` 认 `Infinity` / `NaN` / `1e400`（后两个解出来就是 inf / nan），也认一长串
数字（解出来是 Python 大整数）。而

    int(float("1e400"))   # inf             -> OverflowError
    int(float(10**400))   # 大整数转 float 也 overflow
    float(10**400)        # -> OverflowError

`OverflowError` **不在** `ValueError` 里。原先两处都只 `except (TypeError, ValueError)`：
`_yt_img` 写 `int(s.get("width") or 0)`、`_yt_len` 写 `int(float(sec))`，于是一个
`"width": 1e400` 或 `"lengthSeconds": Infinity` 就让 `yt_static` → `shape` 整条路抛出去。
`/api/browse` 那条路由（`ui_server.do_GET`）拿了结果只判 `ok`、外面没有第二道 try，
`BaseHTTPRequestHandler` 收到未捕获异常就是把连接关掉 —— iframe 收到的是一页空白，
用户在界面上根本看不到「这一页没取回来」那句人话（同一类症状见 check_browse_url.py）。

修法与 login / readplan / mindmap 那几处越界收口同一把尺子：不有限、认不出、以及比
`_YT_LEN_MAX` 还长的读数一律当「不知道」，卡片照常铺。这一条全程离线，不起服务、
不联网：合成的 ytInitialData / ytInitialPlayerResponse 直接喂给 `shape`。

  A  `_yt_num`   —— 一个读数收成非负整数：有限才认，inf / NaN / 1e400 / 大整数 / 怪形状归 0。
  B  `_yt_len`   —— 正常时长照旧；脏读数回空串，绝不抛。
  C  `_yt_img`   —— 脏宽度归 0、不抛，正常值仍然挑最宽的那张。
  D  对照        —— 裸 `int(float(…))` 确实抛 OverflowError（这条门禁不是空转）。
  E  `shape`     —— 一页带脏读数的油管页仍然铺得出来，卡片/主角都在。
  F  正常页      —— 干净的数字一个字不改（时长照印、最宽的缩略图照挑）。

跑在旧写法（`_yt_num` 还没抽出来）上时，A 那一组如实报「没这个函数」，
B–F 继续跑，把真正的病根 —— `_yt_len` / `_yt_img` / `shape` 抛 OverflowError —— 一条条列出来。
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import web_browse as wb  # noqa: E402

# 时长上限：写死在这里一份，好让这条门禁在「模块还没定义它」的旧写法上也能跑到底。
LEN_MAX = getattr(wb, "_YT_LEN_MAX", 366 * 24 * 3600)

checks = []


def chk(name, ok, extra=""):
    checks.append((bool(ok), name, "" if ok else str(extra)[:300]))
    print(("  ok  " if ok else " FAIL ") + name + ("" if ok else "  <- " + str(extra)[:300]))


def _call(fn, default=None):
    """调一下被测函数；抛了就把异常写成可读的一串 —— 门禁要的是「它抛了」，不是自己崩掉。"""
    try:
        return fn()
    except Exception as e:                                   # noqa: BLE001
        return "RAISED %s: %s" % (type(e).__name__, e)


# ── A 一个读数的尺子 ─────────────────────────────────────────────
DIRTY = [float("inf"), float("-inf"), float("nan"), "1e400", "Infinity", "NaN",
         10 ** 400, -(10 ** 400), "abc", None, {}, [], "   ", "-1"]
if not hasattr(wb, "_yt_num"):
    chk("web_browse._yt_num 存在（把「一个读数收成非负整数」抽成一把尺子）", False,
        "模块里没有 _yt_num")
else:
    for v in DIRTY:
        got = _call(lambda v=v: wb._yt_num(v))
        chk("读不出数的形状当 0（%s）" % str(v)[:24], got == 0, got)

    for v, want in ((0, 0), (5, 5), (400, 400), ("336", 336), (3.9, 3), (True, 0)):
        got = _call(lambda v=v: wb._yt_num(v))
        chk("正常读数照收（%r → %r）" % (v, want), got == want, got)

# ── B 时长 ───────────────────────────────────────────────────────
for v, want in ((0, ""), (None, ""), ("", ""), (212, "3:32"), (3661, "1:01:01"),
                (59, "0:59"), ("212", "3:32")):
    got = _call(lambda v=v: wb._yt_len(v))
    chk("正常时长照旧（%r → %r）" % (v, want), got == want, got)

for v in DIRTY + [LEN_MAX + 1, 10 ** 9]:
    got = _call(lambda v=v: wb._yt_len(v))
    chk("脏 / 越界时长回空串且不抛（%s）" % str(v)[:24], got == "", got)

# 一年以内是真的、超过一年当认不出；边界两侧各钉一条。
chk("恰好一年整是合法的", _call(lambda: wb._yt_len(LEN_MAX)) != "",
    _call(lambda: wb._yt_len(LEN_MAX)))
chk("一年再多一秒回空串", _call(lambda: wb._yt_len(LEN_MAX + 1)) == "",
    _call(lambda: wb._yt_len(LEN_MAX + 1)))

# ── C 缩略图 ─────────────────────────────────────────────────────
HI = "https://i.ytimg.com/vi/x/maxresdefault.jpg"
MID = "https://i.ytimg.com/vi/x/hqdefault.jpg"
LOW = "https://i.ytimg.com/vi/x/default.jpg"
EVIL = "https://evil.example/wide.jpg"


def _img(sources):
    return _call(lambda: wb._yt_img(sources))


chk("脏宽度不抛，正常值照挑最宽的那张",
    _img([{"url": LOW, "width": 168},
          {"url": HI, "width": float("inf")},
          {"url": MID, "width": 336}]) == MID,
    _img([{"url": LOW, "width": 168}, {"url": HI, "width": float("inf")},
          {"url": MID, "width": 336}]))
chk("宽度全是脏值时仍选第一个合法图床",
    _img([{"url": LOW, "width": float("inf")},
          {"url": MID, "width": "1e400"}]) == LOW,
    _img([{"url": LOW, "width": float("inf")},
          {"url": MID, "width": "1e400"}]))
chk("脏宽度不会把别家域名放进来",
    _img([{"url": EVIL, "width": float("inf")},
          {"url": LOW, "width": 168}]) == LOW,
    _img([{"url": EVIL, "width": float("inf")}, {"url": LOW, "width": 168}]))
chk("没有可用缩略图回空串", _img([{"url": EVIL, "width": float("inf")}]) == "")

# ── D 对照：旧写法确实会抛 ───────────────────────────────────────
def _raw_raises(fn):
    try:
        fn()
    except OverflowError:
        return True
    except Exception:                                        # noqa: BLE001
        return False
    return False


chk("对照：裸 int(float('1e400')) 抛 OverflowError",
    _raw_raises(lambda: int(float("1e400"))))
chk("对照：裸 float(10**400) 抛 OverflowError",
    _raw_raises(lambda: float(10 ** 400)))
chk("对照：OverflowError 不是 ValueError（原 except 抓不到它）",
    issubclass(OverflowError, ArithmeticError) and not issubclass(OverflowError, ValueError))


# ── E/F 端到端：合成的油管页 ─────────────────────────────────────
def _page(marker, payload, extra=""):
    return ("<!doctype html><html><head><meta charset=\"utf-8\"></head><body>"
            "<script>var %s = %s;%s</script></body></html>"
            % (marker, payload, extra)).encode("utf-8")


VIDEO = "dQw4w9WgXcQ"
WATCH = "https://www.youtube.com/watch?v=" + VIDEO


def _cards_page(lengths, widths):
    """一页 ytInitialData：两条 videoRenderer，时长/宽度由调用方给原始 JSON 片段。"""
    th = ",".join('{"url":"%s","width":%s}' % (u, w)
                  for u, w in zip((LOW, MID), widths))
    rows = ",".join(
        '{"videoRenderer":{"videoId":"%s","title":{"simpleText":"T%d"},'
        '"thumbnail":{"thumbnails":[%s]},"lengthText":{"simpleText":"%s"}}}'
        % (VIDEO, i, th, ln) for i, ln in enumerate(lengths))
    return _page("ytInitialData", '{"contents":{"rows":[%s]}}' % rows)


def _shape(blob):
    """走真实入口 shape()：油管那一站换成静态页；异常原样冒出来给门禁看。"""
    return _call(lambda: wb.shape(blob, "text/html", "utf-8", WATCH))


# 脏宽度：旧写法在这条上抛 OverflowError。
out = _shape(_cards_page(["3:32"], ['Infinity', "336"]))
body = out.get("body", b"") if isinstance(out, dict) else str(out).encode()
chk("shape：缩略图宽度是 Infinity 时那一页照铺（不甩连接）",
    isinstance(out, dict) and b'class="card' in body and MID.encode() in body, out)

# 脏时长（主角那一格）：旧写法在 _yt_len 上抛 OverflowError。
HERO = ('{"videoDetails":{"videoId":"%s","title":"主角","author":"频道",'
        '"lengthSeconds":%s,"thumbnail":{"thumbnails":[%s]}}}'
        % (VIDEO, "1e400", '{"url":"%s","width":Infinity}' % LOW))
out = _shape(_page("ytInitialPlayerResponse", HERO))
body = out.get("body", b"") if isinstance(out, dict) else str(out).encode()
chk("shape：主角时长是 1e400 时那一页照铺（不甩连接）",
    isinstance(out, dict) and b'class="card' in body and LOW.encode() in body, out)

# 正常页：干净的数字一个字不改 —— 时长印出来、最宽的那张缩略图胜出。
out = _shape(_cards_page(["3:32", "1:01:01"], ["168", "336"]))
body = out.get("body", b"") if isinstance(out, dict) else str(out).encode()
chk("shape：干净页的时长照印（3:32 / 1:01:01）",
    isinstance(out, dict) and "3:32".encode() in body and "1:01:01".encode() in body, out)
chk("shape：干净页挑最宽的那张缩略图（336 胜 168）",
    isinstance(out, dict) and MID.encode() in body, out)

bad = [c for c in checks if not c[0]]
print("\n油管静态目录脏读数门禁：%d 项通过，失败 %d 项" % (len(checks) - len(bad), len(bad)))
for _, n, x in bad:
    print("  ✗ " + n + ("  | " + x if x else ""))
sys.exit(1 if bad else 0)
