# -*- coding: utf-8 -*-
"""web_parse 的脏形状自测：外站 JSON 字段不是我们定的形状时，不许把 Python 报错弹给用户。

背景：知乎 / 小红书 / X 三家都是拿别人家的 JSON / 页面在解析，字段随时可能变形状。
`(x or "").strip()` 挡得住 None，挡不住一个数字；`for p in (x or [])` 挡得住空表，
挡不住 5。这两种写法读的是**外部数据**，一旦踩上就是
`'int' object has no attribute 'strip'` / `'int' object is not iterable`，
顺着 `/api/clip` 的 `str(e)` 原样弹到用户脸上 —— 而这一路上「脏形状」本来就是常态。

这一条钉三件事，全程离线（夹具是一个 127.0.0.1 上的临时 http.server）：
  1. 脏 JSON 只可能抛 ValueError（人话），绝不抛别的异常类型；
  2. 字段脏 ≠ 整篇作废：正文还在就照样收，作者/封面这类认不出的字段退成空；
  3. 一处收口：`_text` / `_list` / `_clip` 三个尺子真的接在两家外站字段上。

跑法：.venv/bin/python tests/check_web_parse_dirty.py
"""
import atexit
import json
import os
import shutil
import sys
import tempfile
import threading
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

# 和别的套件一样先把数据目录指到临时沙盒：web_parse 自己不写盘，但它 import 的
# book_import 会按 GUIZANG_DATA 推路径，设晚了就可能摸到用户真实的书库。
SANDBOX = tempfile.mkdtemp(prefix="gz-webparse-dirty-")
atexit.register(lambda: shutil.rmtree(SANDBOX, ignore_errors=True))
os.environ["GUIZANG_DATA"] = os.path.join(SANDBOX, "data")
os.makedirs(os.environ["GUIZANG_DATA"], exist_ok=True)

import web_parse  # noqa: E402

FAIL = []


def chk(name, cond, extra=""):
    print(("PASS  " if cond else "FAIL  ") + name + ("" if cond else "   " + repr(extra)))
    if not cond:
        FAIL.append(name)


def han(text):
    return any("\u4e00" <= ch <= "\u9fff" for ch in (text or ""))


def err_of(url):
    """extract() 抛出来的那句话；没抛就标记；抛了非 ValueError 也标记成一看就懂的样子。"""
    try:
        web_parse.extract(url)
        return "__没有抛异常__"
    except ValueError as e:
        return str(e)
    except Exception as e:  # noqa: BLE001
        return "__抛错了：%s__" % type(e).__name__


def clean_msg(msg):
    """是这个模块该给的话：中文、不是「没抛」、也不是 Python 异常类型名。"""
    return han(msg) and not msg.startswith("__")


# ── 夹具 ────────────────────────────────────────────────────────────
X_TEXT = "归藏的脏形状夹具：这段正文足够长，用来验证脏字段被跳过后正文照样收得下，标题也还有的取。"
X_IMG = "https://pbs.twimg.com/media/dirty-ok.jpg"

# id → 回包正文（全部是合法 JSON，只是形状不对）
DIRTY_X = {
    # 合法 JSON 但不是对象：json.loads 过得了，`data.get(...)` 过不了
    "1712749080000000001": b"[1,2,3]",
    "1712749080000000002": b"null",
    "1712749080000000003": b'"just a string"',
    # text 是数字：以前 `(data.get("text") or "").strip()` 抛 AttributeError
    "1712749080000000004": json.dumps(
        {"text": 12345, "user": {"name": "n", "screen_name": "s"}}).encode("utf-8"),
    # photos 是数字：以前 `for p in (… or [])` 抛 TypeError
    "1712749080000000005": json.dumps(
        {"text": X_TEXT, "user": {"name": 42, "screen_name": ["a"]},
         "photos": 7}).encode("utf-8"),
    # 字段混合脏形状：正文还在，作者认不出，配图只认得出那一条字符串
    "1712749080000000006": json.dumps(
        {"text": X_TEXT, "user": {"name": 42, "screen_name": ["a"]},
         "photos": [{"url": 7}, {"url": X_IMG}, "notadict", 5],
         "created_at": 20240108}, ensure_ascii=False).encode("utf-8"),
}

XHS_DESC = "归藏的脏形状夹具：小红书这一段的标题与配图字段都是脏的，只有正文和一条图片地址认得出来。"
XHS_IMG = "https://sns-img.xhscdn.com/dirty-ok.jpg"

XHS_STATES = {
    # 标题脏、作者脏、时间脏、配图表里混着数字与空值 —— 正文与那条真地址必须活下来
    "dirtytitle0001": {"note": {"noteDetailMap": {"dirtytitle0001": {"note": {
        "title": 123, "desc": XHS_DESC, "time": "x",
        "user": {"nickname": 9},
        "imageList": [{"urlDefault": 5}, {"urlDefault": XHS_IMG}, "s", 7,
                      {"url": None}]}}}}},
    # 正文也是数字：认不出正文就老老实实说人话，不是弹 Python 报错
    "dirtydesc0002": {"note": {"noteDetailMap": {"dirtydesc0002": {"note": {
        "title": 456, "desc": 123, "imageList": 7}}}}},
    # 配图表整个是数字：有正文就照收，只是没有配图
    "dirtylist0003": {"note": {"noteDetailMap": {"dirtylist0003": {"note": {
        "title": "正常标题", "desc": XHS_DESC, "imageList": 7}}}}},
    # noteDetailMap 里那条 note 不是对象：认不出笔记，说人话
    "dirtypick0004": {"note": {"noteDetailMap": {"dirtypick0004": {"note": [1, 2, 3]}}}},
}


def xhs_page(state):
    blob = json.dumps(state, ensure_ascii=False)
    return ('<!doctype html><html><head><meta charset="utf-8"><title>小红书</title>'
            '</head><body><script>window.__INITIAL_STATE__=%s;</script>'
            "</body></html>" % blob).encode("utf-8")


ROUTES = {"/explore/" + k: (xhs_page(v), "text/html; charset=utf-8")
          for k, v in XHS_STATES.items()}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        query = urllib.parse.parse_qs(parsed.query)
        if parsed.path == "/tweet-result":
            tweet_id = (query.get("id") or [""])[0]
            body = DIRTY_X.get(tweet_id)
            if body is None:
                self.send_response(404)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            ctype = "application/json"
        else:
            entry = ROUTES.get(parsed.path)
            if entry is None:
                self.send_response(404)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            body, ctype = entry
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
PORT = httpd.server_address[1]
atexit.register(lambda: httpd.shutdown())
threading.Thread(target=httpd.serve_forever, daemon=True).start()


def _serve(url):
    """把请求换到夹具服务上，第二个返回值保留原始地址（上层要拿它当最终地址）。"""
    u = urllib.parse.urlparse(url)
    target = "http://127.0.0.1:%d%s" % (PORT, u.path or "/")
    if u.query:
        target += "?" + u.query
    try:
        with urllib.request.urlopen(target, timeout=5) as r:
            return r.read().decode("utf-8", errors="replace"), url
    except urllib.error.HTTPError as e:
        raise ValueError("打不开这个页面：%s" % str(e)[:120])


web_parse.fetch = _serve
web_parse.fetch_json = _serve


def x_url(tid):
    return "https://x.com/demo/status/" + tid


def xhs_url(nid):
    return "https://www.xiaohongshu.com/explore/" + nid


# ── 1. X：三种「合法 JSON 但不是对象」只许说人话 ─────────────────────
for tid, raw in (("1712749080000000001", "[1,2,3]"),
                 ("1712749080000000002", "null"),
                 ("1712749080000000003", '"just a string"')):
    msg = err_of(x_url(tid))
    chk("X：回包是 %s 时抛的是人话（不是 Python 报错）" % raw, clean_msg(msg), msg)
    chk("X：回包是 %s 时 extract_or_none 回 None" % raw,
        web_parse.extract_or_none(x_url(tid)) is None, None)


# ── 2. X：字段脏 ≠ 整篇作废 ─────────────────────────────────────────
# text 是数字 → 没有正文，按「打不开」说人话；关键是那句话不能是异常类型名
msg = err_of(x_url("1712749080000000004"))
chk("X：text 是数字时说人话", clean_msg(msg), msg)

# photos 是数字 → 正文照收，配图当没有
art = web_parse.extract(x_url("1712749080000000005"))
chk("X：photos 是数字时正文照样收下", art["markdown"].strip() == X_TEXT, art["markdown"][:80])
chk("X：photos 是数字时作者退成空串", art["author"] == "", art["author"])
chk("X：photos 是数字时封面为空", art["cover"] == "", art["cover"])
chk("X：photos 是数字时返回的键与剪藏同形",
    set(art) == {"title", "author", "site", "date", "cover", "url", "markdown", "words"},
    sorted(art))

# 字段混合脏形状 → 只认字符串，认得出来的那条配图要留下
art = web_parse.extract(x_url("1712749080000000006"))
chk("X：作者字段脏时退成空串而不是 42", art["author"] == "", art["author"])
chk("X：配图只收字符串那条", ("![](%s)" % X_IMG) in art["markdown"]
    and "![](" + "7" not in art["markdown"], art["markdown"])
chk("X：封面取那条认得出的图", art["cover"] == X_IMG, art["cover"])
chk("X：时间字段是数字时退成空串", art["date"] == "", art["date"])
chk("X：正文一个字节都没丢", X_TEXT in art["markdown"], art["markdown"][:80])


# ── 3. 小红书：脏字段只许说人话 / 正文照收 ──────────────────────────
art = web_parse.extract(xhs_url("dirtytitle0001"))
chk("小红书：标题是数字时退回正文前 40 字",
    art["title"] == XHS_DESC[:40], art["title"])
chk("小红书：昵称是数字时作者退成空串", art["author"] == "", art["author"])
chk("小红书：正文照收", XHS_DESC in art["markdown"], art["markdown"][:80])
chk("小红书：配图只收字符串那条", ("![](%s)" % XHS_IMG) in art["markdown"], art["markdown"])
chk("小红书：封面取那条认得出的图", art["cover"] == XHS_IMG, art["cover"])
chk("小红书：时间是字符串时日期退成空串", art["date"] == "", art["date"])

msg = err_of(xhs_url("dirtydesc0002"))
chk("小红书：正文是数字时说人话", clean_msg(msg), msg)

art = web_parse.extract(xhs_url("dirtylist0003"))
chk("小红书：配图表是数字时有正文就照收", XHS_DESC in art["markdown"], art["markdown"][:80])
chk("小红书：配图表是数字时封面为空", art["cover"] == "", art["cover"])
chk("小红书：配图表是数字时标题还在", art["title"] == "正常标题", art["title"])

msg = err_of(xhs_url("dirtypick0004"))
chk("小红书：笔记本身不是对象时说人话", clean_msg(msg), msg)


# ── 4. 三把尺子本身：认不出的形状退成空，别把一坨 JSON 当正文 ────────
chk("_text：只认字符串", web_parse._text("a") == "a" and web_parse._text(5) == ""
    and web_parse._text(None) == "" and web_parse._text({"a": 1}) == "",
    [web_parse._text("a"), web_parse._text(5), web_parse._text({"a": 1})])
chk("_clip：按字符截断，非字符串当空", web_parse._clip("abcdef", 3) == "abc"
    and web_parse._clip(12345, 3) == "", [web_parse._clip("abcdef", 3), web_parse._clip(1, 3)])
chk("_list：只认列表与元组", web_parse._list([1]) == [1] and web_parse._list((1,)) == (1,)
    and web_parse._list("abc") == [] and web_parse._list(5) == [] and web_parse._list(None) == [],
    [web_parse._list("abc"), web_parse._list(5)])
chk("_one_line：数字 / None 不抛，正常文本照旧",
    web_parse._one_line(5) == "" and web_parse._one_line(None) == ""
    and web_parse._one_line("a\n  b") == "a b",
    [web_parse._one_line(5), web_parse._one_line("a\n  b")])
chk("_images_md：数字与空值不拼进 Markdown，只拼字符串",
    web_parse._images_md([5, None, "u", ""]) == "\n\n![](u)",
    web_parse._images_md([5, None, "u", ""]))
chk("_fmt_epoch：列表这类认不出的值回空串", web_parse._fmt_epoch([1]) == "")
chk("_fmt_iso：数字回空串而不是报错", web_parse._fmt_iso(20240108) == "")
try:
    web_parse._guard(123)
    guard_msg = "__没有抛异常__"
except ValueError as e:
    guard_msg = str(e)
except Exception as e:  # noqa: BLE001
    guard_msg = "__抛错了：%s__" % type(e).__name__
chk("_guard：非字符串回人话", guard_msg == "链接是空的", guard_msg)

# ── 5. 一处实现多处接线（尺子真的接在两家外站字段上）─────────────────
SRC = (REPO / "web_parse.py").read_text(encoding="utf-8")
for label, wire in (
        ("X 的回包先验对象", "isinstance(data, dict)"),
        ("X 的 text 过 _text", '_text(data.get("text"))'),
        ("X 的 photos 过 _list", '_list(data.get("photos"))'),
        ("小红书的 desc 过 _text", '_text(note.get("desc"))'),
        ("小红书的 imageList 过 _list", '_list(note.get("imageList"))'),
        ("统一出口的标题过 _clip", '"title": _clip(title, 120)'),
):
    chk("接线：" + label, wire in SRC)

print()
if FAIL:
    print("失败 %d 项：%s" % (len(FAIL), "、".join(FAIL)))
    sys.exit(1)
print("全部通过")
