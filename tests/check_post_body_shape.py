# -*- coding: utf-8 -*-
"""POST 请求体里「本该是列表」的字段，形状写脏了不许把连接甩掉。

为什么单开一条：`do_POST` 开头立过一条规矩 —— 「合法 JSON 也可能是数组 / 字符串 /
数字，这里统一收成空对象，省得每个分支各自判类型（畸形体不该把连接搞崩）」。可惜
那一道只管顶层：顶层不是对象时收成 `{}`，字段级没管。于是：

    POST /api/book_state  {"books": 123}
    POST /api/delete_many {"books": 123}
    POST /api/flomo       {"items": 123}
    POST /api/notes_csv   {"items": 123}

这四条都会在 `for b in ids:` / `items[:200]` / `for it in ...` 处抛
`TypeError`（对象还多一种 `unhashable type: 'slice'`）。这些处理函数没有兜底
try/except（不像 `feed_do`），异常一路穿出 `do_POST`，`BaseHTTPRequestHandler`
不会替你回 500，它只是把连接关掉 —— 前端 `await fetch` 收到 RemoteDisconnected，
屏幕上就是「按下去毫无动静」。修法与查询串那条 `_qint` 同一把尺子：`_body_list()`
认不出就回空表（当没传），绝不抛。

这一条钉三层：
  A 纯函数：`_body_list` 只放行真正的列表，脏形状一律回空表，好值不被误伤；
  B 源码：四处调用点全部改用它，且 `do_POST` 里不再有裸的「body 字段当列表用」，
    以后新加路由写回旧形状会当场红；
  C 真机：四个口拿 `{"books": 123}` / `{"items": 123}` 打过去，必须回完整的
    200 + JSON（不是 0 字节、不是 RemoteDisconnected），正常列表照旧做得成，
    服务日志里不留 Traceback。

不起浏览器，归静态段，--fast 也得带上。
"""
import atexit
import json
import os
import pathlib
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request

HERE = pathlib.Path(__file__).resolve().parent
REPO = HERE.parent
sys.path.insert(0, str(REPO))

# 自带一个沙盒，绝不碰用户真实的 cache/ 与书库，也不和别的套件共用那份共享沙盒。
SB = pathlib.Path(tempfile.mkdtemp(prefix="gz-post-shape-"))
atexit.register(lambda: shutil.rmtree(SB, ignore_errors=True))
for _d in ("data", "books", "shots"):
    (SB / _d).mkdir(parents=True, exist_ok=True)
os.environ["GUIZANG_DATA"] = str(SB / "data")
os.environ["GUIZANG_BOOKS"] = str(SB / "books")
os.environ["GUIZANG_SHOT_DIR"] = str(SB / "shots")
os.environ["GUIZANG_ANNA_NO_WINDOW"] = "1"
os.environ["PYTHONUNBUFFERED"] = "1"

import ui_server  # noqa: E402  —— 必须在环境变量设好之后 import

FAIL = []
PASSED = [0]


def chk(name, cond, extra=""):
    if cond:
        PASSED[0] += 1
    else:
        FAIL.append(name)
    print(("PASS  " if cond else "FAIL  ") + name + ("" if cond else "   " + str(extra)[:240]))


# ── A 纯函数：只放行真正的列表 ────────────────────────────────────
# 取不到就当这一条红，而不是让 AttributeError 把整份门禁带走 —— 后面 B / C 两层
# 仍要跑完，改坏的人和第一次读它的人都能看到「缺的是哪一样」。
_body_list = getattr(ui_server, "_body_list", None)
chk("字段级收口 _body_list 存在（唯一入口）", callable(_body_list))
if callable(_body_list):
    chk("列表原样放行（同一个对象）",
        _body_list(["a", "b"]) == ["a", "b"])
    chk("空列表仍是列表（不是 None）",
        _body_list([]) == [])
    chk("认不出的形状一律回空表，不抛",
        all(_body_list(v) == []
            for v in (None, 123, 1.5, True, float("nan"), float("inf"), "abc",
                      {"a": 1}, ("x", "y"), {1, 2}, b"\xff", object())),
        [(repr(v), _body_list(v))
         for v in (None, 123, 1.5, True, "abc", {"a": 1}, ("x", "y"), b"\xff")])
    chk("坏形状回的是新空表，调用方拿到就能安全切片 / 迭代",
        isinstance(_body_list(123), list) and _body_list(123)[:200] == [])

# ── B 源码：四个调用点全用它，且不再有裸的旧形状 ──────────────────
SERVER = (REPO / "ui_server.py").read_text(encoding="utf-8")
POST = SERVER[SERVER.index("    def do_POST(self):"):SERVER.index("    def _dispatch(self, body):")]

chk("book_state / delete_many 的 books 走 _body_list",
    POST.count('_body_list((body or {}).get("books"))') == 2,
    [l.strip() for l in POST.splitlines() if '"books"' in l][:4])
chk("flomo / notes_csv 的 items 走 _body_list",
    POST.count('_body_list((body or {}).get("items"))') == 2,
    [l.strip() for l in POST.splitlines() if '"items"' in l][:4])

bare_iter = re.findall(r"for\s+\w+\s+in\s+\(body or \{\}\)\.get\(", POST)
chk("do_POST 里不再有「把 body 字段直接 for 起来」",
    not bare_iter, bare_iter)
bare_default = re.findall(r'\(body or \{\}\)\.get\("(?:books|items)"\)\s*or\s*\[\]', POST)
chk("do_POST 里不再有 `... or []` 那种「看着像列表就当列表」的字段收口",
    not bare_default, bare_default)
chk("_body_list 只有一处定义（一把尺子）",
    SERVER.count("def _body_list(") == 1, SERVER.count("def _body_list("))

# ── C 真机：四个口打脏形状，要回 200 + JSON ──────────────────────
def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


port = _free_port()
logp = SB / "server.log"
logf = open(logp, "w")
srv = subprocess.Popen([sys.executable, "ui_server.py", "--port", str(port)],
                       cwd=str(REPO), env=dict(os.environ),
                       stdout=logf, stderr=subprocess.STDOUT)
atexit.register(lambda: srv.terminate())
base = ""
for _ in range(160):
    m = re.search(r"http://127\.0\.0\.1:(\d+)", logp.read_text(errors="replace"))
    if m:
        try:
            urllib.request.urlopen("http://127.0.0.1:%s/api/state" % m.group(1),
                                   timeout=1).read()
            base = "http://127.0.0.1:%s" % m.group(1)
            break
        except Exception:
            pass
    time.sleep(0.25)


def post(path, payload):
    """真打一次。连接被甩时 urlopen 抛的就是那一串，交给调用方判。"""
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(base + path, data=body, method="POST",
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=8) as r:
        return r.status, json.loads(r.read().decode("utf-8", "replace"))


DIRTY = (123, 1.5, True, {"a": 1}, "abc", None)
ROUTES = ("/api/book_state", "/api/delete_many", "/api/flomo", "/api/notes_csv")

if not base:
    chk("真机：服务起来了", False, logp.read_text(errors="replace")[-300:])
else:
    chk("真机：服务起来了", True)
    for path in ROUTES:
        crashed = []
        for v in DIRTY:
            key = "books" if path in ("/api/book_state", "/api/delete_many") else "items"
            try:
                code, out = post(path, {key: v})
            except Exception as e:            # 连接被甩时抛的就是它
                crashed.append((repr(v), "%s: %s" % (type(e).__name__, e)))
                continue
            if code != 200 or not isinstance(out, dict) or not out.get("ok") in (True, False):
                crashed.append((repr(v), code, out))
        chk("真机 %s：脏形状回 200 + JSON（不是连接被甩）" % path, not crashed, crashed)

    # 正常的列表照旧做得成（别把收口收成一律空转）。
    try:
        code, out = post("/api/book_state", {"books": ["1234567"], "state": "reading"})
        ok = code == 200 and out.get("ok") and "已更新 1 本" in (out.get("msg") or "")
    except Exception as e:
        ok, out = False, "%s: %s" % (type(e).__name__, e)
    chk("真机 book_state：正常书号照旧标得上", ok, out)

    try:
        code, out = post("/api/notes_csv", {"items": [{"text": "这一条要进 CSV", "at": 1}]})
        ok = code == 200 and out.get("ok") and out.get("n") == 1
    except Exception as e:
        ok, out = False, "%s: %s" % (type(e).__name__, e)
    chk("真机 notes_csv：正常列表照旧导得成（且 n=1）", ok, out)

    try:
        code, out = post("/api/flomo", {"items": []})
        ok = code == 200 and out.get("ok") is False and "没有选中" in (out.get("msg") or "")
    except Exception as e:
        ok, out = False, "%s: %s" % (type(e).__name__, e)
    chk("真机 flomo：空列表按「没选中」回人话（不是报错）", ok, out)

    chk("真机：服务日志里没有未捕获异常",
        "Traceback" not in logp.read_text(errors="replace"))
    srv.terminate()

print()
if FAIL:
    print("POST 请求体形状门禁有 %d 处不对：%s" % (len(FAIL), "、".join(FAIL)))
else:
    print("POST 请求体形状：字段级的畸形体也当没传，纯函数不抛、真机不断线。")
sys.exit(1 if FAIL else 0)
