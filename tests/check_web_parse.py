# -*- coding: utf-8 -*-
"""web_parse 的离线自测：一行一条结论，失败就非零退出。

全程不联网。夹具由一个跑在 127.0.0.1 上的临时 http.server 提供；被测链接仍然写成
真域名的样子（x.com / xiaohongshu.com / zhihu.com），因为 platform_of 与那条
「本机、内网不替用户去敲」的护栏都要靠主机名判事 —— 真发请求的那一步被换成了
往夹具服务上打（web_parse.fetch / fetch_json 是模块级名字，正是为这一步留的接口）。
这样离线也能证明「按哪家的取法取、取出来是什么」，又不碰任何真站点。

跑法：.venv/bin/python tests/check_web_parse.py
"""
import atexit
import json
import os
import re
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

# 跟别的套件一样先把数据目录指到临时沙盒：web_parse 自己不写盘，但它顺手 import 的
# book_import 会按 GUIZANG_DATA 推路径，设晚了就可能摸到用户真实的书库。
SANDBOX = tempfile.mkdtemp(prefix="gz-webparse-check-")
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
    return bool(re.search(r"[\u4e00-\u9fff]", text or ""))


def err_of(url):
    """extract() 抛出来的那句话；没抛就回一个标记，免得把「不报错」当成通过。"""
    try:
        web_parse.extract(url)
        return "__没有抛异常__"
    except ValueError as e:
        return str(e)
    except Exception as e:  # noqa: BLE001
        return "__抛错了：%s__" % type(e).__name__


# ── 夹具 ────────────────────────────────────────────────────────────
X_TWEET_ID = "1712749080000000000"
X_DELETED_ID = "9999999999999999999"
# 由 (id/1e15*π).toString(36) 去掉 0 与小数点手算得出，并与 node（V8）核对过
X_TOKEN = "45grcv5u1rx"
X_TEXT = "归藏的离线夹具：这是一条足够长的推文正文，用来验证标题截断、作者与 Markdown 都取对了。"
X_TITLE = "归藏的离线夹具：这是一条足够长的推文正文，用来验证标题截断、作者与 Markdo"
X_IMG1 = "https://pbs.twimg.com/media/fixture-1.jpg"
X_IMG2 = "https://pbs.twimg.com/media/fixture-2.jpg"

X_JSON = json.dumps({
    "text": X_TEXT,
    "user": {"name": "示例作者", "screen_name": "demo_user"},
    "created_at": "2024-01-08T04:58:00.000Z",
    "photos": [{"url": X_IMG1}, {"url": X_IMG2}],
}, ensure_ascii=False).encode("utf-8")

# 被删/设保护的推文：syndication 接口回的是 HTML 错误页，不是 JSON
X_ERR_HTML = ("<!doctype html><html><body><h1>Something went wrong</h1>"
              "<p>Try again or visit Twitter.com.</p></body></html>").encode("utf-8")

XHS_NOTE_ID = "65f0c3a0000000001e02f4b2"
# 标题里故意留一个真的 undefined（字符串里），用来证明清洗只动「该出现值」的位置
XHS_TITLE = "夹具标题：关于 undefined 的笔记"
XHS_DESC = ("归藏的离线夹具：这一段是笔记正文，用来验证 desc 与配图都取对了，"
            "顺带看一眼话题标签 #测试[话题]# 有没有被弄坏。")
XHS_IMG1 = "https://sns-img.xhscdn.com/fixture-a.jpg"
XHS_IMG2 = "https://sns-img.xhscdn.com/fixture-b.jpg"
XHS_NICK = "示例博主"

# 这坨 state 里有裸的 undefined（JS 字面量，不是合法 JSON）——正是要洗掉的那种
XHS_STATE = ("""
{"note":{"currentNoteId":"__ID__",
 "noteDetailMap":{"__ID__":{"comments":undefined,
   "note":{"noteId":"__ID__",
     "title":"__TITLE__",
     "desc":"__DESC__",
     "time":1700000000000,
     "tagList":undefined,
     "user":{"nickname":"__NICK__"},
     "imageList":[{"urlDefault":"__IMG1__"},{"url":"__IMG2__"}]}}}}}
""").replace("__ID__", XHS_NOTE_ID).replace("__TITLE__", XHS_TITLE) \
   .replace("__DESC__", XHS_DESC).replace("__NICK__", XHS_NICK) \
   .replace("__IMG1__", XHS_IMG1).replace("__IMG2__", XHS_IMG2)

XHS_PAGE = ('<!doctype html><html><head><meta charset="utf-8"><title>小红书</title>'
            "</head><body><script>window.__INITIAL_STATE__=%s;</script>"
            "</body></html>" % XHS_STATE).encode("utf-8")

XHS_WALL = ('<!doctype html><html><head><meta charset="utf-8">'
            "<title>小红书</title></head><body>"
            "<div>打开小红书App 扫码查看笔记</div></body></html>").encode("utf-8")

ZHIHU_TITLE = "如何评价归藏的离线解析？"
ZHIHU_AUTHOR = "张三"
ZHIHU_PARA1 = "第一段：归藏的离线夹具里，这一段至少要写够二十个字，否则正文容器会被判成空块。"
ZHIHU_PARA2 = "第二段：正文要能变成 Markdown，标题与作者也要各就各位，这套断言才算真的测到了东西。"
ZHIHU_HTML = ('<!doctype html><html><head><meta charset="utf-8">'
              "<title>如何评价归藏的离线解析？ - 知乎</title>"
              '<meta property="og:title" content="%s">'
              '<meta itemprop="name" content="%s">'
              '<meta property="og:image" content="https://pic1.zhimg.com/fixture.jpg">'
              '<meta itemprop="datePublished" content="2024-03-02T10:20:30.000Z">'
              "</head><body><div class=\"QuestionAnswer-content\">"
              '<div class="RichText"><p>%s</p><p>%s</p></div>'
              "</div></body></html>" % (ZHIHU_TITLE, ZHIHU_AUTHOR,
                                        ZHIHU_PARA1, ZHIHU_PARA2)).encode("utf-8")

ZHIHU_WALL = ('<!doctype html><html><head><meta charset="utf-8">'
              "<title>知乎 - 安全验证</title></head><body>"
              "<div>系统监测到您的网络环境存在异常，请完成安全验证后继续访问</div>"
              "</body></html>").encode("utf-8")

ROUTES = {
    "/explore/" + XHS_NOTE_ID: (XHS_PAGE, "text/html; charset=utf-8"),
    "/explore/wallednote0001": (XHS_WALL, "text/html; charset=utf-8"),
    "/p/123456789": (ZHIHU_HTML, "text/html; charset=utf-8"),
    "/question/999/answer/888888": (ZHIHU_WALL, "text/html; charset=utf-8"),
}
# 未登录直接被回 403（这两家很常见）：连页面都拿不到，只能靠状态码判事
FORBIDDEN = {"/p/403000000", "/explore/forbidden0001"}

RECORD = {"paths": [], "tweet_ids": [], "tokens": []}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):        # 别把每个请求都刷到 stdout
        pass

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        query = urllib.parse.parse_qs(parsed.query)
        RECORD["paths"].append(parsed.path)
        if parsed.path in FORBIDDEN:
            body = b""
            self.send_response(403)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", "0")
            self.end_headers()
            self.wfile.write(body)
            return
        if parsed.path == "/tweet-result":
            # syndication 接口：同一个路径，靠 id 区分「正常」与「被删」
            tweet_id = (query.get("id") or [""])[0]
            RECORD["tweet_ids"].append(tweet_id)
            RECORD["tokens"].append((query.get("token") or [""])[0])
            body = X_ERR_HTML if tweet_id == X_DELETED_ID else X_JSON
            ctype = "text/html; charset=utf-8" if tweet_id == X_DELETED_ID else "application/json"
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
    """把请求主机换成夹具服务，返回 (文本, 原始地址)。

    返回的第二个值刻意用原始地址：上层拿它当 base 补绝对链接、当最终地址存进书里，
    换成 127.0.0.1 会让结果里记着一段没意义的本地地址。

    HTTP 错误要翻成带原话的 ValueError —— clip_article.fetch 就是这么包的，
    夹具照着它来，_fetch_page 里那条「401/403 说成要登录」才有真东西可测。
    """
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

X_OK_URL = "https://x.com/demo_user/status/" + X_TWEET_ID
X_BAD_URL = "https://x.com/demo_user/status/" + X_DELETED_ID
XHS_URL = "https://www.xiaohongshu.com/explore/" + XHS_NOTE_ID
XHS_WALL_URL = "https://www.xiaohongshu.com/explore/wallednote0001"
ZHIHU_URL = "https://zhuanlan.zhihu.com/p/123456789"
ZHIHU_WALL_URL = "https://www.zhihu.com/question/999/answer/888888"
ZHIHU_403_URL = "https://zhuanlan.zhihu.com/p/403000000"
XHS_403_URL = "https://www.xiaohongshu.com/explore/forbidden0001"
KEYS = {"title", "author", "site", "date", "cover", "url", "markdown", "words"}


# ── 1. platform_of 认出三家，别的站回空串 ───────────────────────────
chk("platform_of：知乎链接认成 zhihu", web_parse.platform_of(ZHIHU_URL) == "zhihu",
    web_parse.platform_of(ZHIHU_URL))
chk("platform_of：小红书链接认成 xhs", web_parse.platform_of(XHS_URL) == "xhs",
    web_parse.platform_of(XHS_URL))
chk("platform_of：X 链接认成 x",
    web_parse.platform_of(X_OK_URL) == "x"
    and web_parse.platform_of("https://twitter.com/a/status/1") == "x",
    web_parse.platform_of(X_OK_URL))
chk("platform_of：公众号 / 普通站 / 空串都回空",
    web_parse.platform_of("https://mp.weixin.qq.com/s/abc") == ""
    and web_parse.platform_of("https://example.com/a") == ""
    and web_parse.platform_of("") == "" and web_parse.platform_of(None) == "",
    [web_parse.platform_of("https://mp.weixin.qq.com/s/abc"),
     web_parse.platform_of("https://example.com/a")])


# ── 2. X：token 算对，标题/作者/正文/配图都对 ──────────────────────
chk("X：token 与手算值一致", web_parse._tweet_token(X_TWEET_ID) == X_TOKEN,
    web_parse._tweet_token(X_TWEET_ID))
RECORD["tokens"] = []
xt = web_parse.extract(X_OK_URL)
chk("X：请求带上了算出来的 token",
    X_TOKEN in RECORD["tokens"] and X_TWEET_ID in RECORD["tweet_ids"], RECORD["tokens"])
chk("X：标题取正文前 40 字", xt["title"] == X_TITLE, xt["title"])
chk("X：作者是显示名", xt["author"] == "示例作者", xt["author"])
chk("X：正文就是推文文字", X_TEXT in xt["markdown"], xt["markdown"][:80])
chk("X：配图都进了 Markdown",
    ("![](%s)" % X_IMG1) in xt["markdown"] and ("![](%s)" % X_IMG2) in xt["markdown"],
    xt["markdown"])
chk("X：封面取第一张图", xt["cover"] == X_IMG1, xt["cover"])
chk("X：日期转成了本地时间文本", bool(xt["date"]) and "2024" in xt["date"], xt["date"])


# ── 3. X：被删的推文说人话 ─────────────────────────────────────────
msg = err_of(X_BAD_URL)
chk("X：被删/设保护的推文抛中文错误", han(msg) and not msg.startswith("__"), msg)
chk("X：错误里说清了是打不开（删了或设了保护）",
    "打不开" in msg or "删" in msg or "保护" in msg, msg)
chk("X：extract_or_none 在这种页面回 None",
    web_parse.extract_or_none(X_BAD_URL) is None, None)


# ── 4. 小红书：state 里带 undefined 也要能读出来 ───────────────────
xh = web_parse.extract(XHS_URL)
chk("小红书：标题读到（含真的 undefined 字样的没被洗坏）", xh["title"] == XHS_TITLE, xh["title"])
chk("小红书：正文读到", XHS_DESC in xh["markdown"], xh["markdown"][:80])
chk("小红书：两张配图都进了 Markdown",
    ("![](%s)" % XHS_IMG1) in xh["markdown"] and ("![](%s)" % XHS_IMG2) in xh["markdown"],
    xh["markdown"])
chk("小红书：作者是昵称", xh["author"] == XHS_NICK, xh["author"])
chk("小红书：封面取第一张图", xh["cover"] == XHS_IMG1, xh["cover"])
chk("小红书：毫秒时间戳转成了日期", bool(xh["date"]) and "2023" in xh["date"], xh["date"])


# ── 5. 小红书：登录墙说人话 ─────────────────────────────────────────
msg = err_of(XHS_WALL_URL)
chk("小红书：登录墙抛中文错误", han(msg) and not msg.startswith("__"), msg)
chk("小红书：错误里提到要登录", "登录" in msg or "扫码" in msg, msg)


# ── 6. 知乎：正文容器变成 Markdown ─────────────────────────────────
zh = web_parse.extract(ZHIHU_URL)
chk("知乎：标题取自 og:title", zh["title"] == ZHIHU_TITLE, zh["title"])
chk("知乎：作者取自 itemprop=name", zh["author"] == ZHIHU_AUTHOR, zh["author"])
chk("知乎：正文两段都在 Markdown 里",
    ZHIHU_PARA1 in zh["markdown"] and ZHIHU_PARA2 in zh["markdown"], zh["markdown"][:120])
chk("知乎：封面取自 og:image", zh["cover"].endswith("fixture.jpg"), zh["cover"])
chk("知乎：日期读到了", bool(zh["date"]), zh["date"])


# ── 7. 知乎：反爬页说人话 ───────────────────────────────────────────
msg = err_of(ZHIHU_WALL_URL)
chk("知乎：验证页抛中文错误", han(msg) and not msg.startswith("__"), msg)
chk("知乎：错误里提到登录或验证", "登录" in msg or "验证" in msg, msg)

# 这两家对未登录读者常常直接回 403（实测 2025 年知乎 /hot 就是 403），
# 页面正文根本没传过来，只能靠状态码把它翻成「要登录」
msg = err_of(ZHIHU_403_URL)
chk("知乎：直接被回 403 时说成要登录", han(msg) and "登录" in msg, msg)
msg = err_of(XHS_403_URL)
chk("小红书：直接被回 403 时说成要登录", han(msg) and "登录" in msg, msg)


# ── 8. 不认识的站 ───────────────────────────────────────────────────
chk("陌生站：platform_of 回空串", web_parse.platform_of("https://example.com/x") == "", None)
chk("陌生站：extract_or_none 回 None",
    web_parse.extract_or_none("https://example.com/x") is None, None)
msg = err_of("https://example.com/x")
chk("陌生站：extract 抛中文错误", han(msg) and not msg.startswith("__"), msg)


# ── 9. 本机 / 内网地址一律不碰 ──────────────────────────────────────
for bad_url in ("http://127.0.0.1:%d/explore/%s" % (PORT, XHS_NOTE_ID),
                "http://localhost:8000/status/1",
                "http://192.168.1.1/admin",
                "file:///etc/passwd"):
    msg = err_of(bad_url)
    chk("护栏：拒掉 %s" % bad_url,
        han(msg) and not msg.startswith("__") and web_parse.extract_or_none(bad_url) is None,
        msg)
chk("护栏：拒的是本机/内网这件事本身，不是「不认识的站」",
    "内网" in err_of("http://127.0.0.1:%d/x" % PORT), err_of("http://127.0.0.1:%d/x" % PORT))


# ── 10. 键必须完全一致，且字数不为零 ────────────────────────────────
for label, art in (("X", xt), ("小红书", xh), ("知乎", zh)):
    chk("%s：返回的键与剪藏同形" % label, set(art) == KEYS, sorted(art))
    chk("%s：words 不是 0" % label, art["words"] > 0, art["words"])
# 夹具正文 48 个字符，剪藏那套字数会去掉空白，所以是 48 - 2 个空格 = 46；
# 图片引用（![](…)）整个不计。写成字面量而不是再调一次 _word_count，否则等于自己证明自己。
chk("X：words 与剪藏的算法一致（空白与图片不计）", xt["words"] == 46, xt["words"])
chk("extract_or_none：三家都能拿到", all(web_parse.extract_or_none(u) is not None
                                        for u in (X_OK_URL, XHS_URL, ZHIHU_URL)), None)


print()
if FAIL:
    print("失败 %d 项：%s" % (len(FAIL), "、".join(FAIL)))
    sys.exit(1)
print("全部通过")
