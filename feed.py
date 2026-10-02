# -*- coding: utf-8 -*-
"""RSS / Atom / JSON Feed 订阅：发现 → 抓取 → 解析 → 去重 → 入库。

为什么单独一个文件：订阅和「取书」「剪藏」是三条不同的路。取书走浏览器抓 Canvas，
剪藏是一次性的单篇网页，订阅则是同一批站点反复回来取增量，需要条件请求（ETag /
Last-Modified）、条目级去重、每个源各自的错误隔离。混进 clip_article 的话，
「一次抓一篇」的假设会被「一批源里的某一个坏了也不能拖垮其余」打破。

抓来的订阅内容是**不可信外部内容**：这里只把它解析成文本与链接，不执行、不解释。
feedparser 先做一轮（编码、相对地址、实体），这里再补一道更硬的过滤：script/style/
iframe 整块丢掉，on* 事件属性全部摘掉，javascript: / vbscript: / data: 这类地址一律
丢弃（data: 只放行图片）。截断到 500 条/源是为了让 JSON 文件不至于无限长。

没有后台线程、没有定时器：刷新只在被调用时发生（见 refresh），进程退出即停。
"""

import gzip
import hashlib
import html
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

import feedparser

import platform_compat

REPO = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = platform_compat.data_dir(REPO)
STORE = os.path.join(DATA_DIR, "feed.json")

# 浏览器 UA：不少站点对非浏览器 UA 直接 403。这里只取回内容，不伪装登录态。
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
      "(KHTML, like Gecko) Version/17.0 Safari/605.1.15")
TIMEOUT = 15
MAX_BYTES = 8 * 1024 * 1024
MAX_ENTRIES = 500                      # 每个源最多留这么多条，超了从最旧开始丢
SUMMARY_LIMIT = 600                    # 列表页摘要长度上限

# 发现订阅时按这个顺序探测常见路径
PROBE_PATHS = ("feed", "rss", "rss.xml", "atom.xml", "feed.xml",
               "index.xml", "atom", "feed.json")

FEED_TYPES = {
    "application/rss+xml": "rss",
    "application/atom+xml": "atom",
    "application/feed+json": "json",
    "application/json": "json",
    "text/rss+xml": "rss",
    "text/atom+xml": "atom",
}

# 只允许这两种 scheme 发出去；file:// data: gopher:// 之类一律拒绝
ALLOWED_SCHEMES = ("http", "https")


# ── 存取 ────────────────────────────────────────────────────────────

def _blank():
    return {"feeds": [], "entries": {}, "seen": {}}


def _load():
    try:
        with open(STORE, encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return _blank()
    if not isinstance(data, dict):
        return _blank()
    data.setdefault("feeds", [])
    data.setdefault("entries", {})
    data.setdefault("seen", {})
    return data


def _save(data):
    os.makedirs(os.path.dirname(STORE) or ".", exist_ok=True)
    tmp = STORE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, STORE)             # 原子替换：中途崩了也不会留下半截文件


def _now():
    return int(time.time())


def _new_id(url):
    """源的 id：URL 的 sha1 前 12 位。换标题不换 id，去重才稳。"""
    return hashlib.sha1((url or "").encode("utf-8")).hexdigest()[:12]


def _entry_key(item):
    """条目的稳定去重键：id || guid || sha1(link+title)。

    guid 优先是因为它本就是发布方给的稳定标识；有些源两个都没有（老式 RSS），
    才退回链接加标题的哈希。空 guid 不能当键，否则整源会塌成一条。
    """
    for k in ("id", "guid"):
        v = (item.get(k) or "").strip()
        if v:
            return v
    raw = ((item.get("link") or "") + "\x00" + (item.get("title") or "")).encode("utf-8")
    return "h:" + hashlib.sha1(raw).hexdigest()


# ── HTTP ────────────────────────────────────────────────────────────

def _check_url(url):
    u = (url or "").strip()
    if not u:
        raise ValueError("链接是空的")
    p = urllib.parse.urlparse(u)
    if p.scheme.lower() not in ALLOWED_SCHEMES:
        raise ValueError("只认 http/https 链接（现在这个是 %s）" % (p.scheme or "空"))
    if not (p.hostname or "").strip():
        raise ValueError("这个链接没有主机名，打不开")
    return u


def _http(url, headers=None):
    """取回 (blob, final_url, headers)。走 urllib，不依赖 curl。

    gzip 自己解：有些站点只在 Accept-Encoding 里给了 gzip 才肯按 gzip 回，
    而 urllib 不会自动解压，不处理的话拿到的是一堆压缩字节。
    """
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept": ("application/rss+xml,application/atom+xml,application/feed+json,"
                   "application/xml,text/xml,application/json,text/html;q=0.9,*/*;q=0.8"),
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Accept-Encoding": "gzip",
    })
    for k, v in (headers or {}).items():
        if v:
            req.add_header(k, v)
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        blob = r.read(MAX_BYTES + 1)
        final = r.geturl()
        hdrs = {k.lower(): v for k, v in r.headers.items()}
    if len(blob) > MAX_BYTES:
        raise ValueError("这个订阅太大了（超过 8MB）")
    if (hdrs.get("content-encoding") or "").lower() == "gzip":
        try:
            blob = gzip.decompress(blob)
        except Exception:
            pass
    return blob, final, hdrs


def _fetch_bytes(url, headers=None):
    try:
        return _http(url, headers)
    except urllib.error.HTTPError as e:
        if e.code == 304:
            return b"", url, {"__304__": "1"}
        raise ValueError("取这个订阅失败：HTTP %s" % e.code)
    except ValueError:
        raise
    except Exception as e:
        raise ValueError("打不开这个订阅：%s" % str(e)[:120])


# ── 解析 ────────────────────────────────────────────────────────────

_SCRIPT_STYLE = re.compile(r"(?is)<(script|style|iframe)\b[^>]*>.*?</\1>")
_ON_ATTR = re.compile(r"(?i)\son\w+\s*=\s*(\"[^\"]*\"|'[^']*'|[^\s>]+)")
_URL_ATTR = re.compile(r"""(?i)\s(href|src|xlink:href)\s*=\s*("[^"]*"|'[^']*'|[^\s>]+)""")
_BAD_SCHEME = re.compile(r"(?i)^\s*(javascript|vbscript|livescript):")
_DATA_IMG = re.compile(r"(?i)^\s*data:image/")


def sanitize_html(raw, base=""):
    """清掉外部订阅内容里的可执行部分，并把相对地址补成绝对。

    剪藏的正文在 clip_article 里已经有一道过滤，但那条路只处理「用户主动粘的
    单篇」，订阅是「同一批源反复灌进来」，风险面不同，这道过滤单独写、写得更硬。
    data: 只放行图片（内联缩略图常见），其余 data:/javascript:/vbscript: 全丢。
    """
    if not raw:
        return ""
    txt = _SCRIPT_STYLE.sub("", raw)
    txt = _ON_ATTR.sub("", txt)

    def fix(m):
        attr, val = m.group(1), m.group(2)
        quote = val[0] if val[:1] in ("\"", "'") else ""
        inner = val[1:-1] if quote else val
        s = inner.strip()
        if _BAD_SCHEME.match(s):
            return ' %s="#"' % attr
        if s.lower().startswith("data:") and not _DATA_IMG.match(s):
            return ' %s="#"' % attr
        if base and not re.match(r"(?i)^[a-z][a-z0-9+.-]*:", s) and not s.startswith("#"):
            try:
                s = urllib.parse.urljoin(base, s)
            except Exception:
                pass
        out = '"%s"' % s.replace('"', "&quot;")
        return " %s=%s" % (attr, out)

    return _URL_ATTR.sub(fix, txt)


_TAG = re.compile(r"(?s)<[^>]+>")
_WS = re.compile(r"[ \t\u00a0]+")


def _text_of(markup, base=""):
    """HTML 片段 → 纯文本（列表摘要用，不保留标签）。"""
    if not markup:
        return ""
    safe = sanitize_html(markup, base)
    safe = re.sub(r"(?i)<br\s*/?>", "\n", safe)
    safe = re.sub(r"(?i)</p\s*>", "\n", safe)
    txt = _TAG.sub("", safe)
    txt = html.unescape(txt)
    txt = _WS.sub(" ", txt)
    txt = re.sub(r"\n\s*\n\s*\n+", "\n\n", txt)
    return txt.strip()


def _abs(url, base):
    if not url:
        return ""
    try:
        return urllib.parse.urljoin(base, url.strip()) if base else url.strip()
    except Exception:
        return url.strip()


def _first(d, *keys):
    for k in keys:
        v = d.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
        if v:
            return v
    return ""


def parse_feed(blob, base=""):
    """把字节解析成统一结构：{title, site, kind, link, entries:[...]}。

    统一结构是关键：RSS/Atom/JSON Feed 三种格式的字段名各说各话，调用方（入库、
    列表）只该看到一套字段。JSON Feed 手写约三十行，不引第三方 jsonfeed 库。
    """
    text = _decode(blob)
    stripped = text.lstrip()
    kind = ""
    if stripped.startswith("{"):
        kind = "json"
    elif "<feed" in text[:2000].lower() or "<entry" in text[:8000].lower():
        kind = "atom"
    else:
        kind = "rss"

    if kind == "json":
        return _parse_jsonfeed(text, base)

    # 交给 feedparser 的是已经解码好的文本，不是原始字节：编码我们已经按 GBK 兜底
    # 判过一次了，再让它自己猜一遍只会把「头里写 utf-8、正文其实是 GB2312」的源
    # 解成满屏乱码。
    parsed = feedparser.parse(text)
    base = base or (parsed.get("href") or "")
    feed = parsed.get("feed") or {}
    title = _first(feed, "title") or ""
    link = _first(feed, "link") or base
    entries = []
    for e in parsed.get("entries", []):
        raw_summary = _first(e, "summary")
        if not raw_summary:
            content = e.get("content") or []
            if content and isinstance(content, list):
                raw_summary = content[0].get("value", "")
        full = ""
        content = e.get("content") or []
        if content and isinstance(content, list):
            full = content[0].get("value", "") or ""
        link_e = _abs(_first(e, "link"), base)
        item = {
            "id": _first(e, "id", "guid"),
            "guid": _first(e, "guid", "id"),
            "title": _text_of(_first(e, "title") or "", ""),
            "link": link_e,
            "author": _first(e, "author") or _author_name(e),
            "published": _published(e),
            "summary": _text_of(raw_summary, base)[:SUMMARY_LIMIT],
            "content_html": sanitize_html(full or raw_summary or "", base),
            "summary_html": sanitize_html(raw_summary or full or "", base),
        }
        item["has_content"] = len(_TAG.sub("", item["content_html"]).strip()) > len(item["summary"]) + 40
        entries.append(item)
    return {"title": title, "site": link, "kind": kind, "link": link, "entries": entries}


def _author_name(e):
    a = e.get("authors") or e.get("author_detail") or {}
    if isinstance(a, list) and a and isinstance(a[0], dict):
        return a[0].get("name", "")
    if isinstance(a, dict):
        return a.get("name", "")
    return ""


def _published(e):
    for k in ("published", "updated", "created"):
        v = e.get(k)
        if v:
            return str(v)
    return ""


def _parse_jsonfeed(text, base=""):
    try:
        doc = json.loads(text)
    except Exception as e:
        raise ValueError("这个 JSON Feed 读不出来：%s" % str(e)[:80])
    if not isinstance(doc, dict):
        raise ValueError("这个 JSON Feed 的结构不对")
    title = doc.get("title") or ""
    home = doc.get("home_page_url") or base
    link = doc.get("feed_url") or base
    entries = []
    for it in (doc.get("items") or []):
        if not isinstance(it, dict):
            continue
        link_e = _abs(it.get("url") or it.get("external_url") or "", home or base)
        summary = it.get("summary") or ""
        body = it.get("content_html") or it.get("content_text") or ""
        author = ""
        a = it.get("author")
        if isinstance(a, dict):
            author = a.get("name") or ""
        elif isinstance(a, str):
            author = a
        entries.append({
            "id": str(it.get("id") or ""),
            "guid": str(it.get("id") or ""),
            "title": _text_of(it.get("title") or ""),
            "link": link_e,
            "author": author,
            "published": str(it.get("date_published") or it.get("date_modified") or ""),
            "summary": _text_of(summary or body, link_e)[:SUMMARY_LIMIT],
            "content_html": sanitize_html(body or summary, link_e or home or base),
            "summary_html": sanitize_html(summary or body, link_e or home or base),
            "has_content": bool(it.get("content_html") or it.get("content_text")),
        })
    return {"title": title, "site": home, "kind": "json", "link": link, "entries": entries}


def _decode(blob):
    """字节 → 文本。先按 feedparser 认出来的编码，再退 utf-8，最后 GBK。

    GBK 那步不是可选项：国内不少老站点的 RSS 头里写 encoding=utf-8 但正文其实是
    GB2312，直接按 utf-8 errors=replace 会得到满屏『锟斤拷』。这里用替换解码先探，
    出现替换符再换 GBK 重试。
    """
    if isinstance(blob, str):
        return blob
    head = blob[:200].decode("ascii", errors="ignore")
    m = re.search(r'encoding=["\']([\w-]+)["\']', head, re.I)
    charset = m.group(1) if m else ""
    for enc in (charset, "utf-8"):
        if not enc:
            continue
        try:
            return blob.decode(enc)
        except Exception:
            continue
    txt = blob.decode("utf-8", errors="replace")
    if "\ufffd" in txt:
        try:
            gbk = blob.decode("gb18030")
            if gbk.count("\ufffd") < txt.count("\ufffd"):
                txt = gbk
        except Exception:
            pass
    return txt


# ── 发现 ────────────────────────────────────────────────────────────

_LINK_RE = re.compile(r"(?is)<link\b[^>]*>")


def _link_rel_alternate(raw, base=""):
    """HTML 里的 <link rel="alternate" type="application/rss+xml"> 之类。"""
    out = []
    for tag in _LINK_RE.findall(raw or ""):
        if not re.search(r'(?i)rel\s*=\s*["\']?[^"\'>]*alternate', tag):
            continue
        tm = re.search(r'(?i)type\s*=\s*["\']?([^"\'>\s]+)', tag)
        hm = re.search(r'(?i)href\s*=\s*["\']?([^"\'>\s]+)', tag)
        if not tm or not hm:
            continue
        kind = FEED_TYPES.get(tm.group(1).strip().lower())
        if not kind:
            continue
        title = ""
        tm2 = re.search(r'(?i)title\s*=\s*["\']([^"\']*)["\']', tag)
        if tm2:
            title = tm2.group(1).strip()
        out.append({"url": _abs(html.unescape(hm.group(1)), base), "title": title, "type": kind})
    return out


def _link_header(hdrs, base=""):
    """HTTP Link: 头里 rel=alternate 的那几个。"""
    raw = ""
    for k in ("link", "Link"):
        if k in hdrs:
            raw = hdrs[k]
            break
    out = []
    for part in re.split(r",(?=\s*<)", raw or ""):
        if not re.search(r'(?i)rel\s*=\s*"?alternate', part):
            continue
        um = re.search(r"<([^>]+)>", part)
        if not um:
            continue
        tm = re.search(r'(?i)type\s*=\s*"?([^"\';]+)', part)
        kind = FEED_TYPES.get((tm.group(1) if tm else "").strip().lower())
        if not kind:
            continue
        out.append({"url": _abs(um.group(1), base), "title": "", "type": kind})
    return out


def _looks_like_feed(blob):
    """字节开头像不像 feed：用来判断「本来就是个订阅地址」和探测成功与否。

    只认「根元素就是 feed」这一种。不能见着 <rdf 就当 RSS —— 博客页脚常夹一块
    创作共用的 <rdf:RDF> 授权声明（还裹在 HTML 注释里），一见就认会把整页 HTML
    当成订阅源，反而把页面里 <link alternate> 指着的真源挡在后面用不上。
    判据：feed 的根元素之前不会先出现 <!doctype / <html。
    """
    head = blob[:4096].lstrip()
    if head[:1] == b"{":
        try:
            doc = json.loads(_decode(blob))
        except Exception:
            return ""
        if isinstance(doc, dict) and ("items" in doc or "version" in doc and "json" in str(doc.get("version", ""))):
            return "json"
        return ""
    low = head.lower()
    html_at = [i for i in (low.find(b"<html"), low.find(b"<!doctype")) if i >= 0]
    first_html = min(html_at) if html_at else -1
    for tag, kind in ((b"<rss", "rss"), (b"<rdf:rdf", "rss"), (b"<feed", "atom")):
        i = low.find(tag)
        if i >= 0 and (first_html < 0 or i < first_html):
            return kind
    return ""


def discover(url):
    """一个地址 → 它的订阅源列表 [{"url","title","type"}]。

    顺序有讲究：自己就是 feed 就立刻返回（别再往下问一遍，省一次往返）；
    否则读 HTML 的 <link alternate>；再看 HTTP Link 头；最后才挨个探测常见路径。
    探测是最后手段，因为每个候选都是一次真实请求。
    """
    u = _check_url(url)
    seen = set()
    result = []

    def push(cand):
        cu = (cand.get("url") or "").strip()
        if not cu or cu in seen:
            return
        seen.add(cu)
        result.append({"url": cu, "title": cand.get("title") or "", "type": cand.get("type") or "rss"})

    # 1) 地址本身就是 feed
    try:
        blob, final, hdrs = _fetch_bytes(u)
        if hdrs.get("__304__"):
            blob = b""
        kind = _looks_like_feed(blob) if blob else ""
        if kind:
            push({"url": final or u, "title": "", "type": kind})
            return result
    except ValueError:
        raise
    except Exception:
        pass

    raw = ""
    try:
        blob, final, hdrs = _fetch_bytes(u)
        if not hdrs.get("__304__"):
            raw = _decode(blob)
    except Exception:
        final, hdrs = u, {}

    base = final or u
    for cand in _link_rel_alternate(raw, base):
        push(cand)
    if not result and hdrs:
        for cand in _link_header(hdrs, base):
            push(cand)
    if result:
        return result

    # 4) 探测常见路径
    p = urllib.parse.urlparse(base)
    root = "%s://%s/" % (p.scheme, p.netloc)
    for path in PROBE_PATHS:
        cand = urllib.parse.urljoin(root, path)
        try:
            blob, cfinal, chdrs = _fetch_bytes(cand)
        except Exception:
            continue
        kind = _looks_like_feed(blob)
        if kind:
            push({"url": cfinal or cand, "title": "", "type": kind})
            break
    return result


def _pick_best(cands):
    """从发现结果里挑一个：优先 rss，其次 atom，最后 json。"""
    if not cands:
        return None
    order = {"rss": 0, "atom": 1, "json": 2}
    return sorted(cands, key=lambda c: order.get(c.get("type"), 9))[0]


# ── 订阅管理 ────────────────────────────────────────────────────────

def subs():
    """所有订阅源，按加入顺序。"""
    data = _load()
    out = []
    for f in data.get("feeds", []):
        fid = f.get("id")
        ents = data.get("entries", {}).get(fid, [])
        unread = sum(1 for e in ents if not e.get("read"))
        out.append({
            "id": fid,
            "url": f.get("url", ""),
            "site": f.get("site", ""),
            "title": f.get("title", ""),
            "kind": f.get("kind", "rss"),
            "folder": f.get("folder", ""),
            "unread": unread,
            "last_fetched": f.get("last_fetched", 0),
            "error": f.get("error", ""),
        })
    return out


def add(url, folder=""):
    """订阅一个地址：发现 → 挑一个最好的 → 抓一次 → 落盘。"""
    cands = discover(url)
    best = _pick_best(cands)
    if not best:
        raise ValueError("这个地址上没找到订阅源（RSS / Atom / JSON Feed 都没有）")
    feed_url = best["url"]
    blob, final, hdrs = _fetch_bytes(feed_url)
    if not blob:
        raise ValueError("这个订阅取回来是空的")
    parsed = parse_feed(blob, final)
    fid = _new_id(feed_url)

    data = _load()
    for f in data["feeds"]:
        if f["id"] == fid:
            raise ValueError("这个源已经订过了")
    feed = {
        "id": fid,
        "url": feed_url,
        "site": parsed.get("site") or parsed.get("link") or feed_url,
        "title": best.get("title") or parsed.get("title") or final,
        "kind": parsed.get("kind") or best.get("type") or "rss",
        "folder": folder or "",
        "last_fetched": _now(),
        "error": "",
        "etag": hdrs.get("etag", ""),
        "modified": hdrs.get("last-modified", ""),
    }
    data["feeds"].append(feed)
    data["seen"].setdefault(fid, {})
    data["entries"][fid] = []
    _merge_entries(data, fid, parsed.get("entries", []))
    _trim(data, fid)
    _save(data)
    return feed


def remove(feed_id):
    """删掉一个源：连它的条目和 seen 表一起清。"""
    data = _load()
    before = len(data["feeds"])
    data["feeds"] = [f for f in data["feeds"] if f.get("id") != feed_id]
    data["entries"].pop(feed_id, None)
    data["seen"].pop(feed_id, None)
    if len(data["feeds"]) == before:
        return False
    _save(data)
    return True


def mark(feed_id, title=None, folder=None):
    """改源的标题或分组。改完返回新的那条。"""
    data = _load()
    for f in data["feeds"]:
        if f.get("id") == feed_id:
            if title is not None:
                f["title"] = title
            if folder is not None:
                f["folder"] = folder
            _save(data)
            return f
    raise ValueError("没有这个订阅源")


# ── 抓取 ────────────────────────────────────────────────────────────

def _merge_entries(data, fid, items):
    """把抓来的条目并进库，返回新增条数。

    去重靠 seen 表（guid → 时间戳）：同一 guid 第二次出现就跳过，即使标题改过。
    已存在的条目保留原 read / pushed 状态，只更新 content_hash 与正文，
    这样「编辑过的文章」能刷新，用户已读标记又不会被清掉。
    """
    ents = data["entries"].setdefault(fid, [])
    seen = data["seen"].setdefault(fid, {})
    index = {e.get("id"): e for e in ents}
    new = 0
    for item in items:
        key = _entry_key(item)
        if not key:
            continue
        chash = hashlib.sha1((item.get("content_html") or "").encode("utf-8")).hexdigest()
        if key in seen and key in index:
            old = index[key]
            if old.get("content_hash") != chash:
                old["content_hash"] = chash
                old["summary"] = item.get("summary", old.get("summary", ""))
                old["content_html"] = item.get("content_html", old.get("content_html", ""))
            seen[key] = _now()
            continue
        ts = _to_ts(item.get("published"))
        ents.append({
            "id": key,
            "title": item.get("title", ""),
            "link": item.get("link", ""),
            "author": item.get("author", ""),
            "published": item.get("published", ""),
            "published_ts": ts,
            "summary": item.get("summary", ""),
            "content_html": item.get("content_html", ""),
            "content_hash": chash,
            "has_content": bool(item.get("has_content")),
            "read": False,
            "pushed": "",
            "fetched_at": _now(),
        })
        seen[key] = _now()
        new += 1
    ents.sort(key=lambda e: e.get("published_ts") or e.get("fetched_at") or 0, reverse=True)
    return new


def _to_ts(s):
    """发布时间的粗略解析成 epoch。解不出就返回 0（排序时排最后）。

    两种格式覆盖绝大多数源：ISO 8601（Atom / JSON Feed）与 RFC 822（老式 RSS）。
    都不认就当没写时间，不要抛异常把整条条目带崩。
    """
    s = (s or "").strip()
    if not s:
        return 0
    try:
        from datetime import datetime
        return int(datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp())
    except Exception:
        pass
    try:
        from email.utils import parsedate_to_datetime
        return int(parsedate_to_datetime(s).timestamp())
    except Exception:
        return 0


def _trim(data, fid):
    """每个源只留最近 MAX_ENTRIES 条，多出来的从最旧开始丢（seen 表保留）。"""
    ents = data["entries"].get(fid) or []
    if len(ents) > MAX_ENTRIES:
        data["entries"][fid] = ents[:MAX_ENTRIES]


def refresh(feed_id=None, force=False):
    """刷新订阅，返回 {feeds, new, errors}。一个源失败不影响其余。

    force=False 时带上 ETag / Last-Modified 做条件请求，304 就跳过解析
    （省流量也省时间，但状态仍要更新，否则「上次刷新」看着像没动）。
    """
    data = _load()
    targets = [f for f in data["feeds"] if feed_id is None or f.get("id") == feed_id]
    if feed_id is not None and not targets:
        raise ValueError("没有这个订阅源")
    new_total = 0
    errors = []
    for f in targets:
        fid = f["id"]
        headers = {}
        if not force:
            if f.get("etag"):
                headers["If-None-Match"] = f["etag"]
            if f.get("modified"):
                headers["If-Modified-Since"] = f["modified"]
        try:
            blob, final, hdrs = _fetch_bytes(f["url"], headers)
            f["last_fetched"] = _now()
            if hdrs.get("__304__") or not blob:
                f["error"] = ""
                continue
            parsed = parse_feed(blob, final)
            if parsed.get("title") and not f.get("title"):
                f["title"] = parsed["title"]
            if parsed.get("site"):
                f["site"] = parsed["site"]
            f["kind"] = parsed.get("kind") or f.get("kind") or "rss"
            f["etag"] = hdrs.get("etag", "")
            f["modified"] = hdrs.get("last-modified", "")
            new_total += _merge_entries(data, fid, parsed.get("entries", []))
            _trim(data, fid)
            f["error"] = ""
        except Exception as e:
            f["error"] = str(e)[:200] or "刷新失败"
            errors.append({"feed_id": fid, "error": f["error"]})
    _save(data)
    return {"feeds": len(targets), "new": new_total, "errors": errors}


# ── 读取 ────────────────────────────────────────────────────────────

def entries(feed_id=None, unread_only=False, limit=200, q=""):
    """条目列表，最新的在前。q 是标题/摘要里的关键词。"""
    data = _load()
    titles = {f["id"]: f.get("title", "") for f in data.get("feeds", [])}
    out = []
    keyword = (q or "").strip().lower()
    ids = [feed_id] if feed_id else list(data.get("entries", {}).keys())
    for fid in ids:
        for e in data.get("entries", {}).get(fid, []):
            if unread_only and e.get("read"):
                continue
            if keyword:
                hay = (e.get("title", "") + " " + e.get("summary", "")).lower()
                if keyword not in hay:
                    continue
            out.append(_entry_view(e, fid, titles.get(fid, "")))
    out.sort(key=lambda e: e.get("published_ts") or 0, reverse=True)
    if limit and limit > 0:
        out = out[:limit]
    return out


def _entry_view(e, fid, feed_title):
    return {
        "id": e.get("id", ""),
        "feed_id": fid,
        "feed_title": feed_title,
        "title": e.get("title", ""),
        "link": e.get("link", ""),
        "author": e.get("author", ""),
        "published": e.get("published", ""),
        "summary": e.get("summary", ""),
        "has_content": bool(e.get("has_content")),
        "read": bool(e.get("read")),
        "pushed": e.get("pushed", "") or "",
    }


def _find(data, entry_id):
    for fid, lst in (data.get("entries") or {}).items():
        for e in lst:
            if e.get("id") == entry_id:
                return fid, e
    return None, None


def entry(entry_id):
    """单条完整记录：带清洗过的 content_html / content_text。"""
    data = _load()
    fid, e = _find(data, entry_id)
    if not e:
        raise ValueError("没有这条订阅")
    titles = {f["id"]: f.get("title", "") for f in data.get("feeds", [])}
    view = _entry_view(e, fid, titles.get(fid, ""))
    view["content_html"] = e.get("content_html", "")
    view["content_text"] = _text_of(e.get("content_html", "") or e.get("summary", ""), e.get("link", ""))
    return view


def mark_read(entry_ids, read=True):
    """把一批条目标成已读（或未读），返回真正改动的条数。"""
    if isinstance(entry_ids, str):
        entry_ids = [entry_ids]
    wanted = set(entry_ids or [])
    if not wanted:
        return 0
    data = _load()
    n = 0
    for lst in (data.get("entries") or {}).values():
        for e in lst:
            if e.get("id") in wanted and bool(e.get("read")) != bool(read):
                e["read"] = bool(read)
                n += 1
    _save(data)
    return n


def to_shelf(entry_id, out_dir):
    """一条订阅 → 书库里的一本书。返回 book_import 那套 info。

    两条路：正文够长就直接当 Markdown 入库（订阅本来就给了全文，不用再敲一次
    对方站点）；只有摘要碎片时才回退去抓原链接，走 clip_article.extract 那条现成
    的正文提取。走哪条由 has_content 决定，用户不必知道差别。
    """
    import book_import
    import clip_article

    data = _load()
    fid, e = _find(data, entry_id)
    if not e:
        raise ValueError("没有这条订阅")
    feed = next((f for f in data.get("feeds", []) if f["id"] == fid), {})
    site = feed.get("site") or ""
    link = e.get("link", "")

    if e.get("has_content") and e.get("content_html"):
        body_md = _html_to_md(e.get("content_html", ""), link)
    else:
        art = clip_article.extract(link)
        body_md = art.get("markdown", "")
        site = art.get("site") or site
    if not body_md.strip():
        raise ValueError("这条订阅里没读出正文")

    title = e.get("title") or "订阅条目"
    head = "> 订自 %s" % (site or "订阅")
    if e.get("published"):
        head += "　·　%s" % e["published"]
    if link:
        shown = (link[:60] + "…") if len(link) > 60 else link
        head += "\n> 原文：[%s](%s)" % (shown, link)
    body = "# %s\n\n%s\n\n%s\n" % (title, head, body_md)
    name = re.sub(r'[\\/:*?"<>|]', "", title)[:60] or "feed"
    words = len(re.sub(r"\s", "", re.sub(r"[#>*`_~\-|=\[\]()!]", "", body_md)))

    info = book_import.import_book(
        out_dir, "%s.md" % name, body.encode("utf-8"),
        title=title, author=e.get("author") or site,
        book_id_prefix="feed", source="feed")

    mp = os.path.join(info["dir"], "meta.json")
    meta = {}
    try:
        with open(mp, encoding="utf-8") as f:
            meta = json.load(f)
    except Exception:
        meta = {}
    meta.update({"source": "feed", "format": "clip", "url": link, "site": site,
                 "date": e.get("published", ""), "words": words, "chars": words,
                 "feed_id": fid, "feed_title": feed.get("title", ""),
                 "clipped_at": time.strftime("%Y-%m-%d %H:%M:%S")})
    tmp = mp + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)
    os.replace(tmp, mp)

    bid = info.get("id", "")
    for lst in (data.get("entries") or {}).values():
        for it in lst:
            if it.get("id") == entry_id:
                it["pushed"] = bid
                it["read"] = True
    _save(data)
    info.update({"url": link, "site": site, "date": e.get("published", ""),
                 "words": words, "chars": words, "source": "feed"})
    return info


_MD_IMG = re.compile(r"(?is)<img\b[^>]*>")


def _html_to_md(raw, base=""):
    """清洗过的 HTML → 粗 Markdown。够读就行，不追求语义完整。"""
    html_txt = sanitize_html(raw, base)
    html_txt = re.sub(r"(?i)<br\s*/?>", "\n", html_txt)
    html_txt = re.sub(r"(?i)</p\s*>", "\n\n", html_txt)
    html_txt = re.sub(r"(?i)<h([1-6])[^>]*>(.*?)</h\1>",
                      lambda m: "\n" + "#" * int(m.group(1)) + " " + m.group(2).strip() + "\n",
                      html_txt, flags=re.S)
    html_txt = re.sub(r"(?i)<li[^>]*>(.*?)</li>", lambda m: "\n- " + _TAG.sub("", m.group(1)).strip(),
                      html_txt, flags=re.S)

    def img(m):
        tag = m.group(0)
        sm = re.search(r'(?i)src\s*=\s*"([^"]*)"', tag)
        if sm and sm.group(1) and sm.group(1) != "#":
            return "\n![图](%s)\n" % sm.group(1)
        return ""
    html_txt = _MD_IMG.sub(img, html_txt)
    html_txt = re.sub(r'(?i)<a\b[^>]*href\s*=\s*"([^"]*)"[^>]*>(.*?)</a>',
                      lambda m: "[%s](%s)" % (_TAG.sub("", m.group(2)).strip(), m.group(1))
                      if m.group(1) != "#" else _TAG.sub("", m.group(2)).strip(),
                      html_txt, flags=re.S)
    html_txt = re.sub(r"(?i)</?(div|section|article|table|tr|ul|ol|blockquote)[^>]*>",
                      "\n", html_txt)
    text = _TAG.sub("", html_txt)
    text = html.unescape(text)
    text = re.sub(r"[ \t\u00a0]+", " ", text)
    text = re.sub(r"\n[ \t]+", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


if __name__ == "__main__":          # 手动冒烟：python3 feed.py <站点或订阅地址>
    import sys
    if len(sys.argv) < 2:
        print("用法: python3 feed.py <url> | --list | --refresh")
        sys.exit(1)
    arg = sys.argv[1]
    if arg == "--list":
        print(json.dumps(subs(), ensure_ascii=False, indent=2))
    elif arg == "--refresh":
        print(json.dumps(refresh(), ensure_ascii=False, indent=2))
    else:
        for c in discover(arg):
            print(json.dumps(c, ensure_ascii=False))
