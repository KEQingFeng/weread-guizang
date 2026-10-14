#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""POST 请求体里「本该是字符串」的字段，形状写脏了不许把连接甩掉。

为什么单开一条：`do_POST` 开头立过一条规矩 —— 「合法 JSON 也可能是数组 / 字符串 /
数字，这里统一收成空对象，省得每个分支各自判类型（畸形体不该把连接搞崩）」。可惜
那一道只管顶层：字段级没管。老写法又是

    ((body or {}).get("state") or "").strip()

`or ""` 换不掉数字 / 列表 / 对象（非空值都是真），紧跟着的 `.strip()` 当场
`AttributeError`。于是：

    POST /api/book_state   {"state": 123}
    POST /api/agent/chat   {"q": 123}
    POST /api/agent/quick  {"mode": 123}
    POST /api/config       {"weread_key": 123} / {"flomo_url": 123}

这五处都会在 `.strip()` 处抛。这几个处理函数没有兜底 try/except（不像 `feed_do`），
异常一路穿出 `do_POST`，`BaseHTTPRequestHandler` 不会替你回 500，它只是把连接关掉
—— 前端 `await fetch` 收到 RemoteDisconnected，屏幕上就是「按下去毫无动静」。修法与
查询串那条 `_qint`、请求体列表那条 `_body_list` 同一把尺子：`_body_str()` 只放行真
字符串，认不出就当没写、回默认值，绝不抛。

这一条钉三层：
  A 纯函数：`_body_str` 只放行真字符串，脏形状一律回默认；好值只去两头空白，一个字不改；
  B 源码：五处调用点全部改用它，且 `do_POST` 里不再有裸的「body 字符串字段直接
    .strip()」，以后新加路由写回旧形状会当场红；
  C 真机：五个口拿脏形状打过去，必须回完整的 200 + JSON（不是 0 字节、不是
    RemoteDisconnected），好字符串照旧走得通，服务日志里不留 Traceback。

不起浏览器，归静态段，--fast 也得带上。
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
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

HERE = pathlib.Path(__file__).resolve().parent
REPO = HERE.parent
sys.path.insert(0, str(REPO))

# 自带一个沙盒，绝不碰用户真实的 cache/ 与书库，也不和别的套件共用那份共享沙盒。
SB = pathlib.Path(tempfile.mkdtemp(prefix="gz-post-str-"))
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


# ── A 纯函数：只放行真字符串 ─────────────────────────────────────
# 取不到就当这一条红，而不是让 AttributeError 把整份门禁带走 —— 后面 B / C 两层
# 仍要跑完，改坏的人和第一次读它的人都能看到「缺的是哪一样」。
_body_str = getattr(ui_server, "_body_str", None)
chk("字段级收口 _body_str 存在（唯一入口）", callable(_body_str))
if callable(_body_str):
    chk("真字符串只去两头空白（中间一个字不动）",
        _body_str({"state": "  已读\n"}, "state") == "已读",
        _body_str({"state": "  已读\n"}, "state"))
    chk("认不出的形状一律回默认，不抛",
        all(_body_str({"k": v}, "k") == ""
            for v in (None, 123, 1.5, True, float("nan"), float("inf"),
                      [], ["a"], {"a": 1}, ("x",), {1, 2}, b"\xff", object())),
        [(repr(v), _body_str({"k": v}, "k"))
         for v in (None, 123, 1.5, True, [], ["a"], {"a": 1}, b"\xff")])
    chk("认不出的形状回的是调用方给的默认值",
        _body_str({"mode": 123}, "mode", "translate") == "translate")
    chk("没带这个键 / body 不是对象也回默认",
        _body_str({}, "q") == "" and _body_str(None, "q") == "")
    chk("空串原样是空串（不是 None），调用方的 `or 默认` 照旧管用",
        _body_str({"q": "   "}, "q") == "")
    # 对照：老写法确实会抛 —— 这组断言不是空转。
    old_raised = False
    try:
        (({"state": 1} or {}).get("state") or "").strip()
    except AttributeError:
        old_raised = True
    chk("对照：旧的 `(... or \"\").strip()` 遇到数字确实 AttributeError",
        old_raised)

# ── B 源码：五处调用点全用它，且不再有裸的旧形状 ──────────────────
POST = SERVER[SERVER.index("    def do_POST(self):"):SERVER.index("    def _dispatch(self, body):")]

chk("book_state 的 state 走 _body_str",
    '_body_str(body, "state")' in POST,
    [l.strip() for l in POST.splitlines() if '"state"' in l][:3])
chk("agent/chat 的 q 走 _body_str",
    '_body_str(body, "q")' in POST,
    [l.strip() for l in POST.splitlines() if '".get("q")' in l][:3])
chk("agent/quick 的 mode 走 _body_str（空值仍落回 translate）",
    '_body_str(body, "mode") or "translate"' in POST,
    [l.strip() for l in POST.splitlines() if '"mode"' in l][:4])
chk("config 的两个地址走 _body_str",
    '_body_str(body, "weread_key")' in POST and '_body_str(body, "flomo_url")' in POST,
    [l.strip() for l in POST.splitlines() if "weread_key" in l or "flomo_url" in l][:4])

# 这五个键以后不许再写回「取出来直接 .strip()」：`or ""` 挡不住非空脏值。
# `str(...)` 包住的那几处（clip 的 mode 等）不算 —— 它们在取出来那一刻已经
# 收成字符串，`.strip()` 落在一个真 str 上，不会抛。
bare = []
for k in ("state", "q", "mode", "weread_key", "flomo_url"):
    pat = re.compile(r'(?<!str)\(\(body or \{\}\)\.get\("%s"\)\s*[^)]*\)\.strip\(\)'
                     % re.escape(k))
    for m in pat.finditer(POST):
        bare.append(m.group(0))
chk("do_POST 里不再有裸的 body 字符串字段 .strip()", not bare, bare)
chk("_body_str 只有一处定义（一把尺子）",
    SERVER.count("def _body_str(") == 1, SERVER.count("def _body_str("))

# ── C 真机：脏形状要回 200 + JSON，好字符串照旧 ─────────────────
srv = ThreadingHTTPServer(("127.0.0.1", 0), ui_server.Handler)
PORT = srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start()
atexit.register(srv.shutdown)
BASE = "http://127.0.0.1:%d" % PORT


def call(path, payload=None, timeout=15):
    """→ (status, body_bytes)。status 为 'ERR' 表示连接层就失败了（服务断了）。"""
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(BASE + path, data=data,
                                 headers={"Content-Type": "application/json"})
    try:
        r = urllib.request.urlopen(req, timeout=timeout)
        return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()
    except Exception as e:
        return "ERR", ("%s: %s" % (type(e).__name__, e)).encode()


def ok_json(name, path, payload, must=None, want_ok=None):
    st, body = call(path, payload)
    bad = None
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
            if must and any(k not in obj for k in must):
                bad = "缺字段 %r：%r" % (must, obj)
    chk(name, bad is None, bad or "")


ok_json("book_state state=123：回 200 + JSON，不当场断线",
        "/api/book_state", {"books": ["bk_0123456789abcdef"], "state": 123},
        must=("ok",), want_ok=True)
ok_json("agent/chat q=123：回 200 + JSON（把它当没写，回人话）",
        "/api/agent/chat", {"q": 123}, must=("ok", "msg"), want_ok=False)
ok_json("agent/quick mode=123：回 200 + JSON（落回默认档位）",
        "/api/agent/quick", {"mode": 123, "text": "hello"},
        must=("ok", "msg"))
ok_json("config weread_key=123：回 200 + JSON，不当场断线",
        "/api/config", {"weread_key": 123}, must=("ok",))
ok_json("config flomo_url=123：回 200 + JSON，不当场断线",
        "/api/config", {"flomo_url": 123}, must=("ok",))

# 好字符串照旧走得通（修脏值不许把正常这条路一起改坏）。
ok_json("book_state state='已读'：好字符串照旧生效",
        "/api/book_state", {"books": ["bk_0123456789abcdef"], "state": "已读"},
        must=("ok",), want_ok=True)
lib = ui_server.load_lib()
states = [sc.get("state") or {} for sc in (lib.get("modules") or {}).values()
          if isinstance(sc, dict)]
chk("好字符串真的落进了账本（不是只回了 ok）",
    any(s.get("bk_0123456789abcdef") == "已读" for s in states),
    [s.get("bk_0123456789abcdef") for s in states])

bad = [c for c in checks if not c[0]]
print("\n请求体字符串字段脏形状体检：%d 项通过，失败 %d 项" % (len(checks) - len(bad), len(bad)))
if bad:
    for _ok, n, x in bad:
        print("  ✗ " + n + ("  | " + x if x else ""))
sys.exit(1 if bad else 0)
