# -*- coding: utf-8 -*-
"""feed 模块的离线自测：一行一条结论，失败就非零退出。

全程不联网：夹具由一个跑在 127.0.0.1 上的临时 http.server 提供，GUIZANG_DATA
指向系统临时目录下的沙盒，因此 feed.json 与「入书架」产出的书都落在沙盒里，
绝不碰用户真实的 cache/ 与书库。

跑法：.venv/bin/python tests/check_feed.py
"""
import atexit
import datetime
import hashlib
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import urllib.parse
import xml.etree.ElementTree as ET
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


def raises(fn, *args, **kw):
    """期望它抛 ValueError（模块里给人话报错都用这个类型），其它异常算不合格。"""
    try:
        fn(*args, **kw)
    except ValueError:
        return True
    except Exception as e:  # noqa: BLE001
        print("       抛错了类型：%r" % e)
        return False
    return False


def store_raw():
    """直接读盘上的 feed.json：验「真的写下去了」，不能只信函数返回值。"""
    with open(feed.STORE, encoding="utf-8") as f:
        return json.load(f)


def all_entries():
    raw = store_raw()
    return [e for lst in raw.get("entries", {}).values() for e in lst or []]


# ── 夹具 ────────────────────────────────────────────────────────────
RECORD = {"paths": [], "if_none_match": []}
FAIL_PATHS = set()
SLOW = {"delay": 0.0}       # /slow.xml 故意拖这么久：拿它模拟「刷新还在抓」的那段时间

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
        # 并发回归用的两个源：一个故意慢，一个正常快。
        "/slow.xml": (_rss("<item><title>慢半拍</title><link>%s/s1</link>"
                           "<guid isPermaLink=\"false\">slow-1</guid>"
                           "<description>摘要</description></item>" % BASE).encode("utf-8"),
                      "application/rss+xml", None),
        "/fast.xml": (_rss("<item><title>快枪手</title><link>%s/f1</link>"
                           "<guid isPermaLink=\"false\">fast-1</guid>"
                           "<description>摘要</description></item>" % BASE).encode("utf-8"),
                      "application/rss+xml", None),
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
        if path == "/slow.xml" and SLOW["delay"]:
            time.sleep(SLOW["delay"])       # 卡在抓取里，让主线程有机会插一刀写盘
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


# ── 13. 分组与排序 ─────────────────────────────────────────────────
row = feed.set_feed_group(fid, "技术")
chk("分组：源能进组（group 新键、folder 老键同步）",
    row.get("group") == "技术" and row.get("folder") == "技术", row)
feed.set_feed_group(atom_id, "技术")
chk("分组：按组取源", len(feed.subs(group="技术")) == 2, feed.subs(group="技术"))
chk("分组：group=\"\" 是「未分组」，None 才是不过滤",
    feed.subs(group="") == [] and len(feed.subs()) == 2, feed.subs(group=""))
gs = {r["group"]: r for r in feed.groups()}
chk("分组：每组回未读数与总数",
    gs["技术"]["feeds"] == 2 and gs["技术"]["total"] == 3 and gs["技术"]["unread"] == 2, gs)
chk("分组：组里逐源带未读明细",
    len(gs["技术"]["items"]) == 2 and all("unread" in it and "title" in it
                                          for it in gs["技术"]["items"]), gs["技术"]["items"])
feed.group_create("生活")
chk("分组：建了但还没挂源的组也记得住", "生活" in {r["group"] for r in feed.groups()}, None)
ren = feed.group_rename("技术", "前沿科技")
chk("分组：改组名连组里的源一起改",
    ren["feeds"] == 2 and "技术" not in {r["group"] for r in feed.groups()}
    and len(feed.subs(group="前沿科技")) == 2, ren)
dgrp = feed.group_delete("生活")
chk("分组：删组只散组、不删订阅",
    dgrp["removed"] == 0 and len(feed.subs()) == 2
    and "生活" not in {r["group"] for r in feed.groups()}, dgrp)
ids = feed.reorder(atom_id, index=0)
chk("排序：挪到最前", ids[0] == atom_id and feed.subs()[0]["id"] == atom_id, ids)
ids2 = feed.reorder(fid, after=atom_id)
chk("排序：插到某个源后面", ids2[:2] == [atom_id, fid], ids2)
chk("排序：order 是写进盘上的整数",
    all(isinstance(f.get("order"), int) for f in store_raw()["feeds"]), store_raw()["feeds"])
sm = feed.summary()
chk("总览：源数 / 条数 / 未读对得上",
    sm["feeds"] == 2 and sm["entries"] == 3 and sm["unread"] == 2
    and sm["groups"][0]["group"] == "前沿科技" and sm["groups"][0]["unread"] == 2, sm)


# ── 14. 收藏与稍后读 ───────────────────────────────────────────────
rss_rows = feed.entries(feed_id=fid, limit=0)
by_title = {r["title"]: r for r in rss_rows}
e1, e2 = by_title["第一篇"], by_title["第二篇"]
both = [e1["id"], e2["id"]]
st = feed.toggle_star(e2["id"])
chk("收藏：toggle 打得开", st["starred"] is True and feed.entry(e2["id"])["starred"] is True, st)
chk("收藏：列表能只回收藏", [r["id"] for r in feed.entries(starred=True)] == [e2["id"]], None)
chk("收藏：批量只算真改动的条数", feed.mark_star(both, starred=True) == 1, None)
chk("收藏：再 toggle 一次是取消", feed.toggle_star(e2["id"])["starred"] is False, None)
feed.mark_star(both, starred=False)
chk("收藏：能整批取消", feed.entries(starred=True, limit=0) == [], None)
feed.mark_star(e1["id"], True)
lt = feed.toggle_later(e2["id"])
chk("稍后读：toggle 带上加入时间", lt["later"] is True and lt["later_at"] > 0, lt)
chk("稍后读：视图里 later 是布尔、later_at 是时间戳",
    feed.entry(e2["id"])["later"] is True and feed.entry(e2["id"])["later_at"] > 0, None)
chk("稍后读：只回稍后读的那几条", [r["id"] for r in feed.entries(later=True)] == [e2["id"]], None)
chk("稍后读：批量取出", feed.mark_later(both, later=False) == 1
    and feed.entries(later=True, limit=0) == [], None)
feed.toggle_later(e2["id"])
flat = all_entries()
chk("收藏/稍后读：状态真写进盘了",
    any(e.get("starred") for e in flat) and any(e.get("later") for e in flat), None)


# ── 15. 标签 ───────────────────────────────────────────────────────
chk("标签：批量打上多个标签",
    feed.tag_add(both, ["AI", "读法"]) == 2 and set(feed.entry(e1["id"])["tags"]) == {"AI", "读法"},
    feed.entry(e1["id"]))
chk("标签：重复打不翻倍也不算改动",
    feed.tag_add(e1["id"], ["AI"]) == 0 and feed.entry(e1["id"])["tags"].count("AI") == 1,
    feed.entry(e1["id"])["tags"])
chk("标签：清单带条数与未读数",
    any(r["tag"] == "AI" and r["count"] == 2 and r["unread"] >= 1 for r in feed.tag_list()),
    feed.tag_list())
chk("标签：按标签筛", {r["id"] for r in feed.entries(tag="AI")} == set(both), None)
chk("标签：多标签默认是「或」", len(feed.entries(tag=["AI", "没挂过的"])) == 2, None)
chk("标签：tag_match=all 是「且」", len(feed.entries(tag=["AI", "读法"], tag_match="all")) == 2, None)
chk("标签：「且」遇上没挂的组合就空", feed.entries(tag=["AI", "没挂过的"], tag_match="all") == [], None)
chk("标签：摘掉其中一个", feed.tag_remove(e1["id"], ["读法"]) == 1
    and feed.entry(e1["id"])["tags"] == ["AI"], feed.entry(e1["id"])["tags"])
chk("标签：单条整盘替换", feed.tag_set(e1["id"], ["语言模型", "AI"]) == ["语言模型", "AI"], None)
chk("标签：一条的标签能清空", feed.tag_remove(e1["id"]) == 1
    and feed.entry(e1["id"])["tags"] == [], feed.entry(e1["id"]))
tags_now = [r["tag"] for r in feed.tag_list()]
chk("标签：不再挂任何条目的标签自己就消失了（没有悬空标签表）",
    "读法" in tags_now and "语言模型" not in tags_now, tags_now)
feed.tag_add(both, ["AI"])


# ── 16. 搜索与筛选 ─────────────────────────────────────────────────
OLD_KEYS = {"id", "feed_id", "feed_title", "title", "link", "author", "published",
            "summary", "has_content", "read", "pushed"}
view = feed.entries(limit=1)[0]
chk("兼容：条目视图里老字段一个不少", OLD_KEYS <= set(view), sorted(OLD_KEYS - set(view)))
chk("兼容：老参数 unread_only 还管用",
    len(feed.entries(unread_only=True, limit=0)) == feed.summary()["unread"], None)
chk("搜索：q 命中标题", [r["title"] for r in feed.entries(q="第二篇")] == ["第二篇"], None)
chk("搜索：q 搜到正文里的词（不只标题摘要）",
    [r["title"] for r in feed.entries(q="足够长的正文")] == ["第一篇"], None)
chk("搜索：in_content=False 退回只搜标题摘要",
    feed.entries(q="足够长的正文", in_content=False) == [], None)
chk("搜索：大小写不敏感",
    len(feed.entries(q="Tokenizer")) == 0, None)      # 先确认没这种东西，下面塞一条
data = feed._load()
data["entries"][fid].append({"id": "case-test", "title": "Tokenizer 杂谈",
                             "link": BASE + "/case", "summary": "聊聊分词", "tags": [],
                             "published": "Wed, 08 Oct 2025 09:00:00 GMT",
                             "published_ts": 1759914000, "content_html": "<p>关于 tokenizer</p>",
                             "content_md": "", "has_content": False, "read": False,
                             "starred": False, "later": 0, "pushed": "", "fetched_at": 1759914000})
feed._save(data)
chk("搜索：同一个关键词换大小写都搜得到",
    [r["id"] for r in feed.entries(q="tokenizer")] == ["case-test"]
    and [r["id"] for r in feed.entries(q="TOKENIZER")] == ["case-test"], None)
feed.delete_entries(["case-test"], purge_seen=True, undo=False)
ts_1007 = int(datetime.datetime(2025, 10, 7, 0, 0, tzinfo=datetime.timezone.utc).timestamp())
chk("筛选：since 只要这个时间之后发的",
    [r["title"] for r in feed.entries(since=ts_1007, limit=0)] == ["第二篇"],
    [r["title"] for r in feed.entries(since=ts_1007, limit=0)])
chk("筛选：until 只要这个时间之前发的",
    sorted(r["title"] for r in feed.entries(until=ts_1007, limit=0)) == ["原子条目", "第一篇"],
    sorted(r["title"] for r in feed.entries(until=ts_1007, limit=0)))
chk("筛选：日期串也能当 since（不用先换算 epoch）",
    len(feed.entries(since="2025-10-07T00:00:00Z", limit=0)) == 1, None)
chk("排序：默认最新发布在前，跨源也一样",
    feed.entries(limit=0)[0]["title"] == "第二篇",
    [(r["title"], r["feed_title"]) for r in feed.entries(limit=0)])
chk("排序：published_asc 是正序",
    [r["title"] for r in feed.entries(feed_id=fid, sort="published_asc", limit=0)]
    == ["第一篇", "第二篇"], None)
chk("排序：标题排序也认",
    [r["title"] for r in feed.entries(sort="title_asc", limit=0)]
    == ["原子条目", "第一篇", "第二篇"], [r["title"] for r in feed.entries(sort="title_asc", limit=0)])
chk("组合：多个条件是「与」的关系",
    len(feed.entries(group="前沿科技", unread=True, q="第二篇")) == 1
    and feed.entries(group="前沿科技", unread=True, q="第一篇") == [], None)
chk("组合：组名不存在就筛不出东西", feed.entries(group="没这个组", limit=0) == [], None)
chk("分页：offset 接着 limit 往后翻",
    [r["id"] for r in feed.entries(limit=1, offset=1)] == [feed.entries(limit=2)[1]["id"]], None)
chk("分页：limit=0 是全量", len(feed.entries(limit=0)) == feed.summary()["entries"], None)
chk("计数：count_entries 与列表数一致",
    feed.count_entries(unread=True) == len(feed.entries(unread=True, limit=0)), None)


# ── 17. 批量已读 + 撤销快照 ────────────────────────────────────────
unread_before = len(feed.entries(unread=True, limit=0))
all_read = feed.mark_read_all()
chk("批量：全部标记已读把未读清零",
    all_read["n"] == unread_before
    and feed.entries(unread=True, limit=0) == [] and feed.summary()["unread"] == 0, all_read)
chk("批量：结果里带回撤销令牌", bool(all_read["undo_token"]), all_read)
back = feed.undo(all_read["undo_token"])
chk("撤销：全部已读能整份还原",
    back["ok"] is True and len(feed.entries(unread=True, limit=0)) == unread_before, back)
chk("撤销：快照用完即弃，同一个令牌撤第二次要报错",
    raises(feed.undo, all_read["undo_token"]), None)
chk("撤销：令牌乱给也说人话", raises(feed.undo, "no-such-token"), None)
one_unread = len(feed.entries(feed_id=atom_id, unread=True, limit=0))
r_one = feed.mark_read_all(feed_id=atom_id)
chk("批量：只圈一个源时不碰别的源",
    r_one["n"] == one_unread and feed.entries(feed_id=atom_id, unread=True, limit=0) == []
    and len(feed.entries(feed_id=fid, unread=True, limit=0)) == 1, r_one)
feed.undo(r_one["undo_token"])
grp_unread = len(feed.entries(group="前沿科技", unread=True, limit=0))
r_grp = feed.mark_read_all(group="前沿科技", undo=False)
chk("批量：按组标记已读",
    r_grp["n"] == grp_unread and feed.entries(group="前沿科技", unread=True, limit=0) == [], r_grp)
chk("批量：undo=False 就不留快照", r_grp["undo_token"] == "", r_grp)
chk("批量：read=False 是打回未读",
    feed.mark_read_all(read=False)["n"] >= 1 and feed.summary()["unread"] > 0, None)
toks = [feed.snapshot("第%d份" % i) for i in range(feed.SNAP_MAX + 3)]
lst = feed.snapshots()
chk("撤销：快照最多留 SNAP_MAX 份，超了丢最旧",
    len(lst) == feed.SNAP_MAX and lst[0]["token"] == toks[-1], len(lst))
chk("撤销：snapshots() 最新的在前、带标签和时间",
    lst[0]["label"] == "第%d份" % (feed.SNAP_MAX + 2) and lst[0]["at"] > 0, lst[:2])
chk("撤销：被挤掉的老快照撤不动", raises(feed.undo, toks[0]), None)
chk("撤销：能一次清空", feed.clear_snapshots() == feed.SNAP_MAX and feed.snapshots() == [], None)

# 快照之后「退订全部」的：旧快照躺着整库旧订阅，悄悄还原等于把用户清掉的东西又搬回来
tok_clear = feed.snapshot("还能撤的时候")
keep_state = len(feed.subs())
keep_raw = store_raw()
cleared = feed._load()
cleared["feeds"] = []
cleared["entries"] = {}
feed._save(cleared)
chk("撤销：库被清空过就不悄悄复活整库旧订阅", raises(feed.undo, tok_clear), None)
chk("撤销：拒绝还原时快照仍留着，不让人白丢一次机会",
    tok_clear in {s["token"] for s in feed.snapshots()}, feed.snapshots())
feed._save(keep_raw)                       # 手工把盘恢复到清空前
chk("撤销：退订全部之后确实撤不动（不是快照坏了）", len(feed.subs()) == keep_state, feed.subs())
chk("撤销：盘上又有源了，同一枚令牌还能用",
    feed.undo(tok_clear)["ok"] is True and len(feed.subs()) == keep_state, feed.subs())


# ── 18. 批量删除条目 + 撤销 ────────────────────────────────────────
atom_rows = feed.entries(feed_id=atom_id, limit=0)
aid = atom_rows[0]["id"]
entries_before = feed.summary()["entries"]
rss_ids = [r["id"] for r in feed.entries(feed_id=fid, limit=0)]

d = feed.delete_entries([aid] + rss_ids[:1])
chk("删除：批量删多条一并算进 n", d["n"] == 2, d)
chk("删除：条目从库里没了",
    feed.entries(feed_id=atom_id, limit=0) == []
    and len(feed.entries(feed_id=fid, limit=0)) == len(rss_ids) - 1, None)
chk("删除：靠 seen 压制，强刷也不会又冒出来",
    feed.refresh(atom_id, force=True)["new"] == 0
    and feed.entries(feed_id=atom_id, limit=0) == [], None)
chk("删除：seen 里仍留着去重记录", aid in store_raw()["seen"].get(atom_id, {}), None)
back = feed.undo(d["undo_token"])
chk("删除：撤销把条目找回来",
    len(feed.entries(feed_id=atom_id, limit=0)) == len(atom_rows)
    and feed.summary()["entries"] == entries_before, back)
chk("删除：撤销后强刷仍不翻倍", feed.refresh(atom_id, force=True)["new"] == 0, None)
purged = feed.delete_entries([aid], purge_seen=True)
chk("删除：purge_seen 连去重记录一起清掉",
    purged["n"] == 1 and aid not in store_raw()["seen"].get(atom_id, {}), purged)
chk("删除：清过去重记录的条目下次刷新算成新条目",
    feed.refresh(atom_id, force=True)["new"] == 1, None)
feed.undo(purged["undo_token"])
chk("删除：撤销后条数与 seen 都回来了",
    feed.summary()["entries"] == entries_before
    and aid in store_raw()["seen"].get(atom_id, {}), feed.summary())
chk("删除：没说要删哪条时报错而不是清空", raises(feed.delete_entries, []), None)


# ── 19. OPML 导出 / 导入 ───────────────────────────────────────────
opml = feed.export_opml()
doc = ET.fromstring(opml)
chk("OPML：导出的东西能当 XML 解析", doc is not None, opml[:80])
chk("OPML：声明的是 OPML 2.0", doc.get("version") == "2.0", doc.attrib)
subs_now = feed.subs()
outs = [o for o in doc.iter("outline") if o.get("xmlUrl")]
chk("OPML：每个订阅源都在，地址也对得上",
    sorted(o.get("xmlUrl") for o in outs) == sorted(r["url"] for r in subs_now), outs)
nest = [o for o in doc.iter("outline") if not o.get("xmlUrl") and o.get("title") == "前沿科技"]
chk("OPML：组是嵌套 outline、源挂在组里",
    len(nest) == 1 and len([c for c in nest[0] if c.get("xmlUrl")]) == 2, nest)
chk("OPML：组名同时写进 category，被拍平导入也不丢分组",
    all(o.get("category") == "前沿科技" for o in nest[0] if o.get("xmlUrl")), nest)
chk("OPML：没分组的源直接躺在 body 下",
    len([c for c in doc.find("body") if c.get("xmlUrl")])
    == len([r for r in subs_now if not r["group"]]), None)
re_imp = feed.import_opml(opml)
chk("OPML：导出再导入回来一条都不新增",
    re_imp["added"] == 0 and re_imp["existed"] == len(subs_now) and re_imp["failed"] == 0, re_imp)
chk("OPML：dry_run 只报不订",
    feed.import_opml(opml, dry_run=True)["added"] == 0
    and len(feed.subs()) == len(subs_now), None)

mine = ('<?xml version="1.0" encoding="UTF-8"?><opml version="2.0">'
        '<head><title>别人的订阅</title></head><body>'
        '<outline text="中文圈" title="中文圈">'
        '<outline type="rss" text="中文订阅" xmlUrl="%s/gbk.xml" htmlUrl="%s/"/>'
        '<outline text="技术" title="技术">'
        '<outline type="rss" text="重复的源" xmlUrl="%s/feed.xml"/>'
        '</outline></outline>'
        '<outline type="rss" text="怪协议" xmlUrl="ftp://example.com/rss"/>'
        '<outline type="rss" text="没这个东西" xmlUrl="%s/nothing.xml"/>'
        '</body></opml>') % (BASE, BASE, BASE, BASE)
imp = feed.import_opml(mine)
chk("OPML 导入：逐条回结果", imp["total"] == 4 and len(imp["results"]) == 4, imp)
gbk_row = next(r for r in imp["results"] if r["url"].endswith("/gbk.xml"))
chk("OPML 导入：新地址订上了并带上组名",
    gbk_row["status"] == "added" and bool(gbk_row["feed_id"]) and gbk_row["group"] == "中文圈",
    gbk_row)
chk("OPML 导入：订上的源在列表里查得到、标题也跟着拿到了",
    any(r["url"].endswith("/gbk.xml") and r["group"] == "中文圈" and r["title"]
        for r in feed.subs()), feed.subs())
dup_row = next(r for r in imp["results"] if r["url"].endswith("/feed.xml"))
chk("OPML 导入：已存在的标 existed，不重订也不报错", dup_row["status"] == "existed", dup_row)
chk("OPML 导入：嵌套分类拉平成「父 / 子」", dup_row["group"] == "中文圈 / 技术", dup_row)
bad = [r for r in imp["results"] if r["status"] == "failed"]
chk("OPML 导入：坏地址各有原因且不拖累别的条目",
    len(bad) == 2 and all(r["error"] for r in bad) and imp["added"] == 1, bad)
chk("OPML 导入：非 http/https 的地址被挡在外面",
    any("http" in r["error"] for r in bad), bad)
chk("OPML 导入：抓不到的源给的失败原因是中文",
    any("\u4e00" <= c <= "\u9fff" for r in bad for c in r["error"]), bad)
chk("OPML 导入：给它一段不是 OPML 的文本会说人话", raises(feed.import_opml, "hello world"), None)
chk("OPML 导入：半截 XML 会说人话", raises(feed.import_opml, "<opml><body>"), None)
chk("OPML 导入：光有外壳没有 xmlUrl 也算读不出",
    raises(feed.import_opml, '<opml version="2.0"><head/><body>'
           '<outline text="空组"/></body></opml>'), None)
chk("OPML 导入：声明了自定义实体的拒了（防实体爆炸）",
    raises(feed.import_opml, '<!DOCTYPE opml [<!ENTITY a "x">]><opml version="2.0"><body>'
           '<outline text="t" xmlUrl="http://e.com/f"/></body></opml>'), None)
opml_path = feed.write_opml(os.path.join(SANDBOX, "sub.opml"))
chk("OPML：write_opml 落的是能解析、条目齐的文件",
    os.path.isfile(opml_path)
    and len([o for o in ET.fromstring(open(opml_path, encoding="utf-8").read())
             .iter("outline") if o.get("xmlUrl")]) == len(feed.subs()), opml_path)


# ── 20. 刷新的可观测性 ─────────────────────────────────────────────
calls = []
r2 = feed.refresh(progress=lambda done, total, label: calls.append((done, total, label)))
ids_now = [r["id"] for r in feed.subs()]
chk("刷新：老字段 feeds / new / errors 一个不少",
    {"feeds", "new", "errors"} <= set(r2) and isinstance(r2["errors"], list), sorted(r2))
chk("刷新：每个源都有结果且字段齐",
    len(r2["results"]) == len(ids_now)
    and all({"feed_id", "title", "url", "group", "new", "error", "not_modified",
             "fetched"} <= set(x) for x in r2["results"]), r2["results"])
chk("刷新：304 的源在结果里标了 not_modified",
    any(x["not_modified"] for x in r2["results"] if x["url"].endswith("/feed.xml")),
    r2["results"])
chk("刷新：进度按源推进，最后一步 done==total",
    [c[0] for c in calls] == list(range(1, len(ids_now) + 1))
    and all(c[1] == len(ids_now) for c in calls), calls)
chk("刷新：进度里带了源名", all(c[2] for c in calls), calls)
chk("刷新：没中止时 stopped 为假、done==total",
    r2["stopped"] is False and r2["done"] == r2["total"], r2)
chk("刷新：进度回调里抛错不带崩订阅",
    len(feed.refresh(progress=lambda d, t, l: 1 / 0)["results"]) == len(ids_now), None)
only_grp = feed.refresh(group="前沿科技")["results"]
chk("刷新：group 只刷这个组",
    bool(only_grp) and all(x["group"] == "前沿科技" for x in only_grp)
    and len(only_grp) == len(feed.subs(group="前沿科技")), only_grp)


# ── 21. 刷新中途取消（已完成的部分要留住） ─────────────────────────
def _json_feed(titles, day=8):
    items = [{"id": "json-1", "url": BASE + "/j1", "title": "JSON 条目",
              "content_html": "<p>JSON 正文</p>",
              "date_published": "2025-10-06T10:00:00Z"}]
    for i, t in enumerate(titles, start=1):
        items.append({"id": "json-%d" % (i + 1), "url": BASE + "/j%d" % (i + 1), "title": t,
                      "summary": "只有摘要的一条：%s" % t,
                      "date_published": "2025-10-%02dT10:00:00Z" % (day + i)})
    return json.dumps({"version": "https://jsonfeed.org/version/1.1", "title": "JSON 订阅",
                       "home_page_url": BASE + "/", "items": items},
                      ensure_ascii=False).encode("utf-8")


ROUTES["/feed.json"] = (_json_feed([]), "application/feed+json", None)
jid = feed.add(BASE + "/feed.json")["id"]
feed.reorder(jid, index=0)
before_map = {r["id"]: (r["total"], r["last_fetched"], r["error"]) for r in feed.subs()}
ROUTES["/feed.json"] = (_json_feed(["新的第一条"]), "application/feed+json", None)
tick = {"n": 0}


def stop_second():
    tick["n"] += 1
    return tick["n"] > 1          # 刷完第一个源就叫停


r3 = feed.refresh(should_stop=stop_second)
chk("取消：刷完一个源就收手", r3["stopped"] is True and len(r3["results"]) == 1, r3)
chk("取消：total 仍是全部源数", r3["total"] == len(before_map), r3)
chk("取消：已完成部分的新条目留住了",
    len(feed.entries(feed_id=jid, limit=0)) == 2, feed.entries(feed_id=jid, limit=0))
chk("取消：留下的是写进盘的，不是只在内存里",
    len(store_raw()["entries"].get(jid, [])) == 2, None)
after_map = {r["id"]: (r["total"], r["last_fetched"], r["error"]) for r in feed.subs()}
chk("取消：没轮到的源一个字段都没动",
    all(after_map[k] == before_map[k] for k in before_map if k != jid),
    {k: (before_map[k], after_map[k]) for k in before_map if k != jid})
r4 = feed.refresh(should_stop=lambda: True)
chk("取消：一上来就叫停则一个源都不刷",
    r4["stopped"] is True and r4["done"] == 0 and r4["results"] == [], r4)
chk("取消：should_stop 抛错就当作没中止，订阅照刷",
    len(feed.refresh(should_stop=lambda: 1 / 0)["results"]) == len(before_map), None)


# ── 22. 全文抓取（开关 + 单条 + 批量） ─────────────────────────────
import clip_article      # noqa: E402  只桩正文提取：它是唯一会去敲「原文链接」的那一步

need = next(r for r in feed.entries(feed_id=fid, limit=0) if r["title"] == "第二篇")
stub_md = "# 补出来的全文\n\n这一段是回源补出来的正文，词儿够特别。\n"
orig_extract = clip_article.extract
clip_article.extract = lambda u: {"markdown": stub_md, "site": "示例站"}
try:
    got = feed.fetch_content(need["id"])
    chk("全文：单条补文存进 content_md",
        got["n"] == 1 and stub_md.strip() in feed.entry(need["id"])["content_md"], got)
    chk("全文：补过之后 has_content / has_fulltext 都变真",
        feed.entry(need["id"])["has_content"] is True
        and feed.entry(need["id"])["has_fulltext"] is True, None)
    chk("全文：补出来的正文能搜到",
        [r["title"] for r in feed.entries(q="词儿够特别")] == ["第二篇"], None)
    chk("全文：已有全文的默认不再回源（only_missing）",
        feed.fetch_content(need["id"])["total"] == 0, None)
    chk("全文：only_missing=False 就强制再补一次",
        feed.fetch_content(need["id"], only_missing=False)["n"] == 1, None)
    res = feed.fetch_content(limit=2)
    chk("全文：批量有条数上限，不会整库硬抓",
        len(res["ok"]) <= 2 and res["total"] > 2, res)
    chk("全文：批量补的写进了盘",
        any(e.get("content_md") for lst in store_raw()["entries"].values() for e in lst), None)
    chk("全文：开关默认关",
        feed.settings()["fulltext"] is False and feed.fulltext_enabled() is False,
        feed.settings())
    feed.set_fulltext(True)
    chk("全文：开关打开后存进了设置",
        feed.fulltext_enabled() is True and store_raw()["settings"]["fulltext"] is True,
        feed.settings())
    ROUTES["/feed.json"] = (_json_feed(["新的第一条", "开关自动补的那条"]),
                            "application/feed+json", None)
    feed.refresh(jid, force=True)
    auto = next((r for r in feed.entries(feed_id=jid, limit=0)
                 if r["title"] == "开关自动补的那条"), None)
    chk("全文：开着开关刷新会自动给新条目补全文",
        auto is not None and feed.entry(auto["id"])["content_md"] != "", auto)
    feed.set_fulltext(False)
    ROUTES["/feed.json"] = (_json_feed(["新的第一条", "开关自动补的那条", "关掉后的那条"]),
                            "application/feed+json", None)
    feed.refresh(jid, force=True)
    off = next((r for r in feed.entries(feed_id=jid, limit=0)
                if r["title"] == "关掉后的那条"), None)
    chk("全文：开关关掉后刷新不再自动回源",
        off is not None and feed.entry(off["id"])["content_md"] == "", off)

    def boom(u):
        raise ValueError("对方站点不给看")

    clip_article.extract = boom
    miss = next(r for r in feed.entries(feed_id=jid, limit=0) if not r["has_fulltext"])
    bad_one = feed.fetch_content(miss["id"])
    chk("全文：回源失败收进 failed，不抛到界面外",
        bad_one["n"] == 0 and len(bad_one["failed"]) == 1 and bad_one["failed"][0]["error"],
        bad_one)
    chk("全文：失败的条目下次还能再补",
        feed.fetch_content(miss["id"])["failed"][0]["id"] == miss["id"], None)
    clip_article.extract = lambda u: {"markdown": "   ", "site": ""}
    empty = feed.fetch_content(miss["id"])
    chk("全文：取回来是空的也说「没读出正文」",
        empty["n"] == 0 and "正文" in empty["failed"][0]["error"], empty)

    calls_back = []

    def spy(u):
        calls_back.append(u)
        return {"markdown": "", "site": ""}

    clip_article.extract = spy
    books2 = os.path.join(SANDBOX, "books2")
    info2 = feed.to_shelf(need["id"], books2)
    md_files = sorted(Path(info2["dir"]).rglob("*.md")) if info2.get("dir") else []
    txt = "\n".join(p.read_text(encoding="utf-8") for p in md_files)
    chk("全文：补过全文的条目入书架不再敲第二次门",
        bool(info2.get("id")) and calls_back == [], calls_back)
    chk("全文：补出来的全文真的进了那本书", "回源补出来的正文" in txt, txt[:160])
finally:
    clip_article.extract = orig_extract


# ── 23. 保留策略（默认关闭） ───────────────────────────────────────
chk("清理：策略默认全 0（不启用）",
    feed.settings()["policy"] == {"days": 0, "max_entries": 0, "keep_starred": True},
    feed.settings())
p0 = feed.prune()
chk("清理：没开策略时一条不动并说明原因",
    p0["removed"] == 0 and p0["by_feed"] == {} and "note" in p0, p0)
chk("清理：刷新时默认也不裁", feed.refresh(jid)["pruned"] == 0, None)
j_tot = len(feed.entries(feed_id=jid, limit=0))
dry = feed.prune(max_entries=1, feed_id=jid, dry_run=True)
chk("清理：dry_run 只报不删",
    dry["removed"] >= 1 and len(feed.entries(feed_id=jid, limit=0)) == j_tot, dry)
cut = feed.prune(max_entries=1, feed_id=jid)
kept = feed.entries(feed_id=jid, limit=0)
newest = max(feed.entries(feed_id=jid, sort="published_asc", limit=0),
             key=lambda r: r["published_ts"])
chk("清理：按条数裁到 1 条且留的是最新的",
    len(kept) == 1 and kept[0]["title"] == "关掉后的那条"
    and kept[0]["id"] == newest["id"], [r["title"] for r in kept])
chk("清理：结果里带每个源裁了几条", cut["by_feed"].get(jid) == j_tot - 1, cut)
chk("清理：裁掉的能整份撤回来",
    feed.undo(cut["undo_token"])["ok"] is True
    and len(feed.entries(feed_id=jid, limit=0)) == j_tot, None)
flat = all_entries()
prot = sorted(e["id"] for e in flat if e.get("starred") or e.get("later"))
# 「旧」按 _entry_ts 判：有发布时间看发布时间，没发布时间（不少微博 / 短摘要源就这样）
# 退回抓到时间，所以夹具里那条没写 pubDate 的中文条目算刚抓的，不该被裁。
cutoff = feed._now() - 86400
expected_old = sorted(e["id"] for e in flat
                      if not (e.get("starred") or e.get("later"))
                      and (e.get("published_ts") or e.get("fetched_at") or 0) < cutoff)
d2 = feed.prune(days=1, dry_run=True)
chk("清理：按天数裁会动到所有旧条目，但保住收藏与稍后读",
    len(prot) >= 2 and len(expected_old) >= 1
    and d2["removed"] == len(expected_old) and d2["dry_run"] is True,
    {"removed": d2["removed"], "old": len(expected_old), "prot": len(prot), "all": len(flat)})
chk("清理：没发布时间的条目按抓到时间算，不当旧账裁掉",
    all(e.get("published_ts") for e in flat if e["id"] in expected_old),
    [e["id"] for e in flat if e["id"] in expected_old and not e.get("published_ts")])
d3 = feed.prune(days=1)
chk("清理：真裁一刀后收藏 / 稍后读还在",
    d3["removed"] == len(expected_old)
    and sorted(e["id"] for e in all_entries()
               if e.get("starred") or e.get("later")) == prot
    and len(feed.entries(starred=True, limit=0)) >= 1
    and len(feed.entries(later=True, limit=0)) >= 1,
    {"removed": d3["removed"], "old": len(expected_old)})
chk("清理：这一刀也撤得回来",
    feed.undo(d3["undo_token"]) and len(all_entries()) == len(flat), None)
keep = feed.snapshot("策略实验前")
tot_now = len(all_entries())
rp = feed.refresh(prune_after={"max_entries": 2, "keep_starred": True})
chk("清理：策略生效时刷新顺手裁并回报裁了多少",
    rp["pruned"] >= 1 and len(all_entries()) == tot_now - rp["pruned"], rp)
chk("清理：prune_after=False 时刷新不裁", feed.refresh(prune_after=False)["pruned"] == 0, None)
feed.undo(keep)
chk("清理：刷新顺手裁掉的能撤回来", len(all_entries()) == tot_now, None)
chk("清理：策略能写进设置里长期生效",
    feed.set_settings(policy={"days": 3650})["policy"]["days"] == 3650
    and feed.settings()["policy"]["days"] == 3650, feed.settings())
chk("清理：改一个设置键不会把别的键冲掉",
    feed.set_settings(fulltext=True)["policy"]["days"] == 3650, feed.settings())
feed.set_settings(policy={"days": 0}, fulltext=False)
chk("清理：也能改回关闭",
    feed.settings()["policy"]["days"] == 0 and feed.settings()["fulltext"] is False,
    feed.settings())


# ── 24. 老文件（缺字段）照样能读写 ─────────────────────────────────
legacy = {
    "feeds": [{"id": "legacy1", "url": BASE + "/feed.xml", "title": "老文件里的源",
               "site": BASE + "/", "kind": "rss", "etag": '"v1"', "modified": "",
               "last_fetched": 0, "error": "", "legacy_field": "别丢"}],
    "entries": {"legacy1": [{"id": "old-1", "guid": "old-1", "title": "老条目",
                             "link": BASE + "/a9", "author": "", "published": "",
                             "summary": "老摘要", "content_html": "<p>老正文</p>",
                             "has_content": False, "read": False, "pushed": "",
                             "legacy_note": "留着"}]},
    "seen": {"legacy1": {"old-1": 1}},
}
with open(feed.STORE, "w", encoding="utf-8") as f:
    json.dump(legacy, f, ensure_ascii=False)
chk("老文件：缺 group/order 也能读出来",
    len(feed.subs()) == 1 and feed.subs()[0]["group"] == ""
    and isinstance(feed.subs()[0]["order"], int), feed.subs())
chk("老文件：缺 starred/later/tags 的条目能读能筛",
    feed.entries(starred=True, limit=0) == [] and len(feed.entries(limit=0)) == 1, None)
chk("老文件：缺 settings 时用默认值（自动全文关、策略关）",
    feed.settings()["fulltext"] is False and feed.settings()["policy"]["days"] == 0,
    feed.settings())
chk("老文件：没有发布时间时 published_ts 是 0 而不是崩",
    feed.entries(limit=1)[0]["published_ts"] == 0, feed.entries(limit=1)[0])
chk("老文件：批量收藏写得进去",
    feed.mark_star("old-1", True) == 1 and feed.entry("old-1")["starred"] is True, None)
chk("老文件：打标签 / 稍后读也照常",
    feed.tag_add("old-1", ["旧"]) == 1 and feed.toggle_later("old-1")["later"] is True, None)
chk("老文件：分组未读数照算",
    feed.groups()[0]["total"] == 1 and "group" in feed.groups()[0], feed.groups())
chk("老文件：条件请求照常（304）",
    feed.refresh("legacy1")["results"][0]["not_modified"] is True, None)
raw = store_raw()
chk("老文件：写回之后未知字段一个不丢",
    raw["feeds"][0].get("legacy_field") == "别丢"
    and raw["entries"]["legacy1"][0].get("legacy_note") == "留着", raw["feeds"][0])
chk("老文件：写回时把新字段补齐了",
    raw["feeds"][0].get("group") == "" and raw["feeds"][0].get("folder") == ""
    and raw["entries"]["legacy1"][0].get("tags") == ["旧"], raw["entries"]["legacy1"][0])
legacy["feeds"][0]["folder"] = "旧分组"
legacy["feeds"][0].pop("group", None)
legacy["entries"]["legacy1"][0]["published"] = "Tue, 07 Oct 2025 10:00:00 GMT"
with open(feed.STORE, "w", encoding="utf-8") as f:
    json.dump(legacy, f, ensure_ascii=False)
chk("老文件：老键 folder 认作组名", feed.subs()[0]["group"] == "旧分组", feed.subs())
chk("老文件：组统计跟着出来",
    feed.groups()[0]["group"] == "旧分组" and feed.groups()[0]["feeds"] == 1, feed.groups())
chk("老文件：published_ts 由发布时间补出来（排序与 since 都能用）",
    feed.entries(limit=1)[0]["published_ts"] > 0
    and feed.entries(since="2025-10-01", limit=0) != [], feed.entries(limit=1)[0])
chk("老文件：OPML 照样导得出去",
    ET.fromstring(feed.export_opml()) is not None
    and feed.export_opml().count("xmlUrl") == 1, feed.export_opml()[:200])
chk("老文件：summary / tag_list 不抛",
    feed.summary()["feeds"] == 1 and feed.tag_list() == [], feed.summary())
feed.tag_add("old-1", ["旧"])
chk("老文件：标签清单照样从条目里现算出来",
    [r["tag"] for r in feed.tag_list()] == ["旧"] and feed.tag_list()[0]["count"] == 1,
    feed.tag_list())
with open(feed.STORE, "w", encoding="utf-8") as f:
    f.write("{只剩半截")
chk("坏文件：读不出来就当空的，不抛异常",
    feed.subs() == [] and feed.summary()["feeds"] == 0, None)
feed.tag_add(["x"], ["y"])
raw2 = store_raw()
chk("坏文件：写回的是一份结构完整、能接着用的库",
    {"feeds", "entries", "seen", "settings"} <= set(raw2), sorted(raw2))


# ── 并发：刷新还挂着的时候，界面上的点选不能丢 ──────────────────────
# 这一条钉的是「点了没反应 / 标签存不上」那类 bug 的根因，不是某个函数的返回值：
# 旧写法一进 refresh 就把整份库读进内存，抓完所有源才写一次盘 —— 中间那几秒里用户
# 打的标签、标的已读会被最后那次整份覆盖静默吃掉（真机上复现过：tag_set 报
# 「没有这条订阅」、整档标已读回 0 条）。现在改成抓在锁外、每个源单独进锁重读写回。
for _s in feed.subs():
    feed.remove(_s["id"])
FAIL_PATHS.clear()
SLOW["delay"] = 1.2
slow_feed = feed.add(BASE + "/slow.xml", group="并发")
fast_feed = feed.add(BASE + "/fast.xml", group="并发")
slow_eid = feed.entries(feed_id=slow_feed["id"])[0]["id"]
fast_eid = feed.entries(feed_id=fast_feed["id"])[0]["id"]
feed.mark_read([slow_eid, fast_eid], False)

_bg = {}


def _bg_refresh():
    try:
        _bg["res"] = feed.refresh(force=True)
    except Exception as e:                     # noqa: BLE001
        _bg["err"] = e


_th = threading.Thread(target=_bg_refresh)
_th.start()
time.sleep(0.45)                              # 这会儿刷新正卡在慢源那次请求里
try:
    _tagged = feed.tag_set(fast_eid, ["刷新期间打的"])
    _read_n = feed.mark_read([slow_eid], True)
except ValueError as e:
    _tagged, _read_n = ("抛错：" + str(e)), -1
_th.join(timeout=40)
_raw = store_raw()
_ent = {e["id"]: e for lst in _raw["entries"].values() for e in lst or []}
chk("并发：刷新挂着时打的标签没被整份覆盖吃掉",
    _tagged == ["刷新期间打的"] and _ent.get(fast_eid, {}).get("tags") == ["刷新期间打的"],
    (_tagged, _ent.get(fast_eid, {}).get("tags")))
chk("并发：刷新挂着时标的已读活到了最后",
    _read_n == 1 and _ent.get(slow_eid, {}).get("read") is True,
    (_read_n, _ent.get(slow_eid, {}).get("read")))
chk("并发：刷新照常跑完，条目与源状态也照常落盘",
    "err" not in _bg and _bg["res"]["feeds"] == 2 and _bg["res"]["errors"] == []
    and len(_ent) == 2 and _raw["feeds"][0].get("last_fetched"),
    _bg)
chk("并发：刷新跑完后读接口照样答得上来（读不抱锁等网络）",
    feed.summary()["feeds"] == 2 and feed.summary()["unread"] == 1
    and len(feed.entries(limit=10)) == 2, feed.summary())
SLOW["delay"] = 0.0
for _s in feed.subs():
    feed.remove(_s["id"])


# ── 收摊 ────────────────────────────────────────────────────────────
httpd.shutdown()

print()
if FAIL:
    print("失败 %d 项：%s" % (len(FAIL), "、".join(FAIL)))
    sys.exit(1)
print("全部通过")
