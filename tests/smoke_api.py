# -*- coding: utf-8 -*-
"""接口冒烟：把界面上每一条后端调用真打一遍，专找「无响应 / 500 / 谎报」。

为什么留这一份、而且自己起服务：2026-10-04 用户报的三条（画板没反应、思维导图没反应、
flomo 导入包没反应）都不是界面缺按钮，也不是后端缺实现 —— 是端口上挂着旧进程。这类毛病
单看一个接口看不出来，得「每一条都打一次」才看得见谁没接住。别的真机套件各管一屏，
这一份管的是接口层那张网：GET 路由逐个有回声、三条被点名的功能走到底、
安全动作能改能撤、坏输入不许把连接断掉。

自己起服务、自己铺书架（tests/seed.py 打到本套件的临时目录）、跑完整个包删掉：
冒烟会写画板、导图、标签、文件夹和便签账本，跟门禁那份共享沙盒混在一起会让别的套件
看到不该看到的格子。用户真实的 cache/ 与 ~/Documents/归藏 一个字节都不碰。

便签正文全部来自 tests/flomo_fixture.py（现编的），断言的是形状与条数，不是任何人的原话。
界面怎么说这些话归 tests/check_swapbar.py，真换班归 tests/test_backend_identity.py。
"""
import atexit
import base64
import csv
import io
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
import urllib.error
import urllib.parse
import urllib.request

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
REPO = HERE.parent
from flomo_fixture import N_MEMOS, export_zip  # noqa: E402

FAIL = []
PASSED = [0]
SANDBOX = tempfile.mkdtemp(prefix="gz-smoke-")
atexit.register(lambda: shutil.rmtree(SANDBOX, ignore_errors=True))


def note(ok, name, detail=""):
    if ok:
        PASSED[0] += 1
    print(("PASS  " if ok else "FAIL  ") + name + ("" if ok else "   " + str(detail)[:200]))
    if not ok:
        FAIL.append(name)


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


# ── 沙盒：先铺书架再起服务（冒烟要点一本真在架上的书）──────────────
PY = sys.executable
env = dict(os.environ, GUIZANG_SELFTEST_DIR=SANDBOX,
           GUIZANG_DATA=os.path.join(SANDBOX, "data"),
           GUIZANG_BOOKS=os.path.join(SANDBOX, "books"),
           GUIZANG_SHOT_DIR=os.path.join(SANDBOX, "shots"))
for d in ("data", "books", "shots"):
    os.makedirs(os.path.join(SANDBOX, d), exist_ok=True)
subprocess.run([PY, str(HERE / "seed.py"), "--force"], cwd=str(REPO), env=env,
               stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT, check=True)

srv_port = free_port()
logp = os.path.join(SANDBOX, "server.log")
logf = open(logp, "w")
srv = subprocess.Popen([PY, "ui_server.py", "--port", str(srv_port)],
                       cwd=str(REPO), env=env, stdout=logf, stderr=subprocess.STDOUT)
atexit.register(lambda: srv.terminate())
BASE = ""
for _ in range(160):
    try:
        m = re.search(r"http://127\.0\.0\.1:(\d+)", open(logp).read())
        if m:
            cand = "http://127.0.0.1:%s" % m.group(1)
            json.loads(urllib.request.urlopen(cand + "/api/state", timeout=1).read())
            BASE = cand
            break
    except Exception:
        pass
    time.sleep(0.25)
if not BASE:
    print("沙盒服务没起来：", open(logp).read()[-1500:])
    sys.exit(1)
print("冒烟沙盒：", BASE)


def call(path, payload=None, timeout=25):
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


def chk(name, path, payload=None, want=200, ok_field=None, must=None):
    """打一次，看状态码、看 ok、看该有字段有没有、看回包里有没有藏着异常字样。

    「没响应」在界面上就是「点了没反应」，所以这里连 ERR（连接被断）也算失败 ——
    后端可以回一句「这事儿办不了」，不可以把连接掐了。
    """
    st, body = call(path, payload)
    bad = None
    if st != want:
        bad = "HTTP %s（要 %s）%r" % (st, want, body[:90])
    else:
        try:
            d = json.loads(body)
        except Exception:
            d = None
        if ok_field is not None and isinstance(d, dict) and bool(d.get("ok")) != ok_field:
            bad = "ok=%s（要 %s）msg=%s" % (d.get("ok"), ok_field, d.get("msg"))
        elif must and isinstance(d, dict):
            miss = [k for k in must if k not in d]
            if miss:
                bad = "回包缺字段 %s" % miss
            elif any("Exception" in str(v) or "Traceback" in str(v) for v in d.values()):
                bad = "回包里有异常字样"
    note(bad is None, name, bad or "")
    return body


print("── GET 路由：每一条都得有回声 ────────────────────────────")
_first = json.loads(call("/api/state")[1])["books"][0]
book = _first["id"]
# 夹子、标签这些账本按「一格一份账」记：归夹时后端认的是这本书自己住哪一格，
# 不信前端传的模块名（前端那个标签可能是上一次刷新留下的）。所以本套件建夹子
# 也得建在这本书那一格里，否则「book.move」被拒是对的 —— 拒错了才是 bug。
bmod = _first.get("module") or "weread"
q = "?book=" + book
for name, path, want in [
    ("state", "/api/state", 200),
    ("feed 清单", "/api/feed", 200),
    ("flomo 列表", "/api/flomo/notes", 200),
    ("flomo 附件（不存在要说没有）", "/api/flomo/att/nope.png", 404),
    ("视频工作台", "/api/video", 200),
    ("阅读统计", "/api/readstat", 200),
    ("日志", "/api/log", 200),
    ("封面（无 Key 时 404，前端自己画）", "/api/cover" + q, 404),
    ("MCP 提示词", "/api/mcp", 200),
    ("笔记索引状态", "/api/notes_index", 200),
    ("随机漫步", "/api/notes_random", 200),
    ("详情", "/api/detail" + q, 200),
    ("正文", "/api/read" + q, 200),
    ("md", "/api/md" + q, 200),
    ("我的笔记", "/api/mynotes" + q, 200),
    ("笔记地图", "/api/mynotes/map" + q, 200),
    ("模板", "/api/note_tpl", 200),
    ("画板清单", "/api/board" + q, 200),
    ("AI 小结（缓存）", "/api/ai/summary" + q + "&ch=0001", 200),
    ("思维导图", "/api/mindmap" + q, 200),
]:
    chk(name, path, None, want)

print()
print("── 画板（用户报的第一条）────────────────────────────────")
b = json.loads(chk("board 新建", "/api/board", {"act": "new", "book": book}, 200, True))
doc = b["board"]
doc["objects"] = [{"type": "rect", "left": 10, "top": 10, "width": 40, "height": 30,
                   "fill": "#2b8cf0"}]
chk("board 存", "/api/board", {"act": "save", "book": book, "doc": doc}, 200, True)
SVG = '<svg xmlns="http://www.w3.org/2000/svg"><rect width="9" height="9"/></svg>'
chk("board 导出 svg（裸文本，同 fabric toSVG）", "/api/board",
    {"act": "export", "book": book, "id": doc["id"], "fmt": "svg", "data": SVG}, 200, True)
chk("board 导出 svg（dataURL，同 toDataURL）", "/api/board",
    {"act": "export", "book": book, "id": doc["id"], "fmt": "svg",
     "data": "data:image/svg+xml;base64," + base64.b64encode(SVG.encode()).decode()}, 200, True)
chk("board 导出伪 SVG 要拒", "/api/board",
    {"act": "export", "book": book, "id": doc["id"], "fmt": "svg", "data": "hello"}, 200, False)
chk("board 转笔记", "/api/board", {"act": "md", "book": book, "id": doc["id"]}, 200, True,
    must=["md", "has_image"])
chk("board 读回", "/api/board" + q + "&id=" + doc["id"], None, 200, True)
chk("board 删", "/api/board", {"act": "delete", "book": book, "id": doc["id"]}, 200, True)
chk("board 不存在的书要说没有", "/api/board", {"act": "new", "book": "no_such_book"}, 200, False)

print()
print("── 思维导图（用户报的第二条）────────────────────────────")
mm = {"schema": 1, "title": "冒烟", "form": "tree",
      "nodes": [{"id": "n1", "text": "根", "x": 100, "y": 100},
                {"id": "n2", "text": "枝", "x": 260, "y": 140}],
      "links": [{"from": "n1", "to": "n2"}]}
chk("mindmap 存", "/api/mindmap", {"act": "save", "book": book, "doc": mm}, 200, True)
chk("mindmap 读回", "/api/mindmap" + q, None, 200, True, must=["doc"])
chk("mindmap 出 svg", "/api/mindmap", {"act": "svg", "book": book, "doc": mm}, 200, True)

print()
print("── flomo 导入（用户报的第三条）──────────────────────────")
blob = export_zip()
d = json.loads(chk("flomo 导入 zip", "/api/flomo/notes",
                   {"act": "import", "name": "memos.zip",
                    "data": base64.b64encode(blob).decode()}, 200, True))
if d.get("added") != N_MEMOS:
    note(False, "flomo 导入条数对得上", "%s ≠ %s" % (d.get("added"), N_MEMOS))
else:
    note(True, "flomo 导入条数对得上")
print("     导入 %s 条 / 已在账 %s / 图 %s" % (d.get("added"), d.get("existed"), d.get("atts")))
d2 = json.loads(chk("flomo 再导一次（去重）", "/api/flomo/notes",
                    {"act": "import", "name": "memos.zip",
                     "data": base64.b64encode(blob).decode()}, 200, True))
note(d2.get("added") == 0, "重复导入没再加条目", "又加了 %s 条" % d2.get("added"))
chk("flomo 列表有内容", "/api/flomo/notes", None, 200, True)
mid = json.loads(call("/api/flomo/notes")[1])["memos"][0]["id"]
chk("flomo 收成书", "/api/flomo/notes", {"act": "shelf", "id": mid}, 200, True)
chk("flomo 记忆画像", "/api/flomo/notes?mode=portrait", None, 200, True,
    must=["portrait", "md"])
chk("flomo 忘一条", "/api/flomo/notes", {"act": "forget", "id": mid}, 200, True)
chk("flomo 空文件要说没有", "/api/flomo/notes", {"act": "import", "data": ""}, 200, False)
chk("flomo 坏文件要说没有", "/api/flomo/notes",
    {"act": "import", "name": "x.zip", "data": base64.b64encode(b"not a zip").decode()},
    200, False)

print()
print("── 划线笔记导出 CSV（勾选范围 → flomo 导入格式）───────────")
# 「导出 CSV」那颗钮在划线笔记那一屏，而那一屏要真 Key 才进得去（沙盒里没有），
# 所以把这一条搬到接口层真跑一遍 —— 这里验的正是最容易假成功的那一段：回了 ok，
# 文件到底写没写下去、写下去的字节对不对。只测落盘，不看界面文案（那归 check_notes_csv）。
_rows_in = [{"text": '第一条：逗号, 和引号"x"', "at": 1756864800},
            {"text": "第二条\n中间有换行", "at": 0}]
cd = json.loads(chk("notes_csv 导得出", "/api/notes_csv",
                    {"title": "冒烟·测试书", "items": _rows_in}, 200, True,
                    must=["n", "name", "dir"]))
note(cd.get("n") == 2, "notes_csv 回执条数对得上", cd.get("n"))
_csvp = os.path.join(cd.get("dir") or "", cd.get("name") or "")
try:
    with open(_csvp, "rb") as f:
        _raw = f.read()
except Exception as e:
    _raw = b""
    print("     （读不到导出文件：%s）" % str(e)[:80])
note(_raw[:3] == b"\xef\xbb\xbf", "落盘的 CSV 带 UTF-8 BOM", _raw[:3])
note(b"\r\n" in _raw and b"\n" not in _raw.replace(b"\r\n", b""),
     "落盘的 CSV 每行以 CRLF 收尾")
note(_raw[3:3 + len("content,created_at")] == b"content,created_at",
     "落盘的 CSV 表头是 content,created_at", _raw[3:25])
_rb = list(csv.reader(io.StringIO(_raw.decode("utf-8-sig"))))
note(len(_rb) == 3 and _rb[0] == ["content", "created_at"], "解析回来两行数据", _rb[:1])
note(_rb[1][1] == "2025-09-03 10:00:00" and _rb[2][1] == "",
     "时间列按 YYYY-MM-DD HH:MM:SS 写、空时间留空", [_r[1] for _r in _rb[1:]])
note("逗号" in _rb[1][0] and '"x"' in _rb[1][0] and "换行" in _rb[2][0],
     "逗号 / 引号 / 换行都原样保住（转义对了）", [_r[0] for _r in _rb[1:]])
chk("notes_csv 空选要说没有", "/api/notes_csv", {"title": "x", "items": []}, 200, False)
chk("notes_csv 全是空条目（滤完就没）也要说没有", "/api/notes_csv",
    {"title": "x", "items": [{"at": 1}, {"text": "   "}]}, 200, False)
_c2 = json.loads(chk("notes_csv 连导两次", "/api/notes_csv",
                     {"title": "冒烟·测试书", "items": _rows_in}, 200, True))
note(_c2.get("name") != cd.get("name"), "同名再导不覆盖，自动加了序号",
     (cd.get("name"), _c2.get("name")))

print()
print("── 安全的 POST 动作（跳过会联网 / 删库 / 起长任务的）──────")
call("/api/action", {"action": "log.clear"})
fid = json.loads(chk("folder.new", "/api/action",
                     {"action": "folder.new", "name": "冒烟格", "module": bmod},
                     200, True))["id"]
chk("book.tag 贴标签", "/api/action",
    {"action": "book.tag", "book": book, "add": ["冒烟"]}, 200, True, must=["tags"])
chk("book.move 归夹", "/api/action",
    {"action": "book.move", "book": book, "folder": fid}, 200, True)
chk("book.move 回未归类", "/api/action",
    {"action": "book.move", "book": book, "folder": ""}, 200, True)
# 跨模块不许归：夹子建在别的格子里，后端要拦得明明白白（而不是默默把书挪走或断线）。
other = "clip" if bmod != "clip" else "weread"
ofid = json.loads(chk("folder.new（另一格）", "/api/action",
                      {"action": "folder.new", "name": "冒烟跨格", "module": other},
                      200, True))["id"]
d = json.loads(chk("book.move 跨格要被拒", "/api/action",
                   {"action": "book.move", "book": book, "folder": ofid}, 200, False))
note("这个模块里没有这个文件夹" in (d.get("msg") or ""),
     "book.move 跨格说的是这句原因", (d.get("msg") or "")[:80])
chk("book.order", "/api/action", {"action": "book.order", "book": book, "before": book},
    200, True)
chk("tag.rename", "/api/action",
    {"action": "tag.rename", "from": "冒烟", "to": "冒烟2", "module": bmod}, 200, True)
chk("tag.drop", "/api/action",
    {"action": "tag.drop", "name": "冒烟2", "module": bmod}, 200, True)
chk("folder.drop", "/api/action",
    {"action": "folder.drop", "id": fid, "module": bmod}, 200, True)
chk("folder.drop 再删一次要说没有", "/api/action",
    {"action": "folder.drop", "id": fid, "module": bmod}, 200, False)
chk("folder.drop（另一格）收干净", "/api/action",
    {"action": "folder.drop", "id": ofid, "module": other}, 200, True)
chk("未知动作兜底", "/api/action", {"action": "no.such.action"}, 200, False)
chk("缺参数兜底", "/api/action", {}, 200, False)
chk("坏 book 也不能断线", "/api/action", {"action": "book.tag", "book": "!!!"}, 200, None)
chk("空 body 的 board", "/api/board", {}, 200, False)
chk("没 act 的 board", "/api/board", {"book": book}, 200, False)

print()
print("── 路径穿越与坏输入 ─────────────────────────────────────")
for bad in ("../../etc/passwd", "/absolute/path", "....//....//x"):
    chk("穿越 book=" + bad[:16], "/api/md?book=" + urllib.parse.quote(bad), None, 404)

print()
if FAIL:
    print("冒烟有 %d 处没接住：%s" % (len(FAIL), "、".join(FAIL)))
else:
    print("冒烟 %d 条：接口层没发现「无响应 / 500 / 谎报」。" % PASSED[0])
sys.exit(len(FAIL))
