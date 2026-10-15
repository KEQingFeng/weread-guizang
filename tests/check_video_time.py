#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""视频书 meta 里的越界时间不许把「视频工作台」这一屏甩成空白。

为什么会有这种数字
    `meta.json` 里 `updated_at` 记的是「这本书上次动过是什么时候」，本进程写它时是
    `int(time.time())`。但这份档不只有本进程在写：用户手工编辑、云同步从别处落盘、
    旧版本写回，都可能让它变成别的形状。而 `json` 认 `Infinity` / `-Infinity` /
    `NaN` 这三个字面量，也认 `1e400`（读回来就是 inf）—— 都是合法 JSON。

实测的故障（改之前）
    `books/视频/<某本>/meta.json` 里 `"updated_at": 1e400` 之后：

        GET /api/video?mode=books
          → 回包正文里是 `"updated_at": Infinity`。Python 的 `json.dumps` 默认
            `allow_nan=True`，照写不误；可这不是 JSON 规范里的值，浏览器
            `JSON.parse` 碰到它整份抛 —— 用户看到的是视频工作台这一屏整个空白，
            不是「某本书排序差一点」。同一栏里 `duration` / `words` / `chapters` /
            `page` / `pages` 早都过了 `_vnum` 那把尺子（越界归 0），唯独
            `updated_at` 还是裸 `float(...)` + 只兜异常：`float("1e400")` 回 inf
            不抛，`float("nan")` 回 nan 也不抛，两个都从 try 底下漏了过去。

收口
    `updated_at` 改走同一把尺子 `_vnum`：认不出的（含 NaN / ±inf）归 0，
    认得出的整数照认。本条钉三面 —— 纯函数出的值必须是有限整数、真机回包必须是
    严格 JSON（`parse_constant` 一见到 `Infinity` / `NaN` 就判失败）、以及
    源码里不再有那条裸 `float(...)` 的接线。另留一面「旧写法确实会写出 Infinity」
    的对照，防门禁空转。

跑法：纯函数 + 源码走查 + 一个进程内真 Handler 的裸 HTTP，自带临时沙盒，
不联网、不开浏览器、不起外部服务。归 --fast 静态段。
"""
import atexit
import http.server
import json
import math
import os
import pathlib
import shutil
import sys
import tempfile
import threading
import urllib.error
import urllib.request

HERE = pathlib.Path(__file__).resolve().parent
REPO = HERE.parent
sys.path.insert(0, str(REPO))

# 自带一个沙盒，绝不碰用户真实的 cache/ 与书库，也不和别的套件共用那份共享沙盒。
SB = pathlib.Path(tempfile.mkdtemp(prefix="gz-video-time-"))
atexit.register(lambda: shutil.rmtree(SB, ignore_errors=True))
for _d in ("data", "books", "shots"):
    (SB / _d).mkdir(parents=True, exist_ok=True)
os.environ["GUIZANG_DATA"] = str(SB / "data")
os.environ["GUIZANG_BOOKS"] = str(SB / "books")
os.environ["GUIZANG_SHOT_DIR"] = str(SB / "shots")
os.environ["GUIZANG_ANNA_NO_WINDOW"] = "1"

import ui_server  # noqa: E402  —— 必须在环境变量设好之后 import

SERVER = (REPO / "ui_server.py").read_text(encoding="utf-8")

checks = []


def chk(name, ok, extra=""):
    checks.append((bool(ok), name, "" if ok else str(extra)[:300]))
    print(("  ok  " if ok else " FAIL ") + name + ("" if ok else "  <- " + str(extra)[:300]))


def strict_json(text):
    """严格 JSON 解析：`Infinity` / `NaN` 这类 Python 放行、浏览器拒收的值当场判失败。

    `json.loads` 默认收 `Infinity` / `-Infinity` / `NaN`，所以光靠它验不出这条 bug ——
    必须把 `parse_constant` 揪出来看一眼，正是它对应浏览器 `JSON.parse` 那一拒。
    """
    def boom(const):
        raise ValueError("回包里出现 JSON 规范不收的 " + const)
    return json.loads(text, parse_constant=boom)


# ── A 纯函数：视频书清单里的 updated_at 必须是有限整数 ─────────────
BOOKS = pathlib.Path(ui_server.OUT_DIR)
MOD = "视频"


def put(book_id, raw):
    d = BOOKS / MOD / book_id
    d.mkdir(parents=True, exist_ok=True)
    (d / "meta.json").write_text(raw, encoding="utf-8")
    return d


# 三条：越界时间（1e400 → inf）、NaN、正常时间。写成字面量而不是 json.dump ——
# 手改 / 云同步落盘的账本正是这个形状，json.load 读回来就是 inf / nan。
put("video_inf01", '{"source":"video","title":"越界时间","updated_at":1e400}')
put("video_nan01", '{"source":"video","title":"NaN 时间","updated_at":NaN}')
put("video_good01", '{"source":"video","title":"好时间","updated_at":1700000000}')

try:
    rows = ui_server.video_books()
    vok = True
except Exception as e:                                    # noqa: BLE001
    rows, vok = [], False
    print("      video_books 抛了：" + type(e).__name__)
chk("三条视频书都列得出来（坏时间不带走整栏）", vok and len(rows) == 3, rows)

by_id = {r.get("id"): r for r in rows} if vok else {}
for bid in ("video_inf01", "video_nan01", "video_good01"):
    one = by_id.get(bid) or {}
    v = one.get("updated_at")
    chk("%s 的 updated_at 是普通 int（不是 bool / float / inf / nan）" % bid,
        isinstance(v, int) and not isinstance(v, bool), repr(v))
    chk("%s 的 updated_at 是有限值" % bid,
        isinstance(v, (int, float)) and math.isfinite(v), repr(v))
chk("越界时间归 0（inf / nan 都不是「很久以前」那种能排序的数）",
    (by_id.get("video_inf01") or {}).get("updated_at") == 0
    and (by_id.get("video_nan01") or {}).get("updated_at") == 0,
    [(k, (by_id.get(k) or {}).get("updated_at")) for k in ("video_inf01", "video_nan01")])
chk("认得出的时间照认（好那本一个数不改）",
    (by_id.get("video_good01") or {}).get("updated_at") == 1700000000,
    (by_id.get("video_good01") or {}).get("updated_at"))

# 整份清单真的写得成严格 JSON，且排序不会因为坏值抛。
try:
    text = json.dumps(rows, ensure_ascii=False)
    strict_json(text)
    chk("video_books() 整份能写成严格 JSON（没有 Infinity / NaN）", True)
except Exception as e:                                    # noqa: BLE001
    chk("video_books() 整份能写成严格 JSON（没有 Infinity / NaN）", False,
        "%s: %s" % (type(e).__name__, e))

# 对照：旧写法（裸 float + 只兜异常）遇到 1e400 确实写出 Infinity —— 这组断言不是空转。
old_upd = float("1e400")
old_text = json.dumps({"updated_at": old_upd})
chk("对照：旧写法 float('1e400') 回 inf、json.dumps 确实写出 Infinity",
    old_upd == float("inf") and "Infinity" in old_text, old_text)

# ── B 源码：updated_at 与同栏其它数字走同一把尺子 ───────────────────
chk("updated_at 过 _vnum（不再裸 float）",
    '_vnum(m.get("updated_at"))' in SERVER,
    [l.strip() for l in SERVER.splitlines() if "updated_at" in l and "m.get" in l][:3])
chk("源码里不再有裸 'float(m.get(\"updated_at\")' 的旧写法",
    'float(m.get("updated_at")' not in SERVER)

# ── C 真机：/api/video?mode=books 必须回严格 JSON ──────────────────
srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), ui_server.Handler)
PORT = srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start()
atexit.register(srv.shutdown)
BASE = "http://127.0.0.1:%d" % PORT


def call(path, timeout=15):
    """→ (status, body_text)。status 为 'ERR' 表示连接层就失败了（服务断了）。"""
    try:
        r = urllib.request.urlopen(BASE + path, timeout=timeout)
        return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except Exception as e:                                # noqa: BLE001
        return "ERR", "%s: %s" % (type(e).__name__, e)


st, body = call("/api/video?mode=books")
if st == "ERR":
    chk("真机：GET /api/video?mode=books 回 200（不是连接被甩）", False, body[:200])
elif st != 200:
    chk("真机：GET /api/video?mode=books 回 200（不是连接被甩）", False,
        "HTTP %s：%r" % (st, body[:200]))
else:
    chk("真机：GET /api/video?mode=books 回 200（不是连接被甩）", True)
    try:
        obj = strict_json(body)
        chk("真机：回包是严格 JSON（浏览器 JSON.parse 不会整份失败）", True)
    except Exception as e:                                # noqa: BLE001
        obj = None
        chk("真机：回包是严格 JSON（浏览器 JSON.parse 不会整份失败）", False,
            "%s：%r" % (e, body[:200]))
    if isinstance(obj, dict):
        books = obj.get("books") or []
        chk("真机：三本视频书都在回包里", len(books) == 3, books)
        bad = [(b.get("id"), b.get("updated_at")) for b in books
               if not (isinstance(b.get("updated_at"), int)
                       and not isinstance(b.get("updated_at"), bool))]
        chk("真机：每本的 updated_at 都是普通 int", not bad, bad)

# 干净数据那条大路照旧 200 + 严格 JSON。
st, body = call("/api/state")
if st == 200:
    try:
        strict_json(body)
        chk("干净数据回归：/api/state 仍回 200 + 严格 JSON", True)
    except Exception as e:                                # noqa: BLE001
        chk("干净数据回归：/api/state 仍回 200 + 严格 JSON", False,
            "%s：%r" % (e, body[:200]))
else:
    chk("干净数据回归：/api/state 仍回 200 + 严格 JSON", False,
        "HTTP %s：%r" % (st, body[:200]))

bad = [c for c in checks if not c[0]]
print("\n视频书越界时间体检：%d 项通过，失败 %d 项" % (len(checks) - len(bad), len(bad)))
if bad:
    for _ok, n, x in bad:
        print("  ✗ " + n + ("  | " + x if x else ""))
sys.exit(1 if bad else 0)
