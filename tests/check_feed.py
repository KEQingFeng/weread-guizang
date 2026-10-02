# -*- coding: utf-8 -*-
"""feed 模块的离线自测：一行一条结论，失败就非零退出。

全程不联网：夹具由一个跑在 127.0.0.1 上的临时 http.server 提供，GUIZANG_DATA
指向系统临时目录下的沙盒，因此 feed.json 与「入书架」产出的书都落在沙盒里，
绝不碰用户真实的 cache/ 与书库。

跑法：.venv/bin/python tests/check_feed.py
"""
import atexit
import hashlib
import json
import os
import shutil
import sys
import tempfile
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

# GUIZANG_DATA 必须在 import feed 之前设好：feed.py 在导入时就把 DATA_DIR / STORE
# 定下来了，设晚了它还是往真实数据目录写。
SANDBOX = tempfile.mkdtemp(prefix="gz-feed-check-")
atexit.register(lambda: shutil.rmtree(SANDBOX, ignore_errors=True))
os.environ["GUIZANG_DATA"] = os.path.join(SANDBOX, "data")
os.makedirs(os.environ["GUIZANG_DATA"], exist_ok=True)

import feed  # noqa: E402

FAIL = []


def chk(name, cond, extra=""):
    print(("PASS  " if cond else "FAIL  ") + name + ("" if cond else "   " + repr(extra)))
    if not cond:
        FAIL.append(name)


# ── 夹具 ────────────────────────────────────────────────────────────
RECORD = {"paths": [], "if_none_match": []}
FAIL_PATHS = set()

PORT = 0          # 起服务后填真值
BASE = ""         # http://127.0.0.1:PORT


def _rss(body_items):
    return ('<?xml version="1.0" encoding="utf-8"?>\n'
            '<rss version="2.0" xmlns:content="http://purl.org/rss/1.0/modules/content/">'
            "<channel><title>示例订阅</title><link>%s/</link>"
            "<description>演示用源</description>%s</channel></rss>" % (BASE, body_items))


def build_routes():
    long_body = ("<p>这是一段足够长的正文，用来让 has_content 判定为真。" * 6 + "</p>")
    items = (
        "<item><title>第一篇</title><link>%s/a1</link>"
        "<guid isPermaLink=\"false\">guid-1</guid>"
        "<author>作者甲</author>"
        "<pubDate>Mon, 06 Oct 2025 10:00:00 GMT</pubDate>"
        "<description>摘要一</description>"
        "<content:encoded><![CDATA[%s"
        '<a href="/rel">相对链接</a>'
        "<script>evil()</script>"
        '<a href="javascript:alert(1)">点我</a>]]></content:encoded>'
        "</item>" % (BASE, long_body)
    ) + (
        "<item><title>第二篇</title><link>%s/a2</link>"
        "<guid isPermaLink=\"false\">guid-2</guid>"
        "<pubDate>Tue, 07 Oct 2025 10:00:00 GMT</pubDate>"
        "<description>摘要二</description></item>" % BASE
    )
    rss = _rss(items).encode("utf-8")

    atom = ('<?xml version="1.0" encoding="utf-8"?>'
            '<feed xmlns="http://www.w3.org/2005/Atom">'
            "<title>原子订阅</title><link href=\"%s/\"/>"
            '<id>atom-feed</id><updated>2025-10-06T10:00:00Z</updated>'
            "<entry><title>原子条目</title><link href=\"%s/atom1\"/>"
            "<id>atom-1</id><updated>2025-10-06T10:00:00Z</updated>"
            "<summary>原子摘要</summary></entry></feed>" % (BASE, BASE)).encode("utf-8")

    json_feed = json.dumps({
        "version": "https://jsonfeed.org/version/1.1",
        "title": "JSON 订阅",
        "home_page_url": BASE + "/",
        "items": [{
            "id": "json-1",
            "url": BASE + "/j1",
            "title": "JSON 条目",
            "content_html": "<p>JSON 正文</p>",
            "date_published": "2025-10-06T10:00:00Z",
        }],
    }, ensure_ascii=False).encode("utf-8")

    # GBK 源：XML 头里故意写 utf-8，正文其实是 gb18030 —— 很多老站就是这样，
    # 直接按 utf-8 解会得到「锟斤拷」，正好验证 _decode 的兜底。
    gbk = ('<?xml version="1.0" encoding="utf-8"?>'
           '<rss version="2.0"><channel><title>中文标题</title>'
           "<link>%s/</link><description>中文描述</description>"
           "<item><title>中文字条</title><link>%s/g1</link>"
           "<description>中文摘要</description></item>"
           "</channel></rss>" % (BASE, BASE)).encode("gb18030")

    page = ('<!doctype html><html><head><meta charset="utf-8">'
            "<title>示例站</title>"
            '<link rel="alternate" type="application/rss+xml" '
            'title="示例订阅" href="%s/feed.xml">'
            "</head><body>站点首页</body></html>" % BASE).encode("utf-8")
    site2 = ('<!doctype html><html><head><meta charset="utf-8">'
             "<title>没写链接的站</title></head><body>首页</body></html>").encode("utf-8")

    # 真实博客常这样：页脚挂一块创作共用的 <rdf:RDF> 授权声明（还裹在注释里）。
    # 以前见着 <rdf 就当成 RSS，于是整页 HTML 被当订阅源返回，页里 <link alternate>
    # 指着的真源反而用不上 —— 这个路由就是钉住那次的回归。
    blog = ('<!doctype html><html><head><meta charset="utf-8">'
            "<title>带授权声明的博客</title>"
            '<link rel="alternate" type="application/rss+xml" '
            'title="博客订阅" href="%s/feed.xml">'
            "</head><body><article>正文</article>"
            "<!--\n<rdf:RDF xmlns=\"http://web.resource.org/cc/\"\n"
            "         xmlns:dc=\"http://purl.org/dc/elements/1.1/\">\n"
            "<Work rdf:about=\"%s/\"><license rdf:resource=\"x\"/></Work>\n"
            "</rdf:RDF>\n-->"
            '<script src="%s/ad.js"></script></body></html>' % (BASE, BASE, BASE)).encode("utf-8")

    return {
        "/page": (page, "text/html; charset=utf-8", None),
        "/site2": (site2, "text/html; charset=utf-8", None),
        "/feed": (rss, "application/rss+xml", '"v1"'),
        "/feed.xml": (rss, "application/rss+xml", '"v1"'),
        "/atom.xml": (atom, "application/atom+xml", None),
        "/feed.json": (json_feed, "application/feed+json", None),
        "/gbk.xml": (gbk, "application/rss+xml", None),
        "/blog": (blog, "text/html; charset=utf-8", None),
    }


ROUTES = {}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):        # 别把每个请求都刷到 stdout
        pass

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        RECORD["paths"].append(path)
        inm = self.headers.get("If-None-Match")
        if inm:
            RECORD["if_none_match"].append(inm)
        if path in FAIL_PATHS:
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        entry = ROUTES.get(path)
        if entry is None:
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        body, ctype, etag = entry
        # 条件命中回 304：客户端带的 ETag 和我们记的一样就别再传一遍正文。
        if etag and inm == etag:
            self.send_response(304)
            self.send_header("ETag", etag)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        if etag:
            self.send_header("ETag", etag)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
PORT = httpd.server_address[1]
BASE = "http://127.0.0.1:%d" % PORT
atexit.register(lambda: httpd.shutdown())
ROUTES.update(build_routes())
threading.Thread(target=httpd.serve_forever, daemon=True).start()


# ── 1. 靠 <link rel="alternate"> 发现订阅 ───────────────────────────
found = feed.discover(BASE + "/page")
chk("发现：<link rel=alternate> 能找到订阅",
    len(found) >= 1 and found[0]["url"].endswith("/feed.xml") and found[0]["type"] == "rss", found)
chk("发现：带出了 <link> 上的标题", bool(found) and found[0]["title"] == "示例订阅", found)


# ── 2. 没写 alternate 时退回探测 /feed ──────────────────────────────
found2 = feed.discover(BASE + "/site2")
chk("发现：没写链接时探测 /feed 成功",
    len(found2) >= 1 and found2[0]["url"].endswith("/feed") and found2[0]["type"] == "rss", found2)


# ── 2.5 页脚夹着创作共用 <rdf:RDF> 的博客页：认 <link alternate>，别把 HTML 当源 ──
# 回归：真实案例是阮一峰的博客首页（页脚一块 CC 授权 RDF，裹在 HTML 注释里）。
found3 = feed.discover(BASE + "/blog")
chk("发现：页脚有 <rdf:RDF> 的博客页仍认 <link alternate>",
    len(found3) == 1 and found3[0]["url"].endswith("/feed.xml") and found3[0]["type"] == "rss", found3)
chk("发现：整页 HTML 没被当成订阅源", not any(c["url"].endswith("/blog") for c in found3), found3)
chk("判定：整页 HTML 里夹 <rdf:RDF> 不算 feed",
    feed._looks_like_feed(ROUTES["/blog"][0]) == "", "")
chk("判定：HTML 注释里的 rdf 块不算 feed",
    feed._looks_like_feed(b'<!doctype html><html><body><!-- <rdf:RDF xmlns="x"/> -->'
                          b"</body></html>") == "", "")
chk("判定：开头就是 <rdf:RDF> 的 RSS 1.0 仍算 rss",
    feed._looks_like_feed(b'<?xml version="1.0"?><rdf:RDF xmlns="http://purl.org/rss/1.0/">'
                          b"<channel><title>t</title></channel></rdf:RDF>") == "rss", "")
chk("判定：真 RSS / Atom / JSON 都还认得出",
    feed._looks_like_feed(ROUTES["/feed.xml"][0]) == "rss"
    and feed._looks_like_feed(ROUTES["/atom.xml"][0]) == "atom"
    and feed._looks_like_feed(ROUTES["/feed.json"][0]) == "json", "")


# ── 3. RSS 2.0 字段解析 ─────────────────────────────────────────────
rss_p = feed.parse_feed(ROUTES["/feed.xml"][0], BASE + "/feed.xml")
rss_titles = [e["title"] for e in rss_p["entries"]]
chk("RSS：feed 标题读到", rss_p["title"] == "示例订阅", rss_p["title"])
chk("RSS：kind 判成 rss", rss_p["kind"] == "rss", rss_p["kind"])
chk("RSS：两条条目都在", rss_titles == ["第一篇", "第二篇"], rss_titles)
first = rss_p["entries"][0]
chk("RSS：条目链接是绝对地址", first["link"] == BASE + "/a1", first["link"])
chk("RSS：作者字段读到", first["author"] == "作者甲", first["author"])
chk("RSS：pubDate 读到", "2025" in first["published"] or "Oct" in first["published"],
    first["published"])
chk("RSS：摘要非空", bool(first["summary"]), first["summary"])


# ── 4. GBK 源不乱码 ─────────────────────────────────────────────────
gbk_p = feed.parse_feed(ROUTES["/gbk.xml"][0], BASE + "/gbk.xml")
chk("GBK：标题解成中文", gbk_p["title"] == "中文标题", gbk_p["title"])
chk("GBK：没有替换符（不是乱码）", "\ufffd" not in gbk_p["title"], gbk_p["title"])
chk("GBK：条目也解对了",
    gbk_p["entries"] and gbk_p["entries"][0]["title"] == "中文字条",
    gbk_p["entries"][0]["title"] if gbk_p["entries"] else None)


# ── 5. Atom 解析 ────────────────────────────────────────────────────
atom_p = feed.parse_feed(ROUTES["/atom.xml"][0], BASE + "/atom.xml")
chk("Atom：feed 标题读到", atom_p["title"] == "原子订阅", atom_p["title"])
chk("Atom：kind 判成 atom", atom_p["kind"] == "atom", atom_p["kind"])
chk("Atom：条目读到了", bool(atom_p["entries"]) and atom_p["entries"][0]["title"] == "原子条目",
    [e["title"] for e in atom_p["entries"]])


# ── 6. JSON Feed 解析 ───────────────────────────────────────────────
json_p = feed.parse_feed(ROUTES["/feed.json"][0], BASE + "/feed.json")
chk("JSON：feed 标题读到", json_p["title"] == "JSON 订阅", json_p["title"])
chk("JSON：kind 判成 json", json_p["kind"] == "json", json_p["kind"])
chk("JSON：条目读到了", bool(json_p["entries"]) and json_p["entries"][0]["title"] == "JSON 条目",
    [e["title"] for e in json_p["entries"]])


# ── 7. 入订阅、条件请求、304 ────────────────────────────────────────
feed_info = feed.add(BASE + "/page")
fid = feed_info["id"]
chk("add：返回带 id 的源", bool(fid) and feed_info["kind"] == "rss", feed_info)
chk("add：标题自动取到", feed_info["title"] == "示例订阅", feed_info["title"])

before = feed.entries(feed_id=fid, limit=0)
before_n = len(before)
chk("add：首次入库拿到两条", before_n == 2, before_n)

RECORD["if_none_match"] = []
res = feed.refresh(fid)
after = feed.entries(feed_id=fid, limit=0)
chk("刷新：304 之后条数不变", len(after) == before_n, (before_n, len(after)))
chk("刷新：304 时 new 为 0", res["new"] == 0, res)
chk("刷新：确实发了 If-None-Match", '"v1"' in RECORD["if_none_match"], RECORD["if_none_match"])


# ── 8. guid 去重：重抓不翻倍 ────────────────────────────────────────
res2 = feed.refresh(fid, force=True)       # force 绕过条件请求，完整抓一遍
after2 = feed.entries(feed_id=fid, limit=0)
chk("去重：强刷后条数仍不变", len(after2) == before_n, (before_n, len(after2)))
chk("去重：强刷 new 为 0", res2["new"] == 0, res2)
guids = [e["id"] for e in after2]
chk("去重：没有重复 guid", len(guids) == len(set(guids)), guids)


# ── 9. 相对链接补成绝对 ─────────────────────────────────────────────
# 取「第一篇」的完整记录（列表顺序已按时间倒排，用标题定位更稳）
target = next((e for e in before if e["title"] == "第一篇"), before[0] if before else None)
full = feed.entry(target["id"])
chk("绝对化：相对链接被补成站点绝对地址",
    (BASE + "/rel") in full["content_html"], full["content_html"][:200])


# ── 10. script 与 javascript: 被清掉 ────────────────────────────────
chk("清洗：<script> 整块没了", "<script" not in full["content_html"].lower(),
    full["content_html"][:200])
chk("清洗：javascript: 链接没了", "javascript:" not in full["content_html"].lower(),
    full["content_html"][:200])
chk("清洗：正文文字仍在", "正文" in full["content_text"], full["content_text"][:120])


# ── 11. to_shelf 落成真书（meta.source == "feed"） ──────────────────
out_dir = os.path.join(SANDBOX, "books")
info = feed.to_shelf(target["id"], out_dir)
book_dir = info.get("dir", "")
meta_path = os.path.join(book_dir, "meta.json")
meta = {}
try:
    with open(meta_path, encoding="utf-8") as f:
        meta = json.load(f)
except Exception as e:
    meta = {"__err__": str(e)}
chk("入书架：书目录真建出来了", bool(book_dir) and os.path.isdir(book_dir), book_dir)
chk("入书架：meta.json 存在", os.path.isfile(meta_path), meta_path)
chk("入书架：meta.source 是 feed", meta.get("source") == "feed", meta.get("source"))
chk("入书架：meta 带 url/site/date/words",
    all(k in meta for k in ("url", "site", "date", "words", "chars")), sorted(meta.keys()))
chk("入书架：把 book id 记回了条目",
    feed.entry(target["id"])["pushed"] == info.get("id"),
    (feed.entry(target["id"])["pushed"], info.get("id")))
chk("入书架：条目顺带标成已读", feed.entry(target["id"])["read"] is True, None)


# ── 12. 404 不抛异常，收进 errors ───────────────────────────────────
atom_info = feed.add(BASE + "/atom.xml")
atom_id = atom_info["id"]
FAIL_PATHS.add("/atom.xml")
try:
    r404 = feed.refresh(atom_id)
    ok_no_raise = True
except Exception as e:  # noqa: BLE001
    r404 = {"errors": []}
    ok_no_raise = False
    print("       refresh 抛了：%r" % e)
FAIL_PATHS.discard("/atom.xml")
chk("404：刷新不抛异常", ok_no_raise, None)
chk("404：错误收进了 errors",
    len(r404.get("errors", [])) == 1 and r404["errors"][0]["feed_id"] == atom_id, r404)
chk("404：错误话是中文",
    bool(r404.get("errors")) and any("\u4e00" <= c <= "\u9fff"
                                     for c in r404["errors"][0]["error"]),
    r404.get("errors"))


# ── 收摊 ────────────────────────────────────────────────────────────
httpd.shutdown()

print()
if FAIL:
    print("失败 %d 项：%s" % (len(FAIL), "、".join(FAIL)))
    sys.exit(1)
print("全部通过")
