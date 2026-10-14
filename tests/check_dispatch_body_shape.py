#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""`/api/action` 里「本该是字符串 / 列表」的字段，形状写脏了不许把解释器的报错递出去。

为什么单开一条：`_body_str` / `_body_list` 已经立下一条规矩 —— 字段级脏形状
（数字 / 列表 / 对象）当没写，绝不抛。但 `_dispatch` 里书库那一摊动作是老代码，
写的是

    bid = (body.get("book") or "").strip()
    add = body.get("add") or []

`or ""` 换不掉非空脏值，`.strip()` 当场 AttributeError；`_action` 虽然兜了一层，
兜出来的却是**解释器的报错原文**：

    {"ok": false, "msg": "这个操作没做成：AttributeError: 'int' object has no attribute 'strip'"}

`book.tag` 的 `add` 更坏：`{"a": 1}` 不是空值，`for x in {"a": 1}` 按 key 拆出
「a」当标签**悄悄贴上去**；`shelf.add` 的 `ids=123` 则是 `list(123)` 抛 TypeError。
修法与其余字段同一把尺子：字符串走 `_body_str`，列表走 `_body_list`，单字符串 / 列表
两种正当写法照收。

这一条钉三层：
  A 纯函数：`_body_list` 只放行 list，脏形状一律回空表（对象不再按 key 拆）；
  B 源码：`_dispatch` 里不再有裸的 `(body.get("book"|"id"|...) or "").strip()`，
    book / add / ids 三处都走收口函数，以后写回旧形状会当场红；
  C 真机：拿脏形状打 `/api/action`，必须回完整的 200 + JSON + 人话（不是
    AttributeError / TypeError 原文）；好字符串那条路（新建 / 改名 / 删夹子）照旧。

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
SB = pathlib.Path(tempfile.mkdtemp(prefix="gz-dispatch-shape-"))
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


# ── A 纯函数：_body_list 只放行 list ──────────────────────────────
# 取不到就当这一条红，而不是让 AttributeError 把整份门禁带走 —— 后面 B / C 两层
# 仍要跑完，改坏的人和第一次读它的人都能看到「缺的是哪一样」。
_body_list = getattr(ui_server, "_body_list", None)
_body_str = getattr(ui_server, "_body_str", None)
chk("字段级收口 _body_list / _body_str 都在（唯一入口）",
    callable(_body_list) and callable(_body_str))

if callable(_body_list):
    chk("真列表原样放行（一个字不改）",
        _body_list([1, "a", None]) == [1, "a", None])
    chk("认不出的形状一律回空表，不抛",
        all(_body_list(v) == []
            for v in (None, 123, 1.5, True, float("nan"), "abc",
                      {"a": 1}, ("x",), {1, 2}, b"\xff", object())),
        [(repr(v), _body_list(v))
         for v in (123, "abc", {"a": 1}, ("x",), b"\xff")])
    # 对照：老写法把对象按 key 拆成假标签、把数字当场抛了 —— 这组断言不是空转。
    bogus = list(({"add": {"a": 1}} or {}).get("add") or [])
    chk("对照：旧的 `body.get(\"add\") or []` 会把对象的 key 当标签",
        bogus == ["a"], bogus)
    raised = False
    try:
        list({"ids": 123}.get("ids") or [])
    except TypeError:
        raised = True
    chk("对照：旧的 `list(body.get(\"ids\"))` 遇到数字确实 TypeError", raised)

if callable(_body_str):
    chk("_body_str 认不出的形状回默认，真字符串只去两头空白",
        _body_str({"name": "  甲\n"}, "name") == "甲"
        and _body_str({"name": 123}, "name") == "")

# ── B 源码：_dispatch 全走收口函数，不再有裸的旧形状 ─────────────
D_START = SERVER.index("    def _dispatch(self, body):")
D_END = SERVER.index("\ndef main():", D_START)
DISPATCH = SERVER[D_START:D_END]

chk("book 在 _dispatch 开头收口（各处 `(book or \"\").strip()` 落回真 str）",
    '_body_str(body, "book")' in DISPATCH,
    [l.strip() for l in DISPATCH.splitlines() if "book = " in l][:3])

# 这几个键以后不许再写回「取出来直接 .strip()」：`or ""` 挡不住非空脏值。
# `str(...)` 包住的那几处（tag.rename 的 from / to）不算 —— 取出来已经收成字符串。
# 注意 `export_dir` 那条是 `set_export_dir(body.get("dir"))`，脏值在 set_export_dir
# **函数体内**才 .strip()，不在这个正则的形状里；而它同时是
# tests/check_notes_csv.py 的 W5（`'body.get("dir")' in srv`）唯一的落点，动它会把
# 那条**已有**门禁打红。既有门禁不许改，所以这一处留旧形状，已在 PR.md「已知问题」记明。
bare = []
for k in ("dir", "module", "name", "id", "book", "folder", "before"):
    pat = re.compile(r'(?<!str)\(body\.get\("%s"\) or ""\)\.strip\(\)' % re.escape(k))
    for m in pat.finditer(DISPATCH):
        bare.append(m.group(0))
chk("_dispatch 里不再有裸的 body 字符串字段 .strip()", not bare, bare[:6])

chk("book.tag 的 add 走 `单字符串 / _body_list` 两条正当形状",
    "raw_add" in DISPATCH
    and "isinstance(raw_add, str)" in DISPATCH
    and "_body_list(raw_add)" in DISPATCH,
    [l.strip() for l in DISPATCH.splitlines() if "add" in l][:5])
chk("book.tag 的 remove 走 _body_str",
    '_body_str(body, "remove")' in DISPATCH)
chk("shelf.add 的 ids 走 `单字符串 / _body_list`，不再 `list(ids)`",
    "isinstance(raw_ids, str)" in DISPATCH
    and "_body_list(raw_ids)" in DISPATCH
    and "list(ids)" not in DISPATCH,
    [l.strip() for l in DISPATCH.splitlines() if "ids" in l][:5])
chk("_body_list / _body_str 各只有一处定义（一把尺子）",
    SERVER.count("def _body_list(") == 1 and SERVER.count("def _body_str(") == 1,
    (SERVER.count("def _body_list("), SERVER.count("def _body_str(")))

# 模块名还要再收一道：`lib_scope` 是 `module in DIR_OF`，列表 / 对象当模块名进来
# 就是 `TypeError: unhashable type` —— 同样是把解释器的话递到界面上。
chk("_dispatch 的 module 收口后再进 lib_scope（不再 unhashable）",
    'lib_scope(lib, body.get("module"))' not in DISPATCH
    and DISPATCH.count('lib_scope(lib, _body_str(body, "module"))') >= 4,
    [l.strip() for l in DISPATCH.splitlines() if "lib_scope(lib, body" in l][:4])

# ── C 真机：脏形状回 200 + 人话，好字符串照旧 ─────────────────────
srv = ThreadingHTTPServer(("127.0.0.1", 0), ui_server.Handler)
PORT = srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start()
atexit.register(srv.shutdown)
BASE = "http://127.0.0.1:%d" % PORT

# 这些报错原文一旦出现在回话里，就是脏形状穿过收口、把解释器的话递给了用户。
NOISE = ("AttributeError", "TypeError", "Traceback", "object has no attribute",
         "object is not iterable", "unhashable")


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


def act_json(name, payload, want_ok=None):
    """脏形状打 /api/action：必须 200 + 完整 JSON + 人话 + 不出解释器报错。"""
    st, body = call("/api/action", payload)
    bad = None
    obj = None
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
            msg = str(obj.get("msg") or "")
            if not msg:
                bad = "没有一句人话可看：%r" % obj
            elif any(w in msg for w in NOISE):
                bad = "把解释器的报错递出来了：%r" % msg
            if want_ok is not None and obj.get("ok") is not want_ok:
                bad = "ok 应为 %r：%r" % (want_ok, obj)
    chk(name, bad is None, bad or "")
    return obj


# 脏形状：每一处老写法都会抛或悄悄写错。
act_json("book.tag book=123：回 200 + 人话，不当场抛",
         {"action": "book.tag", "book": 123, "add": ["x"]}, want_ok=False)
act_json("book.tag add=123：回 200 + 人话，不抛",
         {"action": "book.tag", "book": "nope", "add": 123}, want_ok=False)
act_json("book.tag add={\"a\":1}：回 200 + 人话，不按 key 拆假标签",
         {"action": "book.tag", "book": "nope", "add": {"a": 1}}, want_ok=False)
act_json("book.tag remove=123：回 200 + 人话，不抛",
         {"action": "book.tag", "book": "nope", "remove": 123}, want_ok=False)
act_json("folder.new name=123：回 200 + 人话，不抛",
         {"action": "folder.new", "name": 123}, want_ok=False)
act_json("folder.rename id=123：回 200 + 人话，不抛",
         {"action": "folder.rename", "id": 123, "name": "x"}, want_ok=False)
act_json("book.move book=1 folder={}：回 200 + 人话，不抛",
         {"action": "book.move", "book": 1, "folder": {}}, want_ok=False)
act_json("book.order book={} before=[]：回 200 + 人话，不抛",
         {"action": "book.order", "book": {}, "before": []}, want_ok=False)
act_json("book.delete book=1：回 200 + 人话，不抛、不误删",
         {"action": "book.delete", "book": 1}, want_ok=False)
act_json("shelf.add ids=123：回 200 + 人话，不 `list(123)`",
         {"action": "shelf.add", "ids": 123}, want_ok=False)
act_json("shelf.add ids={\"a\":1}：回 200 + 人话，不把 key 当书号",
         {"action": "shelf.add", "ids": {"a": 1}}, want_ok=False)

# 脏形状不许留下痕迹：拿数字当名字没建出夹子。
lib0 = ui_server.load_lib()
mods0 = lib0.get("modules") or {}
n0 = len((mods0.get("weread") or {}).get("folders") or [])
chk("脏 folder.new 没在账上留下夹子", n0 == 0, n0)

# 好字符串照旧走得通：建 / 改名 / 删一个来回（都在沙盒里）。
r = act_json("folder.new name=' 测试夹 '：好字符串照旧新建（两头空白去掉）",
             {"action": "folder.new", "name": " 测试夹 "}, want_ok=True)
fid = (r or {}).get("id")
chk("folder.new 回了一个夹子 id", isinstance(fid, str) and fid, fid)
st = json.loads(call("/api/state")[1].decode("utf-8"))
fnames = [f["name"] for f in ((st.get("libs") or {}).get("weread") or {})
          .get("folders", [])]
chk("好名字真的落进账本（不是只回了 ok）", fnames == ["测试夹"], fnames)

r = act_json("folder.rename id=<真 id> name=' 新名 '：好字符串照旧改名",
             {"action": "folder.rename", "id": fid, "name": " 新名 "}, want_ok=True)
st = json.loads(call("/api/state")[1].decode("utf-8"))
fnames = [f["name"] for f in ((st.get("libs") or {}).get("weread") or {})
          .get("folders", [])]
chk("改名真的生效", fnames == ["新名"], fnames)

act_json("folder.drop id=<真 id>：好字符串照旧删得掉",
         {"action": "folder.drop", "id": fid}, want_ok=True)
st = json.loads(call("/api/state")[1].decode("utf-8"))
fnames = [f["name"] for f in ((st.get("libs") or {}).get("weread") or {})
          .get("folders", [])]
chk("删完账上没有了", fnames == [], fnames)

bad = [c for c in checks if not c[0]]
print("\n/api/action 请求体字段脏形状体检：%d 项通过，失败 %d 项"
      % (len(checks) - len(bad), len(bad)))
if bad:
    for _ok, n, x in bad:
        print("  ✗ " + n + ("  | " + x if x else ""))
sys.exit(1 if bad else 0)
