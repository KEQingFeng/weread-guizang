# -*- coding: utf-8 -*-
"""订阅刷新合并的离线门禁：这一趟源没给正文，别把已存的正文抹成空。

为什么要单开一份：`feed._merge_entries` 用 `content_hash` 判「文章是不是编辑过」。
`parse_feed` 每条一定带上 `summary` / `content_html` 两个键（可以是空串），所以
「新值拿不到就退回旧值」的那句 `.get(key, old)` 实际从不会触发。于是源在两次抓取
之间只在 title / link 之间摆动、或者回了半截内容时，第二次刷新会把上一次已经存好
的正文覆盖成空 —— 没有报错、没有提示，用户只是某天发现那篇文章的正文没了。

这里钉住「只认非空的正文」这条：空正文不算「文章被改短」，旧的留着；真有新正文
（包括第一次只有标题、第二次才给正文）照旧刷新；用户自己的已读 / 稍后读状态不受
影响；同一趟里新出现的空正文条目该收还是收。

全程离线：`_fetch_bytes` 换成记账的假实现，GUIZANG_DATA 指向临时沙盒，
feed.json 落在沙盒里，绝不碰用户真实的 cache/。
"""
import atexit
import os
import shutil
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

# GUIZANG_DATA 必须在 import feed 之前设好：feed.py 在导入时就把 STORE 定下来了。
SANDBOX = tempfile.mkdtemp(prefix="guizang-feedmerge-")
atexit.register(lambda: shutil.rmtree(SANDBOX, ignore_errors=True))
os.environ["GUIZANG_DATA"] = os.path.join(SANDBOX, "data")
os.makedirs(os.environ["GUIZANG_DATA"], exist_ok=True)

import feed  # noqa: E402

FAILED = []
COUNT = 0


def chk(name, cond, extra=""):
    global COUNT
    COUNT += 1
    print(("  ok  " if cond else " FAIL ") + name + ("" if cond else "  <- " + repr(extra)))
    if not cond:
        FAILED.append(name)


def rss(*items):
    """一份最小 RSS。每项是 (guid, title, description_html_or_空)。"""
    body = ['<?xml version="1.0"?><rss version="2.0"><channel>',
            "<title>夹具站</title><link>http://x.test/</link>"]
    for guid, title, desc in items:
        body.append("<item><guid>%s</guid><title>%s</title>"
                    "<description>%s</description></item>" % (guid, title, desc))
    body.append("</channel></rss>")
    return "".join(body).encode("utf-8")


FULL = rss(("k1", "一篇有正文的", "&lt;p&gt;ORIGINAL BODY TEXT&lt;/p&gt;"))
EMPTY = rss(("k1", "一篇有正文的", ""))
EDITED = rss(("k1", "一篇有正文的", "&lt;p&gt;EDITED BODY&lt;/p&gt;"))
TITLE_ONLY = rss(("k2", "只有标题", ""))
LATER_FULL = rss(("k2", "只有标题", "&lt;p&gt;后到的正文&lt;/p&gt;"))
TWO_ITEMS = rss(("k1", "一篇有正文的", "&lt;p&gt;EDITED BODY&lt;/p&gt;"),
                ("k3", "另一条只有标题", ""))

served = {"body": FULL, "headers": {}}


def fake_fetch_bytes(url, headers=None):
    """记账的假抓取：换 served['body'] 就等于「源下一趟变了」。"""
    return served["body"], url, dict(served["headers"])


feed._fetch_bytes = fake_fetch_bytes

f = feed.add("http://x.test/feed.xml")
fid = f["id"]
eid = feed.entries(fid)[0]["id"]

view = feed.entry(eid)
chk("入库时正文存下来了",
    view["summary"] == "ORIGINAL BODY TEXT" and "ORIGINAL BODY TEXT" in view["content_html"],
    (view["summary"], view["content_html"]))

# 用户自己的状态：这一条被读过、也被标了稍后读。刷新不该动它。
feed.mark_read([eid])
feed.mark_later([eid])

# ── 核心：源这一趟没给正文，已存的正文不许被抹掉 ──────────────────
served["body"] = EMPTY
served["headers"] = {"etag": '"v2"'}
res = feed.refresh(fid, force=True)
chk("源没给正文时不算新增", res["new"] == 0, res.get("new"))

view = feed.entry(eid)
chk("空正文不再把已存正文抹成空（摘要）", view["summary"] == "ORIGINAL BODY TEXT", view["summary"])
chk("空正文不再把已存正文抹成空（正文 HTML）",
    "ORIGINAL BODY TEXT" in view["content_html"], view["content_html"])
chk("空正文也不清 content_text", "ORIGINAL BODY" in view["content_text"], view["content_text"])

# 再刷一次空正文，仍然稳（防止「第一次挡住了、第二次又漏」）
served["headers"] = {"etag": '"v3"'}
feed.refresh(fid, force=True)
chk("连着两趟空正文都稳", feed.entry(eid)["summary"] == "ORIGINAL BODY TEXT",
    feed.entry(eid)["summary"])

# ── 真的换了正文（非空 -> 另一个非空）仍要刷新 ────────────────────
served["body"] = EDITED
served["headers"] = {"etag": '"v4"'}
feed.refresh(fid, force=True)
view = feed.entry(eid)
chk("源真换了正文仍会刷新", view["summary"] == "EDITED BODY"
    and "EDITED BODY" in view["content_html"], (view["summary"], view["content_html"]))

# ── 第一次只有标题、第二次才给正文：照样补上 ──────────────────────
served["body"] = TITLE_ONLY
served["headers"] = {"etag": '"v5"'}
f2 = feed.add("http://x.test/title-only.xml")
fid2 = f2["id"]
eid2 = feed.entries(fid2)[0]["id"]
chk("只有标题的条目照样入库（正文先空着）", feed.entry(eid2)["summary"] == "",
    feed.entry(eid2)["summary"])

served["body"] = LATER_FULL
served["headers"] = {"etag": '"v6"'}
feed.refresh(fid2, force=True)
chk("后到的正文能补进来（空 -> 非空不受影响）",
    "后到的正文" in feed.entry(eid2)["content_html"], feed.entry(eid2)["content_html"])

# ── 用户状态在刷新里没被清掉 ──────────────────────────────────────
view = feed.entry(eid)
chk("已读标记还在", view["read"] is True, view)
chk("稍后读标记还在", view["later"] is True, view)

# ── 同一趟里新出现的空正文条目仍然收进来（只保旧的，不挡住新的）──
served["body"] = TWO_ITEMS
served["headers"] = {"etag": '"v7"'}
res = feed.refresh(fid, force=True)
ids = [e["id"] for e in feed.entries(fid, limit=0)]
new_ids = [i for i in ids if feed.entry(i)["summary"] == ""]
chk("同一趟里新增的空正文条目没被吞掉", res["new"] == 1 and len(new_ids) == 1,
    (res.get("new"), ids))
chk("新条目是那条 title-only 的 k3", feed.entry(new_ids[0])["title"] == "另一条只有标题",
    feed.entry(new_ids[0])["title"])

chk("旧条目正文仍然是刷新后的那份", feed.entry(eid)["summary"] == "EDITED BODY",
    feed.entry(eid)["summary"])

print()
print("订阅刷新合并门禁：%d 项通过，失败 %d 项" % (COUNT - len(FAILED), len(FAILED)))
for name in FAILED:
    print("  ✗ " + name)
shutil.rmtree(SANDBOX, ignore_errors=True)
sys.exit(1 if FAILED else 0)
