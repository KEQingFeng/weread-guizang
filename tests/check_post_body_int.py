#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""POST 请求体里「本该是数字」的字段，形状写脏了不许把 Python 报错原文甩到界面。

为什么单开一条：`_body_str`（字符串字段）/ `_body_list`（列表字段）都收口了，数字
字段还留着一档 —— 订阅整理那几个参数一直是裸的 `int(body["index"])`：

    POST /api/feed  {"act": "reorder",      "index": "abc"}
    POST /api/feed  {"act": "fetch_content","limit": {"a": 1}}
    POST /api/feed  {"act": "prune",        "days": [1], "max_entries": "x"}

`feed_do` 外面那一层 `except Exception` 会把这些接住，所以症状不是断连，而是
`str(e)[:180]` 把解释器的报错原样递回界面 —— 用户点一下「挪顺序」，弹出来的是
`invalid literal for int() with base 10: 'abc'`。`_body_str` / `_body_list` 立下的
口径是「字段级脏形状当没写」，数字字段也照同一条走：认不出回默认值，绝不抛、也
绝不把 Python 报错原文露出去。

与查询串那条 `_qint` 是**同一把尺子**：`_body_int` 直接调 `_qint`，所以字符串
"42" 认、小数 / `inf` / 半个对象 / 布尔都不认。`reorder` 的 index 是个例外：
认不出时当面回「没挪」，不能当成「index 没传」—— 那在 `feed.reorder` 里是「挪到
末尾」，等于替用户乱动。

这一条钉三层：
  A 纯函数：`_body_int` 的认值 / 夹取 / 默认值，并与 `_qint` 逐项等值（同一把尺子）；
  B 源码：四个调用点全部改用它，全文件不再有裸的 `int(body[...])` / `int(body.get(...))`；
  C 真机：三个口拿脏形状打过去，必须回完整的 200 + JSON（不是 0 字节、不是
    RemoteDisconnected、也不许夹着 `invalid literal` 这种报错原文），好值照旧走得通。

不起浏览器，归静态段，--fast 也得带上。
"""
import atexit
import json
import os
import pathlib
import shutil
import sys
import tempfile
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

HERE = pathlib.Path(__file__).resolve().parent
REPO = HERE.parent
sys.path.insert(0, str(REPO))

SB = pathlib.Path(tempfile.mkdtemp(prefix="gz-post-int-"))
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


# ── A 纯函数：认得出照认，认不出回默认，再按需夹住 ────────────────
_body_int = getattr(ui_server, "_body_int", None)
chk("字段级收口 _body_int 存在（唯一入口）", callable(_body_int))
if callable(_body_int):
    dirty = [None, "abc", "  ", "1.5", 1.5, True, False, [], ["3"], {"a": 1},
             ("x",), {1, 2}, b"3", float("nan"), float("inf"), object()]
    chk("认不出的形状一律回默认，不抛",
        all(_body_int({"k": v}, "k", -1) == -1 for v in dirty),
        [(repr(v), _body_int({"k": v}, "k", -1)) for v in dirty[:8]])
    chk("认不出的形状回的是调用方给的默认值",
        _body_int({"days": "abc"}, "days", 7) == 7)
    chk("没带这个键 / body 不是对象也回默认",
        _body_int({}, "index") is None and _body_int(None, "index", 9) == 9)
    chk("认得出的照认：数字与纯数字串",
        _body_int({"k": 42}, "k") == 42 and _body_int({"k": "42"}, "k") == 42
        and _body_int({"k": " -3 "}, "k") == -3)
    chk("lo / hi 把越界夹回边界（与 _qint 同口径）",
        _body_int({"k": 0}, "k", None, lo=1) == 1
        and _body_int({"k": 500}, "k", None, lo=1, hi=200) == 200
        and _body_int({"k": -5}, "k", None, lo=1) == 1)
    # 同一把尺子：对同一批输入，_body_int 与 _qint 必须逐项相同（布尔 / 小数 / inf 都算）。
    probes = ([None, "abc", "", " 7 ", 7, -7, 1.5, True, False, [], ["3"],
               {"a": 1}, float("inf"), float("nan"), 10 ** 40, "1e400", "42"]
              + [repr(v) for v in (None, 1.5, True)])
    same = all(
        (_body_int({"k": v}, "k", "D", lo=-3, hi=33)
         == ui_server._qint(v, "D", lo=-3, hi=33))
        for v in probes)
    chk("与查询串 _qint 逐项等值（同一把尺子）", same,
        [(repr(v), _body_int({"k": v}, "k", "D", lo=-3, hi=33),
          ui_server._qint(v, "D", lo=-3, hi=33)) for v in probes][:6])
    # 对照：老写法确实会抛 —— 这组断言不是空转。
    old_raised = False
    try:
        int(({"index": "abc"} or {}).get("index"))
    except (TypeError, ValueError):
        old_raised = True
    chk("对照：旧的 `int(body[\"index\"])` 遇到 'abc' 确实抛", old_raised)
    old_raised = False
    try:
        int(({"limit": {"a": 1}} or {}).get("limit") or 50)
    except TypeError:
        old_raised = True
    chk("对照：旧的 `int(body.get(\"limit\") or 50)` 遇到对象确实抛 TypeError", old_raised)

# ── B 源码：四个调用点全用它，且不再有裸的 int(body[...]) ─────────
# 这四个数字字段都在 `feed_do` 里（订阅管理这一路），不在 do_POST 那个大方法体里，
# 所以按函数切片，别只扫 do_POST。
_start = SERVER.index("def feed_do(body):")
_nxt = SERVER.index("\ndef ", _start + 1)
FEED_DO = SERVER[_start:_nxt]

chk("reorder 的 index 走 _body_int",
    '_body_int(body, "index", None, lo=0)' in FEED_DO,
    [l.strip() for l in FEED_DO.splitlines() if '"index"' in l][:3])
chk("fetch_content 的 limit 走 _body_int",
    '_body_int(body, "limit", 50, lo=1, hi=200)' in FEED_DO,
    [l.strip() for l in FEED_DO.splitlines() if '"limit"' in l][:3])
chk("prune 的 days / max_entries 走 _body_int",
    '_body_int(body, "days", None)' in FEED_DO
    and '_body_int(body, "max_entries", None)' in FEED_DO,
    [l.strip() for l in FEED_DO.splitlines() if '"days"' in l or '"max_entries"' in l][:4])

# 用 AST 认「真的在调用 int(...)」，而不是字符串里恰好写着这几个字（文档 / 注释里
# 会举反例，比如 `_body_int` 的说明）。判据：int(...) 这个调用的参数子树里出现了
# 名字 `body` —— 那一定是「把 body 字段直接 int()」，正是要绝迹的旧形状。
import ast  # noqa: E402

bare = []
for node in ast.walk(ast.parse(SERVER)):
    if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
            and node.func.id == "int" and node.args):
        continue
    if any(isinstance(n, ast.Name) and n.id == "body" for n in ast.walk(node.args[0])):
        bare.append("line %d: %s" % (node.lineno, ast.unparse(node)))
chk("源码里不再有裸的 int(body[...]) / int(body.get(...))", not bare, bare)
chk("_body_int 只有一处定义（一把尺子）",
    SERVER.count("def _body_int(") == 1, SERVER.count("def _body_int("))

# ── C 真机：脏数字要回 200 + 人话，好数字照旧 ────────────────────
srv = ThreadingHTTPServer(("127.0.0.1", 0), ui_server.Handler)
PORT = srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start()
atexit.register(srv.shutdown)
BASE = "http://127.0.0.1:%d" % PORT


def call(path, payload, timeout=15):
    data = json.dumps(payload).encode()
    req = urllib.request.Request(BASE + path, data=data,
                                 headers={"Content-Type": "application/json"})
    try:
        r = urllib.request.urlopen(req, timeout=timeout)
        return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()
    except Exception as e:
        return "ERR", ("%s: %s" % (type(e).__name__, e)).encode()


PYERR = ("invalid literal", "TypeError", "ValueError", "OverflowError")


def feed_ok(name, payload, want_ok=None, forbid_msg=True):
    st, body = call("/api/feed", payload)
    bad = None
    obj = {}
    if st == "ERR":
        bad = "连接被甩：%r" % body[:120]
    elif st != 200:
        bad = "HTTP %s：%r" % (st, body[:120])
    else:
        try:
            obj = json.loads(body or b"{}")
        except Exception as e:
            bad = "回包不是完整 JSON（%s）：%r" % (e, body[:120])
        else:
            if want_ok is not None and obj.get("ok") is not want_ok:
                bad = "ok 应为 %r：%r" % (want_ok, obj)
            if forbid_msg and any(t in json.dumps(obj, ensure_ascii=False) for t in PYERR):
                bad = "把 Python 报错原文递出去了：%r" % obj
    chk(name, bad is None, bad or "")
    return obj


for v in ("abc", {"a": 1}, [1], 1.5):
    feed_ok("reorder index=%r：回 200 + 人话，不当成「挪到末尾」" % (v,),
            {"act": "reorder", "id": "nope", "index": v}, want_ok=False)
feed_ok("reorder index=None：回 200 + JSON（当没传，老行为不变）",
        {"act": "reorder", "id": "nope", "index": None}, want_ok=False)

feed_ok("fetch_content limit='abc'：回 200 + JSON（当没写，默认 50）",
        {"act": "fetch_content", "limit": "abc"}, want_ok=True)
feed_ok("fetch_content limit={'a':1}：回 200 + JSON",
        {"act": "fetch_content", "limit": {"a": 1}}, want_ok=True)
feed_ok("prune days='abc' / max_entries=[]：回 200 + JSON",
        {"act": "prune", "days": "abc", "max_entries": []}, want_ok=True)
feed_ok("prune days={'a':1} / max_entries='x'：回 200 + JSON",
        {"act": "prune", "days": {"a": 1}, "max_entries": "x"}, want_ok=True)

# 好值照旧走得通：index=0 走的是「没有这个订阅源」，不是形状报错。
obj = feed_ok("reorder index=0（好数字）：仍回 200，且不是形状报错",
              {"act": "reorder", "id": "nope", "index": 0}, want_ok=False)
chk("reorder index=0：报的是「没有这个订阅源」而不是数字认不出",
    "没有这个订阅源" in (obj.get("msg") or ""), obj)
obj = feed_ok("fetch_content limit=7（好数字）：200 + ok",
              {"act": "fetch_content", "limit": 7}, want_ok=True)

bad = [c for c in checks if not c[0]]
print("\n请求体数字字段脏形状体检：%d 项通过，失败 %d 项" % (len(checks) - len(bad), len(bad)))
if bad:
    for _ok, n, x in bad:
        print("  ✗ " + n + ("  | " + x if x else ""))
sys.exit(1 if bad else 0)
