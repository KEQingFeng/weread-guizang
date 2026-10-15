#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""POST /api/action 那一批手写动作的字段，形状写脏了不许把 Python 报错原文递到界面。

为什么单开一条：`_body_str`（字符串字段）/ `_body_list`（列表字段）把 `do_POST`
里那几条路由收口之后，源码体检只扫了 `do_POST` 的方法体，**没扫 `/api/action`
那条分派链**。于是 `_dispatch` 里还留着一整族老写法：

    want = (body.get("dir") or "").strip()          # openexports
    mod = (body.get("module") or "").strip()        # openbooks
    name = (body.get("name") or "").strip()[:40]    # folder.new / folder.rename
    s = lib_scope(lib, body.get("module"))          # 四个 action 都这么传
    add = body.get("add") or []                     # book.tag 直接 for x in add
    ids = body.get("ids") or []                     # shelf.add 直接 list(ids)

`or ""` 换不掉数字 / 列表 / 对象（非空值都是真），紧跟着的 `.strip()` 当场
`AttributeError`；`body.get("module")` 写成一个列表，`module in DIR_OF` 直接
`TypeError: unhashable type: 'list'`；`add` / `ids` 写成数字，`for` / `list()`
又是 `TypeError: 'int' object is not iterable`。

症状与订阅整理那条**一模一样**：这些动作外面套着 `_action` 的兜底 `try/except`，
所以不是断连，而是 `f"这个操作没做成：{type(e).__name__}: {str(e)[:140]}"`
把解释器的报错原文递到界面上 —— 用户点一下「新建文件夹 / 移动 / 贴标签」，
弹出来的是 `AttributeError: 'int' object has no attribute 'strip'`。
`_body_str` / `_body_list` 立下的口径是「字段级脏形状当没写」，这一族也照同一条走。

这一条钉三层：
  A 源码：`_dispatch` 里不再有裸的 `(body.get(...) or "").strip()`，`module` 一律
    先过 `_body_str` 再进 `lib_scope`，`add` / `ids` 走 `_body_list`；以后往这个
    分派器里加 action 写回旧形状会当场红（补上 `do_POST` 那条没扫到的盲区）。
  B 真机：一串脏形状打 `/api/action`，必须回完整的 200 + JSON，且回话里**不许**
    夹着 `AttributeError` / `TypeError` / `unhashable` / `not iterable` 这类
    解释器原文；该回人话的回人话。
  C 好值回归：好名字 / 好标签 / 好模块名照旧走得通（修脏值不许把正常这条路改坏）。

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
SB = pathlib.Path(tempfile.mkdtemp(prefix="gz-action-dirty-"))
atexit.register(lambda: shutil.rmtree(SB, ignore_errors=True))
for _d in ("data", "books", "shots"):
    (SB / _d).mkdir(parents=True, exist_ok=True)
os.environ["GUIZANG_DATA"] = str(SB / "data")
os.environ["GUIZANG_BOOKS"] = str(SB / "books")
os.environ["GUIZANG_SHOT_DIR"] = str(SB / "shots")
os.environ["GUIZANG_ANNA_NO_WINDOW"] = "1"

import ui_server  # noqa: E402  —— 必须在环境变量设好之后 import

SERVER = (REPO / "ui_server.py").read_text(encoding="utf-8")
# 只取 _dispatch 这一个方法：从它的定义到模块级的 def main()（分派器后面没有别的
# 缩进方法）。_action 在它前面，所以不能拿 _action 当下界。
DISPATCH = SERVER[SERVER.index("    def _dispatch(self, body):"):SERVER.index("\ndef main():")]

checks = []


def chk(name, ok, extra=""):
    checks.append((bool(ok), name, "" if ok else str(extra)[:300]))
    print(("  ok  " if ok else " FAIL ") + name + ("" if ok else "  <- " + str(extra)[:300]))


# ── A 源码：分派器里不再有裸的旧形状 ─────────────────────────────
# 这九个键在 _dispatch 里都只该是字符串。/api/action 的 body 顶层已经是对象，
# 字段级没收口才是病根。
KEYS = ("dir", "module", "name", "id", "book", "folder", "before", "from", "to")
bare = []
for k in KEYS:
    # 老写法①：`(body.get("x") or "").strip()`
    pat1 = re.compile(r'body\.get\("%s"\)\s*or\s*""\)\s*\.strip\(' % re.escape(k))
    # 老写法②：`str(body.get("x") or "").strip()`（str() 包一层不治根：认不出时
    # 把列表 / 对象字符串化成一个荒唐的名字，仍然不是「当没写」）
    pat2 = re.compile(r'str\(\s*body\.get\("%s"\)\s*or\s*""\)\s*\.strip\(' % re.escape(k))
    for m in list(pat1.finditer(DISPATCH)) + list(pat2.finditer(DISPATCH)):
        bare.append(m.group(0))
chk("_dispatch 里不再有裸的 body 字符串字段 .strip()", not bare, bare)

# module 这四个调用点都该先过 _body_str：写成列表时 `in DIR_OF` 会 unhashable。
raw_mod = re.findall(r'lib_scope\(lib,\s*body\.get\("module"\)\)', DISPATCH)
chk("module 进 lib_scope 前先过 _body_str（列表不再 unhashable）",
    not raw_mod and DISPATCH.count('lib_scope(lib, _body_str(body, "module"))') == 5,
    raw_mod or DISPATCH.count('lib_scope(lib, _body_str(body, "module"))'))

# 两个「本该是列表」的字段走 _body_list；字符串那一种特殊放行仍保留。
chk("book.tag 的 add 走 _body_list（数字不再 not iterable）",
    '_body_list(body.get("add"))' in DISPATCH and 'body.get("add") or []' not in DISPATCH,
    [l.strip() for l in DISPATCH.splitlines() if '"add"' in l][:3])
chk("shelf.add 的 ids 走 _body_list（数字不再 not iterable）",
    '_body_list(body.get("ids"))' in DISPATCH and 'body.get("ids") or []' not in DISPATCH,
    [l.strip() for l in DISPATCH.splitlines() if '"ids"' in l][:3])

# 分派器顶部那个 book 也给后面几个 action 用，同样不许裸取。
chk("分派器顶部的 book 走 _body_str",
    '_body_str(body, "book")' in DISPATCH and
    'book = (body or {}).get("book", "")' not in DISPATCH,
    [l.strip() for l in DISPATCH.splitlines() if "book =" in l][:3])

chk("_body_str / _body_list 仍各只有一处定义（一把尺子）",
    SERVER.count("def _body_str(") == 1 and SERVER.count("def _body_list(") == 1,
    (SERVER.count("def _body_str("), SERVER.count("def _body_list(")))

# 对照：旧写法遇到脏值确实会抛 —— 这组断言不是空转。
old_raised = False
try:
    (({"dir": 1} or {}).get("dir") or "").strip()
except AttributeError:
    old_raised = True
chk("对照：旧的 `(body.get(...) or \"\").strip()` 遇到数字确实 AttributeError",
    old_raised)
unhash_raised = False
try:
    ["x"] in {"weread": 1}
except TypeError:
    unhash_raised = True
chk("对照：`[\"x\"] in {模块表}` 确实 TypeError（unhashable）", unhash_raised)

# ── B / C 真机：脏形状回人话，好值照旧生效 ──────────────────────
# openbooks / openexports 会真的去开系统文件管理器：门禁里换成记账，不弹窗。
opened = []
ui_server.pc.open_in_file_manager = lambda p: opened.append(p)

srv = ThreadingHTTPServer(("127.0.0.1", 0), ui_server.Handler)
PORT = srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start()
atexit.register(srv.shutdown)
BASE = "http://127.0.0.1:%d" % PORT

# 铺一本真书进沙盒书库，让 book.tag 这类动作能走到「贴标签」那一步（而不是卡在
# 「找不到这本书」，那样就测不到 add 的迭代）。
BOOK_ID = "bk_action_dirty"
book_dir = pathlib.Path(ui_server.OUT_DIR) / "微信读书" / BOOK_ID
(book_dir / "chapters").mkdir(parents=True, exist_ok=True)
(book_dir / "chapters" / "0001.md").write_text("# 第一章\n\n正文。\n", encoding="utf-8")
(book_dir / "meta.json").write_text(
    json.dumps({"title": "脏形状体检书", "source": "weread", "done": True},
               ensure_ascii=False), encoding="utf-8")

# 解释器报错原文的特征词：脏形状回话里一个都不许出现。
PY_TEXT = ("AttributeError", "TypeError", "ValueError", "KeyError", "IndexError",
           "unhashable", "not iterable", "has no attribute", "Traceback",
           "object is not", "unexpected keyword")


def call(payload, timeout=15):
    """→ (status, body_bytes)。status 为 'ERR' 表示连接层就失败了（服务断了）。"""
    req = urllib.request.Request(BASE + "/api/action",
                                 data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        r = urllib.request.urlopen(req, timeout=timeout)
        return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()
    except Exception as e:
        return "ERR", ("%s: %s" % (type(e).__name__, e)).encode()


def act(name, payload, want_ok=None, must=None):
    """脏形状打一个动作：200 + 完整 JSON + 回话没有人话以外的解释器原文。"""
    st, body = call(payload)
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
            msg = str(obj.get("msg") or "")
            hit = [w for w in PY_TEXT if w in msg]
            if hit:
                bad = "回话里夹着解释器原文 %r：%r" % (hit, msg[:160])
            elif want_ok is not None and obj.get("ok") is not want_ok:
                bad = "ok 应为 %r：%r" % (want_ok, obj)
            elif must and any(k not in obj for k in must):
                bad = "缺字段 %r：%r" % (must, obj)
    chk(name, bad is None, bad or "")
    return bad is None


DIRTY = [123, 4.5, ["x"], {"a": 1}, True]

act("openexports dir=123：当没带，回人话（不是 AttributeError）",
    {"action": "openexports", "dir": 123}, want_ok=True)
act("openbooks module=[...]：列表不再 unhashable，退回书库根",
    {"action": "openbooks", "module": ["weread"]}, want_ok=True)
act("folder.new name=123：回「需要一个名字」",
    {"action": "folder.new", "name": 123}, want_ok=False)
act("folder.rename id={} name=[...]：回「缺少参数」",
    {"action": "folder.rename", "id": {"a": 1}, "name": ["x"]}, want_ok=False)
act("folder.drop id=123：回「没有这个文件夹」",
    {"action": "folder.drop", "id": 123}, want_ok=False)
act("book.move book=123 folder=[...]：回「找不到这本书」",
    {"action": "book.move", "book": 123, "folder": ["x"]}, want_ok=False)
act("book.order book=123 before={}：回「找不到这本书」",
    {"action": "book.order", "book": 123, "before": {"a": 1}}, want_ok=False)
act("book.tag add=123：数字不再 not iterable（书是真的，走到贴标签那步）",
    {"action": "book.tag", "book": BOOK_ID, "add": 123}, want_ok=True)
act("book.tag book=123 add=[...]：回「找不到这本书」",
    {"action": "book.tag", "book": 123, "add": ["x"]}, want_ok=False)
act("tag.rename from=123 to=[...]：回「缺少参数」",
    {"action": "tag.rename", "from": 123, "to": ["x"]}, want_ok=False)
act("tag.drop name=[...]：回「缺少参数」",
    {"action": "tag.drop", "name": ["x"]}, want_ok=False)
act("shelf.add ids=123 book=[...]：数字不再 not iterable，回人话",
    {"action": "shelf.add", "ids": 123, "book": ["x"]}, want_ok=False)
act("export_dir dir=123：当没写，回「已改回系统下载文件夹」",
    {"action": "export_dir", "dir": 123}, want_ok=True)
act("agent.save url=123 key=[] model={}：当没写，回「小 Agent 已关闭」",
    {"action": "agent.save", "url": 123, "key": [], "model": {"a": 1}}, want_ok=True)
act("ai.save url=[] key={} model=123：当没写，回人话（不抛）",
    {"action": "ai.save", "url": [], "key": {"a": 1}, "model": 123}, want_ok=True)
act("media.save engine=123 lang=[]：当没写，照旧回 ok",
    {"action": "media.save", "engine": 123, "lang": [], "url": {}, "key": 4.5,
     "model": True}, want_ok=True)
# 五个脏形状各扫一遍最典型的一条，确认没有哪种形状漏网。
for _v in DIRTY:
    act("folder.new name=%r：脏形状都不许把解释器原文递出来" % (_v,),
        {"action": "folder.new", "name": _v}, want_ok=False)

# ── C 好值回归：修脏值不许把正常这条路一起改坏 ─────────────────
st, body = call({"action": "folder.new", "name": "  资料夹  "})
good_new = False
if st == 200:
    obj = json.loads(body or b"{}")
    good_new = obj.get("ok") is True and bool(obj.get("id"))
chk("folder.new 好名字照旧建得出来（只去两头空白）", good_new, (st, body[:160]))

# 名字只去两头空白：给后面 tag.rename 用同一个口径对一下。
st, body = call({"action": "book.tag", "book": BOOK_ID, "add": "  干货  "})
tags = []
if st == 200:
    tags = (json.loads(body or b"{}").get("tags") or [])
chk("book.tag 带空白的单张标签：去掉空白后才贴（标签里不留空格）",
    tags == ["干货"] or tags[:1] == ["干货"], tags)

st, body = call({"action": "book.tag", "book": BOOK_ID, "remove": "干货"})
tags_after = []
if st == 200:
    tags_after = (json.loads(body or b"{}").get("tags") or [])
chk("book.tag remove 是好字符串时照旧摘得掉", "干货" not in tags_after, tags_after)

st, body = call({"action": "openbooks", "module": "clip"})
good_mod = False
if st == 200:
    obj = json.loads(body or b"{}")
    good_mod = obj.get("ok") is True and "剪藏" in str(obj.get("msg") or "")
chk("openbooks module='clip' 好值照旧落到剪藏那一格", good_mod, (st, body[:160]))
chk("openbooks / openexports 真去开了文件夹（好值真的走到底）", bool(opened), opened)

st, body = call({"action": "export_dir", "dir": ""})
ok_clear = st == 200 and json.loads(body or b"{}").get("ok") is True
chk("export_dir 空串照旧回「已改回系统下载文件夹」", ok_clear, (st, body[:160]))

bad = [c for c in checks if not c[0]]
print("\n/api/action 脏形状体检：%d 项通过，失败 %d 项" % (len(checks) - len(bad), len(bad)))
if bad:
    for _ok, n, x in bad:
        print("  ✗ " + n + ("  | " + x if x else ""))
sys.exit(1 if bad else 0)
