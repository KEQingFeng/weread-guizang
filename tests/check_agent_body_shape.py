#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""小 Agent 两个口的请求体字段（history / text / ctx）写脏了不许把连接甩掉。

为什么单开一条：`_body_str`（字符串字段）与 `_body_list`（列表字段）各收过一档，
可「小 Agent 对话」这两个口还留着半截没收：

    POST /api/agent/chat  {"q": "你好", "ctx": 123, "history": 123}
    POST /api/agent/chat  {"q": "你好", "history": [{"role": "user"}, 1, "x"]}
    POST /api/agent/quick {"mode": "translate", "text": 123}
    POST /api/agent/quick {"mode": "translate", "text": "hi", "ctx": 123}

`history` 那份是老写法直接 `(history or [])[-8:]` 再 `(h or {}).get(...)`：数字 /
非空对象在切片处 TypeError，数组里混进数字 / 字符串在 `.get` 处 AttributeError，
字符串还会按字符逐条炸。`text` / `ctx` 则是 `(body or {}).get(...)` 原样递给
`agent_quick` / `agent_payload`，里面的 `(text or "").strip()` 对数字同样 AttributeError。
这几处都没有兜底 try/except（`agent/chat` 要走到 `_sse_start()` 之后才有），异常一路
穿出 `do_POST`，`BaseHTTPRequestHandler` 不会替你回 500，它只是把连接关掉 —— 前端
`await fetch` 收到 RemoteDisconnected，屏幕上就是「按下去毫无动静」，与
`_body_str` / `_body_list` 那两条同一个症状。

收口是 ui_server._body_msgs：只放行 `[{"role": "user"|"assistant", "content": str}]`，
数组里形状不对的单条**丢掉**、不带走整段；不是列表就当没写。`text` / `ctx` 复用
既有的 `_body_str`。删的只是认不出的形状，顺序、内容一个字不改，`[-8:]` 与截断仍归
`agent_payload` 自己的策略。

这一条钉三层：
  A 纯函数：`_body_msgs` 只放行真消息，脏形状回空表、脏单条丢掉，好值不被误伤、
    不改入参；并对照老写法确实会抛；
  B 源码：`/api/agent/chat` 的 history / ctx 与 `/api/agent/quick` 的 text / ctx 全走
    收口，且全文件不再有裸的 `(body or {}).get("history"|"text"|"ctx")`；
  C 真机：配好（假的）小 Agent，拿脏形状打两个口，必须回完整 200（不是 0 字节、
    不是 RemoteDisconnected），好 history 照旧进 payload、坏单条不许进。

不起浏览器、不连外网（agent_open 被换成假的），归静态段，--fast 也得带上。
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

SB = pathlib.Path(tempfile.mkdtemp(prefix="gz-agent-body-"))
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


# ── A 纯函数：只放行真消息，脏形状丢单条 / 回空表 ─────────────────
_body_msgs = getattr(ui_server, "_body_msgs", None)
chk("请求体消息数组收口 _body_msgs 存在（唯一入口）", callable(_body_msgs))
if callable(_body_msgs):
    good = [{"role": "user", "content": "早"},
            {"role": "assistant", "content": "在"}]
    chk("好数组原样认（顺序与内容一个字不改）",
        _body_msgs({"history": good}, "history") == good,
        _body_msgs({"history": good}, "history"))
    chk("内容不被去空白（截断/去空白是调用方的事）",
        _body_msgs({"history": [{"role": "user", "content": "  hi  "}]}, "history")
        == [{"role": "user", "content": "  hi  "}])
    mixed = [{"role": "user", "content": "早"}, 1, "x", None,
             {"role": "system", "content": "规则"}, {"role": "assistant", "content": 123},
             {"role": "assistant", "content": "在"}, {"content": "没名字"}]
    chk("脏单条丢掉、好的留下、顺序不乱（不带走整段）",
        _body_msgs({"history": mixed}, "history") == good,
        _body_msgs({"history": mixed}, "history"))
    chk("不是列表的形状一律回空表，不抛",
        all(_body_msgs({"history": v}, "history") == []
            for v in (None, 123, 1.5, True, float("nan"), {"a": 1}, "ab",
                      ("x",), {1, 2}, b"\xff", object())),
        [(repr(v), _body_msgs({"history": v}, "history"))
         for v in (123, {"a": 1}, "ab", b"\xff")])
    chk("没带这个键 / body 不是对象也回空表",
        _body_msgs({}, "history") == [] and _body_msgs(None, "history") == [])
    src = [{"role": "user", "content": "早"}]
    _body_msgs({"history": src}, "history")
    chk("不改入参（筛选是复制，不是就地改）",
        src == [{"role": "user", "content": "早"}], src)
    chk("回的是新表（调用方改动不会串回去）",
        _body_msgs({"history": good}, "history") is not good)
    # 对照：老写法确实会抛 —— 这组断言不是空转。
    def _old_raises(history):
        try:
            for h in (history or [])[-8:]:
                (h or {}).get("role")
        except Exception as e:
            return type(e).__name__
        return None

    chk("对照：老写法 数字 / 数组混数字 / 非空对象 确实当场抛",
        _old_raises(123) == "TypeError"
        and _old_raises([1]) == "AttributeError"
        and _old_raises({"a": 1}) == "KeyError",
        (_old_raises(123), _old_raises([1]), _old_raises({"a": 1})))

# ── B 源码：四处都走收口，不再有裸的 body 字段 ────────────────────
POST = SERVER[SERVER.index("    def do_POST(self):"):SERVER.index("    def _dispatch(self, body):")]

chk("agent/chat 的 history 走 _body_msgs",
    '_body_msgs(body, "history")' in POST,
    [l.strip() for l in POST.splitlines() if "history" in l][:4])
chk("agent/chat 的 ctx 走 _body_str",
    'agent_payload(q, _body_msgs(body, "history"),' in POST
    and '_body_str(body, "ctx")' in POST,
    [l.strip() for l in POST.splitlines() if "agent_payload(" in l][:3])
chk("agent/quick 的 text / ctx 走 _body_str",
    '_body_str(body, "text")' in POST and '_body_str(body, "ctx")' in POST,
    [l.strip() for l in POST.splitlines() if "agent_quick(" in l][:3])

bare = []
for k in ("history", "text", "ctx"):
    pat = re.compile(r'\(\(body or \{\}\)\.get\("%s"\)' % re.escape(k))
    for m in pat.finditer(POST):
        bare.append(m.group(0))
chk("do_POST 里不再有裸的 (body or {}).get(\"history\"|\"text\"|\"ctx\")", not bare, bare)
chk("_body_msgs 只有一处定义（一把尺子）",
    SERVER.count("def _body_msgs(") == 1, SERVER.count("def _body_msgs("))

# ── C 真机：脏形状回 200，好 history 进 payload、坏单条不进 ─────────
# 把上游换成假的：既不连外网，也要能看见 agent_payload 到底拼了什么。
CAPTURED = []
STREAM_LINES = [b'data: {"choices":[{"delta":{"content":"ok"},"finish_reason":"stop"}]}\n',
                b'data: [DONE]\n']
JSON_BODY = json.dumps({"choices": [{"message": {"content": "译好了"}}]},
                       ensure_ascii=False).encode("utf-8")


class FakeResp:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return JSON_BODY

    def __iter__(self):
        return iter(STREAM_LINES)


def fake_agent_open(payload, stream=True, timeout=120):
    CAPTURED.append(payload)
    return FakeResp()


ui_server.agent_open = fake_agent_open
ok, msg = ui_server.set_agent_cfg("http://127.0.0.1:9/v1", "k", "test-model")
chk("先把（假的）小 Agent 配上，让请求走得到 payload 那一行", bool(ok), msg)

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


def ok_stream(name, path, payload, must=(b'"done":true',)):
    st, body = call(path, payload)
    bad = None
    if st == "ERR":
        bad = "连接被甩：%r" % body[:120]
    elif st != 200:
        bad = "HTTP %s：%r" % (st, body[:120])
    elif must and any(m not in body for m in must):
        bad = "流没收干净（缺 %r）：%r" % (must, body[:200])
    chk(name, bad is None, bad or "")


def messages_of(last=True):
    p = CAPTURED[-1] if last else CAPTURED[0]
    return p["body"]["messages"]


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


# 三个脏字段一起上：老代码会在 payload 拼装 / 切片处抛，连接被甩。
ok_stream("agent/chat history=123 + ctx=123：回完整流，不断线",
          "/api/agent/chat", {"q": "你好", "history": 123, "ctx": 123})
ok_stream("agent/chat history=[1,\"x\"]：脏单条丢掉，对话照旧发得出去",
          "/api/agent/chat", {"q": "你好", "history": [1, "x", None]})
ok_stream("agent/chat history=\"ab\"：不再按字符逐条炸",
          "/api/agent/chat", {"q": "你好", "history": "ab"})
ok_stream("agent/chat history={\"a\":1}：非空对象不再切片抛",
          "/api/agent/chat", {"q": "你好", "history": {"a": 1}})

# 好 history 必须原样进 payload，坏单条一条都不许进。
CAPTURED.clear()
ok_stream("agent/chat 好 history 照旧发得出去",
          "/api/agent/chat",
          {"q": "你好",
           "history": [{"role": "user", "content": "早"},
                       1,
                       {"role": "system", "content": "规则"},
                       {"role": "assistant", "content": 123},
                       {"role": "assistant", "content": "在"}]})
msgs = messages_of()
tail = [(m.get("role"), m.get("content")) for m in msgs
        if m.get("role") in ("user", "assistant")]
chk("好 history 进了 payload（顺序与内容不变）",
    tail == [("user", "早"), ("assistant", "在"), ("user", "你好")], tail)
chk("坏单条（数字 / system / 数字 content）一条都没进 payload",
    all(c not in ("规则",) for _r, c in tail) and all(r in ("user", "assistant")
                                                      for r, _c in tail), tail)
chk("payload 的 model 仍是配置里那个", CAPTURED[-1]["body"].get("model") == "test-model",
    CAPTURED[-1]["body"].get("model"))

# 上下文照旧：好 ctx 进 system 摘要，脏 ctx 当没写。
CAPTURED.clear()
ok_stream("agent/chat 好 ctx 照旧生效",
          "/api/agent/chat", {"q": "你好", "ctx": "我正在读《活着》"})
sys_texts = [m.get("content") or "" for m in messages_of() if m.get("role") == "system"]
chk("好 ctx 落进 system 摘要里", any("我正在读《活着》" in t for t in sys_texts),
    [t[:80] for t in sys_texts])
CAPTURED.clear()
ok_stream("agent/chat ctx=123 照旧回完整流",
          "/api/agent/chat", {"q": "你好", "ctx": 123})
sys_texts = [m.get("content") or "" for m in messages_of() if m.get("role") == "system"]
chk("脏 ctx 当没写（没被塞成 \"123\"）", all("123" not in t for t in sys_texts),
    [t[:80] for t in sys_texts])

# 8 轮的截断仍是 agent_payload 的策略，收口不许把它弄丢。
many = [{"role": "user", "content": "m%d" % i} for i in range(12)]
p = ui_server.agent_payload("现在", ui_server._body_msgs({"history": many}, "history"), "")
hist = [m["content"] for m in p["body"]["messages"] if m["role"] == "user"]
chk("[-8:] 截断没丢（最近 8 轮 + 本次提问）",
    hist == ["m4", "m5", "m6", "m7", "m8", "m9", "m10", "m11", "现在"], hist)

# quick 两个字段。
ok_json("agent/quick text=123：回 200 + JSON（当没写，回人话）",
        "/api/agent/quick", {"mode": "translate", "text": 123},
        must=("ok", "msg"), want_ok=False)
CAPTURED.clear()
ok_json("agent/quick 好 text 照旧问得出去",
        "/api/agent/quick", {"mode": "translate", "text": "hello"})
user_texts = [m.get("content") or "" for m in CAPTURED[-1]["body"]["messages"]
              if m.get("role") == "user"]
chk("好 text 进了上游请求", any("hello" in t for t in user_texts), user_texts)
CAPTURED.clear()
ok_json("agent/quick ctx=123：回 200 + JSON，不当场 AttributeError",
        "/api/agent/quick", {"mode": "translate", "text": "hello", "ctx": 123})
user_texts = [m.get("content") or "" for m in CAPTURED[-1]["body"]["messages"]
              if m.get("role") == "user"]
chk("脏 ctx 当没写（没拼出「上下文：123」）",
    all("上下文" not in t for t in user_texts), user_texts)
CAPTURED.clear()
ok_json("agent/quick 好 ctx 照旧拼进提问",
        "/api/agent/quick", {"mode": "translate", "text": "hello", "ctx": "前一句"})
user_texts = [m.get("content") or "" for m in CAPTURED[-1]["body"]["messages"]
              if m.get("role") == "user"]
chk("好 ctx 拼成「上下文：…」",
    any("上下文：前一句" in t and "hello" in t for t in user_texts), user_texts)

bad = [c for c in checks if not c[0]]
print("\n小 Agent 请求体字段脏形状体检：%d 项通过，失败 %d 项" % (len(checks) - len(bad), len(bad)))
if bad:
    for _ok, n, x in bad:
        print("  ✗ " + n + ("  | " + x if x else ""))
sys.exit(1 if bad else 0)
