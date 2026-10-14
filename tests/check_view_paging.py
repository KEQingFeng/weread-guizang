#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""订阅 / 便签的翻页参数收口：limit / offset 写脏不再把整页读成错误。

查询串里的数字早就有唯一的收口 `_qint`（tests/check_query_args.py 立下的规矩），
但那条门禁只扫 `do_GET` 的方法体。`feed_view` / `flomo_view` 这两个视图函数在
`do_GET` 之外，各自还留着老写法：

    /api/feed?mode=entries&limit=abc
    /api/flomo/notes?mode=list&limit=abc

两处都是裸 `int(...)`。它们外面各有一个 `except Exception`，所以症状不是
check_query_args 里那种「连接被掐断」，而是更隐蔽的一种：**整页从列表变成一条
报错** —— 前端拿到的是

    {"ok": false, "msg": "读订阅出错：invalid literal for int() with base 10: 'abc'"}

于是「手改过一次地址」或者「别处拼错的链接」会让订阅、便签这两屏一条都铺不出来，
而它本该像 `?tail=abc` 那样「当没传、回默认」。同一份文件里的 `video_view` 早就用
`_vnum` 收口了，这两处是漏网的。

修法：两处的 limit / offset 改走 `_qint`，默认值与夹取边界一个字不动，只把
「认不出」的那一档从抛异常改成回默认。

这一条钉三层：
  A 纯行为：脏值 → 回默认且接口 ok:true；好值 / 越界值 → 口径与改前一模一样
    （好值原样、上限 500、下限 1、offset 不小于 0）；
  B 源码：全文件（除 `_qint` 自己那段把老写法当例子的说明）不许再有裸
    `int(q.get(...))` / `int(one(...))`，以后新加视图函数写回老形状会当场红；
    另附一条对照，证明这组断言不是空转；
  C 真机：自己起一个 in-process `ThreadingHTTPServer` + `ui_server.Handler`，
    拿脏 limit / offset 各打一条裸 HTTP GET，必须是 200 + ok:true 的完整 JSON。

不联网、不起浏览器、数据目录指进自己的临时沙盒，归静态段，--fast 也得带上。
"""
import atexit
import json
import os
import pathlib
import re
import shutil
import sys
import tempfile
import threading
import time
import urllib.request
from http.server import ThreadingHTTPServer

HERE = pathlib.Path(__file__).resolve().parent
REPO = HERE.parent
sys.path.insert(0, str(REPO))

# 自带一个沙盒，绝不碰用户真实的 cache/ 与书库，也不和别的套件共用那份共享沙盒
# （这一条要真读一遍订阅 / 便签的账本，别家的夹具不该影响它的结论）。
SB = pathlib.Path(tempfile.mkdtemp(prefix="gz-view-paging-"))
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


# ── A 纯行为：脏值回默认、好值与越界口径不变 ─────────────────────
# 把下游换成「只记参数、不真查库」的桩，这样量的就是 feed_view / flomo_view 自己
# 传出去的 limit / offset，不受账本里有没有数据影响。
seen = {}


def _fake_entries(**kw):
    seen.clear()
    seen.update(kw)
    return []


orig_entries = ui_server.feed_mod.entries
orig_count = ui_server.feed_mod.count_entries
ui_server.feed_mod.entries = _fake_entries
ui_server.feed_mod.count_entries = lambda **kw: 0


def feed_q(**kw):
    q = {"mode": ["entries"]}
    for k, v in kw.items():
        q[k] = [v]
    return q


def feed_seen(**kw):
    r = ui_server.feed_view(feed_q(**kw))
    return r, seen


# 症状本体：脏 limit / offset 让整页 ok:false。修好后必须 ok:true 且落回默认。
r, s = feed_seen(limit="abc")
chk("feed ?limit=abc：不再是 ok:false 的报错页，回默认 200",
    r.get("ok") is True and s.get("limit") == 200 and "entries" in r,
    (r.get("ok"), s.get("limit"), r.get("msg")))
r, s = feed_seen(offset="abc")
chk("feed ?offset=abc：回默认 0，页面照铺",
    r.get("ok") is True and s.get("offset") == 0, (r.get("ok"), s.get("offset")))
r, s = feed_seen(limit="")
chk("feed ?limit=（空值）：回默认 200",
    r.get("ok") is True and s.get("limit") == 200, s.get("limit"))
r, s = feed_seen(limit="1e3")
chk("feed ?limit=1e3：回默认 200（不是 1000，更不是当场抛）",
    r.get("ok") is True and s.get("limit") == 200, s.get("limit"))
r, s = feed_seen(limit="3.7")
chk("feed ?limit=3.7：回默认 200（不截成 3）",
    r.get("ok") is True and s.get("limit") == 200, s.get("limit"))
r, s = feed_seen(limit="inf")
chk("feed ?limit=inf：回默认 200", r.get("ok") is True and s.get("limit") == 200, s.get("limit"))

# 好值一点不许改口径。
r, s = feed_seen(limit="7", offset="3")
chk("feed ?limit=7&offset=3：原样收下",
    r.get("ok") is True and s.get("limit") == 7 and s.get("offset") == 3,
    (s.get("limit"), s.get("offset")))
r, s = feed_seen(limit="999999")
chk("feed ?limit=999999：仍夹在上限 500", s.get("limit") == 500, s.get("limit"))
r, s = feed_seen(limit="0")
chk("feed ?limit=0：仍夹在下限 1", s.get("limit") == 1, s.get("limit"))
r, s = feed_seen(limit="-9")
chk("feed ?limit=-9：仍夹在下限 1", s.get("limit") == 1, s.get("limit"))
r, s = feed_seen(offset="-5")
chk("feed ?offset=-5：仍夹在 0", s.get("offset") == 0, s.get("offset"))
r, s = feed_seen()
chk("feed 不传：默认还是 limit=200 / offset=0",
    s.get("limit") == 200 and s.get("offset") == 0, (s.get("limit"), s.get("offset")))

seen2 = {}


def _fake_pick(data, **kw):
    seen2.clear()
    seen2.update(kw)
    return {"memos": [], "total": 0, "offset": kw.get("offset", 0), "count": 0}


orig_pick = ui_server.flomo_notes.pick
ui_server.flomo_notes.pick = _fake_pick


def flomo_seen(**kw):
    q = {"mode": ["list"]}
    for k, v in kw.items():
        q[k] = [v]
    r = ui_server.flomo_view(q)
    return r, seen2


r, s = flomo_seen(limit="abc")
chk("flomo ?limit=abc：不再是 ok:false 的报错页，回默认 80",
    r.get("ok") is True and s.get("limit") == 80 and "memos" in r,
    (r.get("ok"), s.get("limit"), r.get("msg")))
r, s = flomo_seen(offset="abc")
chk("flomo ?offset=abc：回默认 0，页面照铺",
    r.get("ok") is True and s.get("offset") == 0, (r.get("ok"), s.get("offset")))
r, s = flomo_seen(limit="3", offset="2")
chk("flomo ?limit=3&offset=2：原样收下",
    r.get("ok") is True and s.get("limit") == 3 and s.get("offset") == 2,
    (s.get("limit"), s.get("offset")))
r, s = flomo_seen(limit="999999")
chk("flomo ?limit=999999：仍夹在上限 500", s.get("limit") == 500, s.get("limit"))
r, s = flomo_seen(limit="0")
chk("flomo ?limit=0：仍夹在下限 1", s.get("limit") == 1, s.get("limit"))
r, s = flomo_seen(offset="-5")
chk("flomo ?offset=-5：仍夹在 0", s.get("offset") == 0, s.get("offset"))
r, s = flomo_seen(limit="1e3")
chk("flomo ?limit=1e3：回默认 80", r.get("ok") is True and s.get("limit") == 80, s.get("limit"))

# 桩用完就还回去，C 段要在真下游上跑。
ui_server.feed_mod.entries = orig_entries
ui_server.feed_mod.count_entries = orig_count
ui_server.flomo_notes.pick = orig_pick

# ── B 源码：全文件不许再有裸 int(q.get(...)) / int(one(...)) ─────
# `_qint` 自己的 docstring 里就写着 `int(q.get("tail")[0])` 当反面例子，扫描前先摘掉
# 那一段，免得门禁把说明文字当成代码。
QINT_DEF = SERVER[SERVER.index("def _qint("):SERVER.index("def _body_len(")]
SCAN = SERVER.replace(QINT_DEF, "")
bare_q = re.findall(r"(?<![A-Za-z0-9_])int\(\s*q\.get\(", SCAN)
bare_one = re.findall(r"(?<![A-Za-z0-9_])int\(\s*one\(", SCAN)
chk("全文件不再有裸 int(q.get(...))（新视图写回老形状会当场红）", not bare_q, bare_q)
chk("全文件不再有裸 int(one(...))", not bare_one, bare_one)

FEEDVIEW = SERVER[SERVER.index("def feed_view(q):"):SERVER.index("def _feed_ids(")]
FLOMOVIEW = SERVER[SERVER.index("def flomo_view(q):"):SERVER.index("def flomo_do(body):")]
chk('feed_view 的 limit 走 _qint 且仍是 1..500',
    '_qint(q.get("limit", ["200"])[0], 200, lo=1, hi=500)' in FEEDVIEW,
    [l.strip() for l in FEEDVIEW.splitlines() if "limit" in l][:3])
chk('feed_view 的 offset 走 _qint 且不小于 0',
    '_qint(q.get("offset", ["0"])[0], 0, lo=0)' in FEEDVIEW,
    [l.strip() for l in FEEDVIEW.splitlines() if "offset" in l][:3])
chk('flomo_view 的 limit 走 _qint 且仍是 1..500',
    '_qint(one("limit", "80"), 80, lo=1, hi=500)' in FLOMOVIEW,
    [l.strip() for l in FLOMOVIEW.splitlines() if "limit" in l][:3])
chk('flomo_view 的 offset 走 _qint 且不小于 0',
    '_qint(one("offset", "0"), 0, lo=0)' in FLOMOVIEW,
    [l.strip() for l in FLOMOVIEW.splitlines() if "offset" in l][:3])

# 对照：老写法对同一个脏值确实会抛。没有这一条，上面那组「不抛」可能只是空转。
try:
    max(1, min(500, int("abc" or 200)))
    chk("对照：裸 int('abc') 确实会抛（这门禁不是空转）", False, "居然没抛")
except ValueError:
    chk("对照：裸 int('abc') 确实会抛（这门禁不是空转）", True)

# ── C 真机：in-process 起一个真 Handler，用裸 HTTP 发脏参数 ───────
srv = ThreadingHTTPServer(("127.0.0.1", 0), ui_server.Handler)
PORT = srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start()
time.sleep(0.1)


def raw_get(path):
    """回 (状态码, 解析后的 JSON)；读不到 / 解不出回 (None, 异常文本)。"""
    try:
        with urllib.request.urlopen("http://127.0.0.1:%d%s" % (PORT, path), timeout=5) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except Exception as e:                       # noqa: BLE001 —— 就是来找断连 / 非 JSON 的
        return None, "%s: %s" % (type(e).__name__, e)


st, body = raw_get("/api/feed?mode=entries&limit=abc")
chk("真机 /api/feed?limit=abc：200 + ok:true（原来是一条 ok:false 的报错页）",
    st == 200 and isinstance(body, dict) and body.get("ok") is True, (st, str(body)[:160]))

st, body = raw_get("/api/feed?mode=entries&offset=abc&limit=7")
chk("真机 /api/feed?offset=abc&limit=7：200 + ok:true，好值仍被认",
    st == 200 and isinstance(body, dict) and body.get("ok") is True, (st, str(body)[:160]))

st, body = raw_get("/api/flomo/notes?mode=list&limit=abc")
chk("真机 /api/flomo/notes?limit=abc：200 + ok:true（原来是一条 ok:false 的报错页）",
    st == 200 and isinstance(body, dict) and body.get("ok") is True, (st, str(body)[:160]))

st, body = raw_get("/api/flomo/notes?mode=list&offset=abc")
chk("真机 /api/flomo/notes?offset=abc：200 + ok:true",
    st == 200 and isinstance(body, dict) and body.get("ok") is True, (st, str(body)[:160]))

st, body = raw_get("/api/feed?mode=entries&limit=7&offset=1")
chk("真机 /api/feed 正常翻页：200 + ok:true（这一改没有误伤好值）",
    st == 200 and isinstance(body, dict) and body.get("ok") is True, (st, str(body)[:160]))

srv.shutdown()
srv.server_close()

# ── 汇总 ────────────────────────────────────────────────────────
bad = [n for ok, n, _ in checks if not ok]
print()
print("订阅 / 便签翻页参数收口体检：通过 %d 项，失败 %d 项" % (len(checks) - len(bad), len(bad)))
for n in bad:
    print("  ✗ " + n)
sys.exit(1 if bad else 0)
