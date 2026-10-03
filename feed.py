# -*- coding: utf-8 -*-
"""RSS / Atom / JSON Feed 阅读器：发现 → 抓取 → 解析 → 去重 → 入库 → 分组 / 筛选 / 撤销。

为什么单独一个文件：订阅和「取书」「剪藏」是三条不同的路。取书走浏览器抓 Canvas，
剪藏是一次性的单篇网页，订阅则是同一批站点反复回来取增量，需要条件请求（ETag /
Last-Modified）、条目级去重、每个源各自的错误隔离。混进 clip_article 的话，
「一次抓一篇」的假设会被「一批源里的某一个坏了也不能拖垮其余」打破。

阅读器的信息架构照 Miniflux / FreshRSS / NetNewsWire 那一套：源分组可排序、条目有
已读 / 收藏 / 稍后读 / 标签、列表能按这几个维度组合筛并全文搜索、批量操作可撤销、
订阅表用 OPML 进出。数据全在一份 <data>/feed.json 里（feeds / entries / seen /
settings 四个顶层键），读写都是整份载入 —— 单机几百个源的量，值不上数据库。

抓来的订阅内容是**不可信外部内容**：这里只把它解析成文本与链接，不执行、不解释。
feedparser 先做一轮（编码、相对地址、实体），这里再补一道更硬的过滤：script/style/
iframe 整块丢掉，on* 事件属性全部摘掉，javascript: / vbscript: / data: 这类地址一律
丢弃（data: 只放行图片）。截断到 500 条/源是为了让 JSON 文件不至于无限长。

没有后台线程、没有定时器：刷新只在被调用时发生（见 refresh，进度与中止都是调用方
传进来的回调），进程退出即停。撤销快照也只在本次进程的内存里活着（见 snapshot）。
"""

import functools
import gzip
import hashlib
import html
import json
import os
import re
import threading
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
SNAP_MAX = 8                           # 撤销快照最多留几份（一份 ≈ 一份 feed.json 的文本）
GROUP_LIMIT = 40                       # 组名长度上限，和界面输入框一个口径
TAG_LIMIT = 12                         # 单条最多挂几个标签，防手滑一次刷进几百个
AUTO_FULLTEXT_BATCH = 10               # 开了自动补全文时每轮最多回源补几条：补全文是一个链接
                                       # 一次请求，不设上限就等于替对方站点跑一次小规模爬取

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

DEFAULT_SETTINGS = {
    "fulltext": False,                              # 刷新时自动回源补全文（默认关，理由见 AUTO_FULLTEXT_BATCH）
    "policy": {"days": 0, "max_entries": 0,        # 保留策略：0 = 不启用（默认关闭）
               "keep_starred": True},
    "groups": [],                                   # 建过但暂时没源的组也记得住
}

# 组的默认名：盘上不写「（未分组）」这种中文常量，前端自己决定怎么显示
UNGROUPED = ""


def _blank():
    return {"feeds": [], "entries": {}, "seen": {}, "settings": _copy_settings({})}


def _copy_settings(raw):
    """把盘上的 settings 叠在默认值上：只认已知键，用户的其它字段原样留着。"""
    out = json.loads(json.dumps(DEFAULT_SETTINGS))
    if isinstance(raw, dict):
        for k, v in raw.items():
            if k == "policy" and isinstance(v, dict):
                out["policy"].update({kk: vv for kk, vv in v.items()
                                      if kk in out["policy"]})
            elif k in DEFAULT_SETTINGS:
                out[k] = v
    return out


def _norm_feed(f, idx=0):
    """老文件里的源缺字段就补默认值，未知字段一概不碰。

    group 是新键、folder 是老键，两者指同一件事：以 group 为准（缺 group 就拿 folder 填），
    回写时两个都写上，这样旧读出（ui_server 的 subs）和新读出都能读到同一个值。
    """
    f.setdefault("error", "")
    f.setdefault("etag", "")
    f.setdefault("modified", "")
    f.setdefault("last_fetched", 0)
    f.setdefault("kind", "rss")
    f.setdefault("url", "")
    f.setdefault("title", "")
    f.setdefault("site", "")
    if not f.get("group"):
        f["group"] = f.get("folder") or UNGROUPED
    f["folder"] = f["group"]
    if not isinstance(f.get("order"), int):
        f["order"] = idx
    return f


def _norm_entry(e):
    """条目级的缺字段兜底（收藏 / 稍后读 / 标签都是后加的键）。"""
    e.setdefault("read", False)
    e.setdefault("pushed", "")
    e.setdefault("starred", False)
    e.setdefault("later", 0)
    e.setdefault("tags", [])
    e.setdefault("content_md", "")
    e.setdefault("summary", "")
    e.setdefault("content_html", "")
    if not e.get("published_ts"):
        e["published_ts"] = _to_ts(e.get("published"))
    return e


def _normalize(data):
    for i, f in enumerate(data.get("feeds") or []):
        if isinstance(f, dict):
            _norm_feed(f, i)
    for lst in (data.get("entries") or {}).values():
        for e in lst or []:
            if isinstance(e, dict):
                _norm_entry(e)
    data["settings"] = _copy_settings(data.get("settings"))
    return data


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
    return _normalize(data)


def _save(data):
    os.makedirs(os.path.dirname(STORE) or ".", exist_ok=True)
    tmp = STORE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, STORE)             # 原子替换：中途崩了也不会留下半截文件


# 整份读 → 改内存 → 整份写，这条链不是原子的。后台刷新抓一轮要几十秒，这期间
# 界面上点的「标已读 / 打标签 / 删除」如果被刷新的那一次覆盖写冲掉，用户看到的就是
# 「点了没反应、标签存不上、整档标已读回 0 条」（实测复现过，见 tests/check_feed_ui.py）。
# 所以改盘的动作全部进这把锁；它是可重入的，函数之间互相调用（整档标已读前先存快照、
# OPML 导入里逐条订阅）不会自己把自己锁死。
# 只读（entries / subs / summary / export_opml…）不加锁：_save 走 os.replace，
# 读到的必是完整的旧文件或完整的新文件，不存在半截。
_LOCK = threading.RLock()


def _locked(fn):
    """整个函数抱锁跑 —— 只用在「不碰网络」的改盘动作上。"""
    @functools.wraps(fn)
    def wrapper(*args, **kw):
        with _LOCK:
            return fn(*args, **kw)
    return wrapper


def _write(apply):
    """锁内「重读整份 → 交给 apply 改 → 落盘」，回 apply 的返回值。

    要抓网络的调用方（订阅一个源 / 刷新 / 补全文 / 收进书架）都用它：抓取留在锁外做完，
    最后这一刀写回时才重读一遍 —— 抱着锁等网络会把界面上所有点选堵死，
    而不在锁里重读又会把等待期间用户改的东西整份覆盖掉。
    """
    with _LOCK:
        data = _load()
        out = apply(data)
        _save(data)
        return out


def _feed_row(data, feed_id):
    """按 id 找源那一行（返回的是当前这份库里的活对象，改了就算改库）。没订过回 None。"""
    for f in data.get("feeds") or []:
        if f.get("id") == feed_id:
            return f
    return None


def _now():
    return int(time.time())


def _settings(data=None):
    return (data or _load()).get("settings") or _copy_settings({})


def settings():
    """当前的阅读偏好（自动补全文开关 + 保留策略 + 组清单）。"""
    return _copy_settings(_settings())


@_locked
def set_settings(fulltext=None, policy=None, groups=None):
    """改偏好，返回改完的整份。policy 只覆盖传进来的那几个键。"""
    data = _load()
    s = data["settings"]
    if fulltext is not None:
        s["fulltext"] = bool(fulltext)
    if isinstance(policy, dict):
        for k, v in policy.items():
            if k in s["policy"]:
                s["policy"][k] = v
    if isinstance(groups, list):
        s["groups"] = [_clean_group_name(g) for g in groups if _clean_group_name(g)]
    _save(data)
    return _copy_settings(s)


def _clean_group_name(name):
    return str(name or "").strip()[:GROUP_LIMIT]


def _clean_tag(tag):
    return str(tag or "").strip()[:24]


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


# ── 撤销快照 ────────────────────────────────────────────────────────
#
# 生命周期（说清楚，免得前端以为刷新页面还能撤销）：快照只存在**本进程的内存**里，
# 上限 SNAP_MAX 份、超出丢最旧的，服务重启就全没了 —— 所以撤销按钮只在同一次运行里有意义。
# 为什么不落盘：一份快照 ≈ 整份 feed.json 的文本，落盘等于把「按一次批量」变成「写一份库」，
# 而批量误操作的救急窗口本来就是当下这几秒。
# 为什么是整库快照而不是反向操作记录：条目会被刷新改写、被去重合并，反向 diff 要处理
# 的边界比「恢复这一刻的库」多得多，而这里一次写几 KB 到几 MB 换来的是撤销永不失配。
# 代价：快照之后发生的其它改动也会被一并回退，所以撤销只在「刚做完批量」时用。
# 一个已知的角：源 id 由地址算出来，「退订再订同一个地址」拿回的是同一个 id，此刻内存里
# 那份快照指的是上一世的库。undo 用「快照里有源、盘上已经清空」当信号拒掉这种穿越 ——
# 正常库里总还剩几个源，只有清空过才会撞上，所以这个判断不会误伤日常的批量撤销。

_SNAPS = {}


@_locked
def snapshot(label=""):
    """给当前的库存一份快照，返回撤销令牌（交给 undo(token)）。"""
    token = hashlib.sha1(os.urandom(16)).hexdigest()[:12]
    _SNAPS[token] = {"label": str(label or "")[:60], "at": _now(),
                     "blob": json.dumps(_load(), ensure_ascii=False,
                                        separators=(",", ":"))}
    while len(_SNAPS) > SNAP_MAX:          # dict 保序，第一个就是最旧的
        _SNAPS.pop(next(iter(_SNAPS)), None)
    return token


def snapshots():
    """现有快照，最新的在前（前端「撤销」按钮的可用列表）。"""
    return [{"token": k, "label": v["label"], "at": v["at"]}
            for k, v in reversed(list(_SNAPS.items()))]


@_locked
def undo(token=None):
    """还原到某个快照；不传 token 就还原最近一份。用完即弃（快照不可重复撤销）。"""
    key = (token or "").strip() or (next(iter(reversed(list(_SNAPS))), ""))
    if not key or key not in _SNAPS:
        raise ValueError("没有可撤销的操作（快照最多留 %d 份，且只在本次运行里有效）" % SNAP_MAX)
    snap = _SNAPS.pop(key)
    try:
        data = json.loads(snap["blob"])
    except Exception:
        raise ValueError("这份快照坏了，还原不了")
    if not isinstance(data, dict):
        raise ValueError("这份快照坏了，还原不了")
    data.setdefault("feeds", [])
    data.setdefault("entries", {})
    data.setdefault("seen", {})
    if data["feeds"] and not (_load().get("feeds") or []):
        # 快照里有源、盘上却一个不剩：多半是「退订全部」之后拿老令牌来撤。那种情况用户要的是
        # 重新订阅，不是一份和现在对不上号的旧库 —— 不还原，但把快照留着，他还能改主意。
        _SNAPS[key] = snap
        raise ValueError("这份快照已经失效（订阅列表后来被清空过），就不还原了")
    _save(_normalize(data))
    return {"ok": True, "token": key, "label": snap["label"], "at": snap["at"],
            "feeds": len(data["feeds"]),
            "entries": sum(len(v or []) for v in data["entries"].values())}


@_locked
def clear_snapshots():
    """丢掉所有快照（退订清空、或界面关掉撤销提示时用）。"""
    n = len(_SNAPS)
    _SNAPS.clear()
    return n


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

SORT_ORDERS = {
    "manual": lambda f: (f.get("order") if isinstance(f.get("order"), int) else 10 ** 6,),
    "title": lambda f: (f.get("title") or "", f.get("order") or 0),
    "unread": lambda f: (-(f.get("unread") or 0), f.get("order") or 0),
    "recent": lambda f: (-(f.get("last_fetched") or 0), f.get("order") or 0),
}


def _feed_stats(data, f):
    """一个源的条目计数：未读 / 总数 / 收藏 / 稍后读。列侧栏每条都要显示，单独收一个函数。"""
    ents = data.get("entries", {}).get(f.get("id"), []) or []
    return {
        "total": len(ents),
        "unread": sum(1 for e in ents if not e.get("read")),
        "starred": sum(1 for e in ents if e.get("starred")),
        "later": sum(1 for e in ents if e.get("later")),
    }


def _feeds_sorted(data, order="manual", group=None):
    """按口径排好的源列表（每行都带 unread/total 之类统计，排序键直接用）。"""
    rows = []
    for f in data.get("feeds", []) or []:
        st = _feed_stats(data, f)
        g = f.get("group") or UNGROUPED
        if group is not None and g != (group or UNGROUPED):
            continue
        row = dict(f)
        row.update(st)
        row["group"] = g
        rows.append(row)
    key = SORT_ORDERS.get(order) or SORT_ORDERS["manual"]
    rows.sort(key=key)
    return rows


def subs(order="manual", group=None):
    """所有订阅源。默认按手动顺序（order），order 缺失时就是加入顺序。

    group="" 只要未分组的源，group=None 不过滤 —— 这两个不一样，别混。
    """
    data = _load()
    out = []
    for row in _feeds_sorted(data, order, group):
        out.append({
            "id": row.get("id"),
            "url": row.get("url", ""),
            "site": row.get("site", ""),
            "title": row.get("title", ""),
            "kind": row.get("kind", "rss"),
            "folder": row.get("group", UNGROUPED),
            "group": row.get("group", UNGROUPED),
            "order": row.get("order", 0),
            "unread": row.get("unread", 0),
            "total": row.get("total", 0),
            "starred": row.get("starred", 0),
            "later": row.get("later", 0),
            "last_fetched": row.get("last_fetched", 0),
            "error": row.get("error", ""),
        })
    return out


def add(url, folder="", group=None, order=None):
    """订阅一个地址：发现 → 挑一个最好的 → 抓一次 → 落盘。

    group 是新叫法、folder 是老叫法，给了 group 就以它为准。
    """
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

    # 上面那两次网络请求留在锁外，落盘这一刀才进锁：等网络的几秒里用户可能标了已读、
    # 改了标签，拿抓取前那一份旧库整份写回去就会把它们冲掉。
    def merge(data):
        for f in data["feeds"]:
            if f["id"] == fid:
                raise ValueError("这个源已经订过了")
        grp = _clean_group_name(group if group is not None else folder)
        feed = {
            "id": fid,
            "url": feed_url,
            "site": parsed.get("site") or parsed.get("link") or feed_url,
            "title": best.get("title") or parsed.get("title") or final,
            "kind": parsed.get("kind") or best.get("type") or "rss",
            "group": grp,
            "folder": grp,
            "order": order if isinstance(order, int) else len(data["feeds"]),
            "added_at": _now(),
            "last_fetched": _now(),
            "error": "",
            "etag": hdrs.get("etag", ""),
            "modified": hdrs.get("last-modified", ""),
        }
        data["feeds"].append(feed)
        data["seen"].setdefault(fid, {})
        data["entries"][fid] = []
        _remember_group(data, grp)
        _merge_entries(data, fid, parsed.get("entries", []))
        _trim(data, fid)
        return feed

    return _write(merge)


@_locked
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


@_locked
def mark(feed_id, title=None, folder=None, group=None, order=None):
    """改源的标题 / 分组 / 手动顺序。改完返回新的那条。"""
    data = _load()
    for f in data["feeds"]:
        if f.get("id") == feed_id:
            if title is not None:
                f["title"] = title
            if group is not None or folder is not None:
                g = _clean_group_name(group if group is not None else folder)
                _remember_group(data, g)
                f["group"] = g
                f["folder"] = g
            if order is not None:
                f["order"] = int(order)
            _save(data)
            return dict(f)
    raise ValueError("没有这个订阅源")


def set_feed_group(feed_id, group):
    """把一个源挪进某个组（group 传空串就是移出组）。"""
    return mark(feed_id, group=group if group is not None else UNGROUPED)


# ── 分组 ────────────────────────────────────────────────────────────

def _remember_group(data, name):
    """组清单里补一笔：建了但还没挂源的组也得留在侧栏里，否则改名会「改名即丢失」。"""
    name = _clean_group_name(name)
    if not name:
        return
    lst = data["settings"].setdefault("groups", [])
    if name not in lst:
        lst.append(name)


def groups(with_feeds=True):
    """每组有多少源 / 多少条 / 多少未读（按组名的手动顺序排，未分组永远排最前）。"""
    data = _load()
    names = list(data["settings"].get("groups") or [])
    index = {}
    order_seq = []
    for row in _feeds_sorted(data, "manual"):
        g = row.get("group") or UNGROUPED
        if g not in index:
            index[g] = {"group": g, "feeds": 0, "entries": 0, "unread": 0,
                        "starred": 0, "later": 0, "items": []}
            order_seq.append(g)
        bucket = index[g]
        bucket["feeds"] += 1
        bucket["entries"] += row.get("total", 0)
        bucket["unread"] += row.get("unread", 0)
        bucket["starred"] += row.get("starred", 0)
        bucket["later"] += row.get("later", 0)
        if with_feeds:
            bucket["items"].append({"id": row.get("id"), "title": row.get("title", ""),
                                    "url": row.get("url", ""), "site": row.get("site", ""),
                                    "unread": row.get("unread", 0),
                                    "total": row.get("total", 0),
                                    "error": row.get("error", ""),
                                    "order": row.get("order", 0)})
    for n in names:                       # 注册过但暂时没有源的组也要露面
        if n and n not in index:
            index[n] = {"group": n, "feeds": 0, "entries": 0, "unread": 0,
                        "starred": 0, "later": 0, "items": []}
            order_seq.append(n)
    out = []
    for g in order_seq:
        row = dict(index[g])
        row["name"] = row["group"]               # name/group 都给，前端叫哪个都能用
        # 源的逐源统计里条数叫 total（subs 一贯的键名），组这一层把 entries 再别名一次，
        # 免得前端在「源」和「组」之间切换时要记两套名字。
        row["total"] = row["entries"]
        if not with_feeds:
            row.pop("items", None)
        out.append(row)
    out.sort(key=lambda r: (r["group"] != UNGROUPED, order_seq.index(r["group"])))
    return out


@_locked
def group_create(name):
    """新建（或记住）一个组，返回组清单。"""
    name = _clean_group_name(name)
    if not name:
        raise ValueError("组名是空的")
    data = _load()
    _remember_group(data, name)
    _save(data)
    return groups(False)


@_locked
def group_rename(old, new):
    """改组名：连组里的源一起改。new 传空串等于把这组打散（源不删）。"""
    old = _clean_group_name(old)
    new = _clean_group_name(new)
    data = _load()
    n = 0
    for f in data["feeds"]:
        if (f.get("group") or UNGROUPED) == old:
            f["group"] = new
            f["folder"] = new
            n += 1
            _remember_group(data, new)
    lst = data["settings"].get("groups") or []
    if new:
        _remember_group(data, new)
    data["settings"]["groups"] = [g for g in lst if g != old]
    _save(data)
    return {"old": old, "group": new, "feeds": n, "groups": groups(False)}


@_locked
def group_delete(name, ungroup=True):
    """删组：默认只把源移出组（订阅还在）；ungroup=False 才会连源一起退订。"""
    name = _clean_group_name(name)
    if not name:
        raise ValueError("未分组不能删")
    data = _load()
    hit = [f["id"] for f in data["feeds"] if (f.get("group") or UNGROUPED) == name]
    if ungroup:
        for f in data["feeds"]:
            if (f.get("group") or UNGROUPED) == name:
                f["group"] = UNGROUPED
                f["folder"] = UNGROUPED
    else:
        data["feeds"] = [f for f in data["feeds"] if (f.get("group") or UNGROUPED) != name]
        for fid in hit:
            data["entries"].pop(fid, None)
            data["seen"].pop(fid, None)
    data["settings"]["groups"] = [g for g in (data["settings"].get("groups") or [])
                                  if g != name]
    _save(data)
    return {"group": name, "feeds": len(hit), "removed": (0 if ungroup else len(hit)),
            "groups": groups(False)}


@_locked
def reorder(feed_id, index=None, after=None):
    """在手动顺序里挪一个源：index 是绝对位置，after 是「挪到某个源后面」。

    存的是全局 order 整数（不是每组内的位置）：分过组之后再拖拽，组内看到的相对次序
    仍然由这个全局 order 决定，不用为每组另存一张表。返回排完的新 id 顺序。
    """
    data = _load()
    ids = [row["id"] for row in _feeds_sorted(data, "manual")]
    if feed_id not in ids:
        raise ValueError("没有这个订阅源")
    ids.remove(feed_id)
    pos = len(ids)
    if after:
        for i, fid in enumerate(ids):
            if fid == after:
                pos = i + 1
                break
    elif index is not None:
        pos = max(0, min(int(index), len(ids)))
    ids.insert(pos, feed_id)
    by_id = {f["id"]: f for f in data["feeds"]}
    for i, fid in enumerate(ids):
        f = by_id.get(fid)
        if f is not None:
            f["order"] = i
    _save(data)
    return ids


# ── 抓取 ────────────────────────────────────────────────────────────

def _merge_entries(data, fid, items, new_ids=None):
    """把抓来的条目并进库，返回新增条数。

    去重靠 seen 表（guid → 时间戳）：同一 guid 第二次出现就跳过，即使标题改过。
    已存在的条目保留原 read / pushed / starred / tags 状态，只更新 content_hash 与正文，
    这样「编辑过的文章」能刷新，用户已读标记又不会被清掉。
    new_ids 传一个列表就把这次新增的 id 收集起来（自动补全文只补新条目，不回头补旧的）。
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
        # 只看 seen，不看条目还在不在库里：用户删过的、被保留策略裁掉的都留在 seen 里，
        # 若顺手用 index 兜一次，下次刷新就把它们又搬回来了（「删了又冒出来」）。
        # 要真想重新收进来，用 delete_entries(purge_seen=True) 或 remove 后重新 add。
        if key in seen:
            if key in index:
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
            "content_md": "",
            "content_hash": chash,
            "has_content": bool(item.get("has_content")),
            "read": False,
            "starred": False,
            "later": 0,
            "tags": [],
            "pushed": "",
            "fetched_at": _now(),
        })
        seen[key] = _now()
        new += 1
        if new_ids is not None:
            new_ids.append(key)
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


def refresh(feed_id=None, force=False, group=None, progress=None, should_stop=None,
            fulltext=None, prune_after=None):
    """刷新订阅，返回 {feeds, new, errors, results, stopped, done, total}。一个源失败不影响其余。

    force=False 时带上 ETag / Last-Modified 做条件请求，304 就跳过解析
    （省流量也省时间，但状态仍要更新，否则「上次刷新」看着像没动）。
    group 只刷某一组；should_stop() 每刷完一个源之间问一次，说停就停，
    已经抓下来的条目照常落盘 —— 停在半路不该等于白刷。
    progress(done, total, label) 是给界面画进度条用的；回调里抛错不会影响刷新本身
    （界面挂了不该把用户的订阅一起拖挂）。

    一个源抓完就单独落一次盘：抓取在锁外，写回时才进锁重读一遍盘上那份 —— 于是刷新
    期间用户点的「已读 / 标签 / 删除」不会被这一轮抓完的整份覆盖冲掉，中途中止也早已
    把完成的源落住了（旧写法是抓完全程才写一次，等待期间的改动会整批丢掉）。
    """
    data = _load()
    want_group = group is not None
    targets = []
    for row in _feeds_sorted(data, "manual"):
        if feed_id is not None and row.get("id") != feed_id:
            continue
        if want_group and (row.get("group") or UNGROUPED) != (group or UNGROUPED):
            continue
        targets.append(row)
    if feed_id is not None and not targets:
        raise ValueError("没有这个订阅源")
    auto_fulltext = bool(_settings(data)["fulltext"] if fulltext is None else fulltext)
    policy = dict(_settings(data)["policy"])
    total = len(targets)
    new_total = 0
    errors = []
    results = []
    stopped = False
    for row in targets:
        if should_stop is not None:
            try:
                if should_stop():
                    stopped = True
                    break
            except Exception:
                pass                      # 探针坏了就当中止信号没用，继续刷，别把订阅带崩
        fid = row["id"]
        label = row.get("title") or row.get("url") or fid
        one = {"feed_id": fid, "title": label, "url": row.get("url", ""),
               "group": row.get("group") or UNGROUPED, "new": 0, "error": "",
               "not_modified": False, "fetched": False}
        headers = {}
        if not force:
            if row.get("etag"):
                headers["If-None-Match"] = row["etag"]
            if row.get("modified"):
                headers["If-Modified-Since"] = row["modified"]

        # ① 抓：这一步可能要几十秒，绝不抱着锁等网络。
        boom = None
        parsed = None
        hdrs = {}
        try:
            blob, final, hdrs = _fetch_bytes(row["url"], headers)
            one["fetched"] = True
            if hdrs.get("__304__") or not blob:
                one["not_modified"] = True
            else:
                parsed = parse_feed(blob, final)
        except Exception as e:
            boom = str(e)[:200] or "刷新失败"

        # ② 写：只并这一个源，其余部分以盘上那份为准（紧接着就落盘，不憋到最后）
        def merge(store):
            cur = _feed_row(store, fid)
            if cur is None:
                return []                     # 刷到一半被退订：抓回来的这一份丢掉，别把它塞回库
            cur["last_fetched"] = _now()
            if boom is not None:
                cur["error"] = boom
                return []
            cur["error"] = ""
            if parsed is None:
                return []                     # 304：没新东西，但「上次刷新」和错误状态照样要更新
            if parsed.get("title") and not cur.get("title"):
                cur["title"] = parsed["title"]
            if parsed.get("site"):
                cur["site"] = parsed["site"]
            cur["kind"] = parsed.get("kind") or cur.get("kind") or "rss"
            cur["etag"] = hdrs.get("etag", "")
            cur["modified"] = hdrs.get("last-modified", "")
            fresh = []
            _merge_entries(store, fid, parsed.get("entries", []), fresh)
            _trim(store, fid)
            return fresh

        fresh = _write(merge)
        if boom is not None:
            one["error"] = boom
            errors.append({"feed_id": fid, "error": boom})
        if fresh:
            one["new"] = len(fresh)
            new_total += len(fresh)
            if auto_fulltext:                 # 补全文也留在锁外抓，补完再一次写回
                want, _miss = _fulltext_targets(_load(), fresh, AUTO_FULLTEXT_BATCH, fid)
                got, _nf = _fulltext_fetch(want)
                if got:
                    _write(lambda store, rows=got: _fulltext_apply(store, rows))
        results.append(one)
        if progress is not None:
            try:
                progress(len(results), total, label)
            except Exception:
                pass
    if prune_after is None:
        do_prune = policy                     # 默认照盘上的策略走（默认值全 0 = 不裁）
    elif isinstance(prune_after, dict):
        do_prune = prune_after
    else:
        do_prune = policy if prune_after else {}
    pruned = _write(lambda store: _apply_policy(store, do_prune)) if do_prune else 0
    return {"feeds": len(results), "new": new_total, "errors": errors,
            "results": results, "stopped": stopped,
            "done": len(results), "total": total, "pruned": pruned}


# ── 读取 ────────────────────────────────────────────────────────────

def _as_ts(v):
    """把「时间」参数统一成 epoch：数字直接用，字符串按 ISO / RFC 822 解，解不出算 0。"""
    if isinstance(v, bool):
        return 0
    if isinstance(v, (int, float)):
        return int(v) if v > 0 else 0
    return _to_ts(v)


def _entry_ts(e):
    return e.get("published_ts") or e.get("fetched_at") or 0


def _haystack(e):
    """搜索用的那坨文本：标题 + 摘要 + 正文（HTML 去掉标签）+ 补来的全文。

    为什么要带上正文：Miniflux / NetNewsWire 的搜索都是「搜得到内容」而不是「搜得到标题」，
    只看标题的话「上周那篇讲 tokenizer 的」根本翻不出来。
    """
    parts = [e.get("title", ""), e.get("summary", ""), e.get("content_md", ""),
             e.get("author", "")]
    raw = e.get("content_html") or ""
    if raw:
        parts.append(_text_of(raw, e.get("link", "")))
    return " ".join(p for p in parts if p).lower()


def entries(feed_id=None, unread_only=False, limit=200, q="",
            starred=None, later=None, unread=None, group=None, tag=None,
            tag_match="any", since=None, until=None, sort="published_desc",
            offset=0, in_content=True):
    """条目列表，默认最新发布在前。老参数（feed_id/unread_only/limit/q）语义不变。

    筛选是**与**关系：group + tag + unread 同时生效。
    group="" 是「未分组」这个组，group=None 才是不过滤。
    tag 收字符串或列表，tag_match="all" 要求同时挂着这几个标签。
    since/until 比发布时间；解不出发布时间的老条目按入库时间算，两个时间都没有就当作
    最旧的挡掉 —— 宁可不给，也别把一条不知道哪年的东西当成最新的端出来。
    """
    data = _load()
    feeds = {f["id"]: f for f in data.get("feeds", [])}
    keyword = (q or "").strip().lower()
    if unread_only:
        unread = True
    lo, hi = _as_ts(since), _as_ts(until)
    want_tags = []
    if isinstance(tag, str):
        want_tags = [t for t in [_clean_tag(tag)] if t]
    elif tag:
        want_tags = [t for t in (_clean_tag(x) for x in tag) if t]

    out = []
    ids = [feed_id] if feed_id else list(data.get("entries", {}).keys())
    for fid in ids:
        feed = feeds.get(fid) or {}
        grp = feed.get("group") or UNGROUPED
        if group is not None and grp != (group or UNGROUPED):
            continue
        for e in data.get("entries", {}).get(fid, []) or []:
            if unread is not None and bool(e.get("read")) == bool(unread):
                continue
            if starred is not None and bool(e.get("starred")) != bool(starred):
                continue
            if later is not None and bool(e.get("later")) != bool(later):
                continue
            if want_tags:
                have = [str(t) for t in (e.get("tags") or [])]
                hit = [t for t in want_tags if t in have]
                if tag_match == "all" and len(hit) != len(want_tags):
                    continue
                if tag_match != "all" and not hit:
                    continue
            ts = _entry_ts(e)
            if lo and ts < lo:
                continue
            if hi and ts > hi:
                continue
            if keyword:
                hay = _haystack(e) if in_content else (
                    (e.get("title", "") + " " + e.get("summary", "")).lower())
                if keyword not in hay:
                    continue
            out.append(_entry_view(e, fid, feed.get("title", ""), grp))

    # 排序用视图里的 published_ts，没有发布时间就退回入库时间（和 _merge_entries 一致）
    def _vts(v):
        return v.get("published_ts") or v.get("fetched_at") or 0

    if sort == "published_asc":
        out.sort(key=_vts)
    elif sort == "added_desc":
        out.sort(key=lambda v: v.get("fetched_at") or 0, reverse=True)
    elif sort == "title_asc":
        out.sort(key=lambda v: (v.get("title") or "", _vts(v)))
    else:
        out.sort(key=_vts, reverse=True)

    if offset and offset > 0:
        out = out[offset:]
    if limit and limit > 0:
        out = out[:limit]
    return out


def count_entries(**kw):
    """按和 entries() 完全相同的筛法数一条条数（侧栏角标用，不含 limit）。"""
    kw["limit"] = 0
    kw["offset"] = 0
    return len(entries(**kw))


def _entry_view(e, fid, feed_title, group=UNGROUPED):
    later = e.get("later") or 0
    return {
        "id": e.get("id", ""),
        "feed_id": fid,
        "feed_title": feed_title,
        "group": group,
        "title": e.get("title", ""),
        "link": e.get("link", ""),
        "author": e.get("author", ""),
        "published": e.get("published", ""),
        "published_ts": e.get("published_ts") or 0,
        "fetched_at": e.get("fetched_at") or 0,
        "summary": e.get("summary", ""),
        "has_content": bool(e.get("has_content")),
        "has_fulltext": bool(e.get("content_md")),
        "read": bool(e.get("read")),
        "starred": bool(e.get("starred")),
        "later": bool(later),
        "later_at": int(later) if later else 0,
        "tags": list(e.get("tags") or []),
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
    feed = next((f for f in data.get("feeds", []) if f["id"] == fid), {})
    view = _entry_view(e, fid, feed.get("title", ""), feed.get("group") or UNGROUPED)
    view["feed_url"] = feed.get("url", "")
    view["site"] = feed.get("site", "")
    view["content_html"] = e.get("content_html", "")
    view["content_md"] = e.get("content_md", "")
    raw = e.get("content_html", "") or e.get("summary", "")
    view["content_text"] = (_text_of(raw, e.get("link", ""))
                            if not e.get("content_md") else e["content_md"])
    return view


@_locked
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


def _as_list(x):
    if x is None or x == "":
        return []
    if isinstance(x, str):
        return [x]
    return list(x or [])


def _scope(data, feed_id=None, group=None, fids=None):
    """按源 / 组圈定条目范围，返回 [(fid, entry)]。三个条件都空就是全库。"""
    want = set(_as_list(fids))
    out = []
    for f in data.get("feeds", []) or []:
        fid = f.get("id")
        if feed_id is not None and fid != feed_id:
            continue
        if want and fid not in want:
            continue
        if group is not None and (f.get("group") or UNGROUPED) != (group or UNGROUPED):
            continue
        for e in data.get("entries", {}).get(fid, []) or []:
            out.append((fid, e))
    return out


@_locked
def mark_read_all(feed_id=None, group=None, read=True, older_than=None, undo=True):
    """整源 / 整组 / 全部标记已读（或未读）。返回 {n, undo_token, scope}。

    默认顺手拍一份快照：一键「全部已读」是最容易后悔的操作，不给撤销等于赌用户手不抖。
    older_than 传时间（epoch 或日期串）就只标这个点之前的，「清掉旧账但不碰今天」。
    """
    token = snapshot("全部标记已读" if feed_id is None and group is None else "标记已读") if undo else ""
    data = _load()
    cutoff = _as_ts(older_than)
    n = 0
    for _fid, e in _scope(data, feed_id, group):
        if cutoff and _entry_ts(e) > cutoff:
            continue
        if bool(e.get("read")) != bool(read):
            e["read"] = bool(read)
            n += 1
    _save(data)
    return {"n": n, "undo_token": token,
            "scope": {"feed_id": feed_id, "group": group, "older_than": cutoff}}


@_locked
def _set_flag(entry_ids, flag, value):
    """批量写一个布尔 / 时间戳标记，返回真正改动的条数（值没变的不计）。"""
    wanted = set(_as_list(entry_ids))
    if not wanted:
        return 0
    data = _load()
    n = 0
    for lst in (data.get("entries") or {}).values():
        for e in lst:
            if e.get("id") not in wanted:
                continue
            new = value() if callable(value) else value
            if e.get(flag) != new:
                e[flag] = new
                n += 1
    _save(data)
    return n


@_locked
def mark_star(entry_ids, starred=True):
    """批量收藏 / 取消收藏，返回改动的条数。"""
    return _set_flag(entry_ids, "starred", bool(starred))


@_locked
def toggle_star(entry_id):
    """翻转一条的收藏状态，返回 {id, starred}。"""
    data = _load()
    _fid, e = _find(data, entry_id)
    if not e:
        raise ValueError("没有这条订阅")
    e["starred"] = not bool(e.get("starred"))
    _save(data)
    return {"id": entry_id, "starred": bool(e["starred"])}


@_locked
def mark_later(entry_ids, later=True):
    """批量放进 / 取出「稍后读」，返回改动的条数。存的是加入时间戳，方便按加入顺序读。"""
    return _set_flag(entry_ids, "later", (lambda: _now()) if later else 0)


@_locked
def toggle_later(entry_id):
    """翻转一条的稍后读状态，返回 {id, later, later_at}。"""
    data = _load()
    _fid, e = _find(data, entry_id)
    if not e:
        raise ValueError("没有这条订阅")
    e["later"] = _now() if not e.get("later") else 0
    _save(data)
    return {"id": entry_id, "later": bool(e["later"]), "later_at": int(e["later"] or 0)}


@_locked
def delete_entries(entry_ids, purge_seen=False, undo=True):
    """删条目。默认只删条目本身，seen 表留着 —— 下次刷新不会又把它抓回来，
    这才是「这条我不想再看见」的语义；purge_seen=True 时连去重记录一起清，
    等于「删错了想让它下次再出现在新条目里」（撤销时会连 seen 一起还原，所以不影响）。
    """
    wanted = set(_as_list(entry_ids))
    if not wanted:
        raise ValueError("没说要删哪条")
    token = snapshot("删除条目") if undo else ""
    data = _load()
    hit = []
    for fid, lst in (data.get("entries") or {}).items():
        keep = [e for e in lst if e.get("id") not in wanted]
        if len(keep) != len(lst):
            hit.extend(e.get("id") for e in lst if e.get("id") in wanted)
            data["entries"][fid] = keep
        if purge_seen and fid in data.get("seen", {}):
            for k in list(data["seen"][fid].keys()):
                if k in wanted:
                    data["seen"][fid].pop(k, None)
    _save(data)
    return {"n": len(hit), "ids": hit, "undo_token": token}


@_locked
def tag_add(entry_ids, tags):
    """给一批条目加标签（已有标签保留、不重复），返回改动的条数。"""
    add = [t for t in (_clean_tag(x) for x in _as_list(tags)) if t]
    if not add:
        return 0
    wanted = set(_as_list(entry_ids))
    if not wanted:
        return 0
    data = _load()
    n = 0
    for lst in (data.get("entries") or {}).values():
        for e in lst:
            if e.get("id") not in wanted:
                continue
            cur = [str(t) for t in (e.get("tags") or [])]
            merged = cur + [t for t in add if t not in cur]
            merged = merged[:TAG_LIMIT]
            if merged != cur:
                e["tags"] = merged
                n += 1
    _save(data)
    return n


@_locked
def tag_remove(entry_ids, tags=None):
    """去掉标签：tags 传具体标签就只删那几个，不传就是把这些条目的标签清空。"""
    wanted = set(_as_list(entry_ids))
    drop = None
    if tags not in (None, "", []):
        drop = set(t for t in (_clean_tag(x) for x in _as_list(tags)) if t)
    data = _load()
    n = 0
    for lst in (data.get("entries") or {}).values():
        for e in lst:
            if e.get("id") not in wanted:
                continue
            cur = [str(t) for t in (e.get("tags") or [])]
            new = [] if drop is None else [t for t in cur if t not in drop]
            if new != cur:
                e["tags"] = new
                n += 1
    _save(data)
    return n


@_locked
def tag_set(entry_id, tags):
    """一条条目的标签整盘替换（去重、截到 TAG_LIMIT 个），返回最终标签。"""
    tags = [t for t in dict.fromkeys(_clean_tag(x) for x in _as_list(tags)) if t]
    data = _load()
    _fid, e = _find(data, entry_id)
    if not e:
        raise ValueError("没有这条订阅")
    e["tags"] = tags[:TAG_LIMIT]
    _save(data)
    return list(e["tags"])


def tag_list():
    """标签清单：每个标签多少条、其中多少未读。标签不单独建表，从条目现算，
    所以删条目 / 撤销都不会留下悬空标签。"""
    data = _load()
    stat = {}
    for lst in (data.get("entries") or {}).values():
        for e in lst:
            for t in (e.get("tags") or []):
                t = str(t)
                row = stat.setdefault(t, {"tag": t, "count": 0, "unread": 0})
                row["count"] += 1
                if not e.get("read"):
                    row["unread"] += 1
    return sorted(stat.values(), key=lambda r: (-r["count"], r["tag"]))


def summary():
    """阅读器总览：源数 / 条数 / 未读 / 收藏 / 稍后读 / 分组 / 标签 / 偏好。

    分组统计直接复用 groups()，避免「侧栏每组未读」和「总览里的组」两处算法走岔。
    """
    data = _load()
    rows = _feeds_sorted(data, "manual")
    entries_all = [e for lst in (data.get("entries") or {}).values() for e in lst or []]
    return {
        "feeds": len(rows),
        "entries": len(entries_all),
        "unread": sum(1 for e in entries_all if not e.get("read")),
        "starred": sum(1 for e in entries_all if e.get("starred")),
        "later": sum(1 for e in entries_all if e.get("later")),
        "errors": sum(1 for r in rows if r.get("error")),
        "fulltext": bool(_settings(data)["fulltext"]),
        "policy": dict(_settings(data)["policy"]),
        "groups": groups(True),
        "tags": tag_list(),
    }


def _fetch_fulltext(link):
    """回源取正文 → Markdown。走 clip_article 那条现成的正文提取，不复述一遍启发式。

    它自带「本机 / 内网地址不去访问」的拦截，这里不绕过：订阅源是可以指向内网的，
    补全文等于替用户多打一次请求，那道拦截正是为了不让人把归藏当内网探针用。
    """
    import clip_article
    art = clip_article.extract(link) or {}
    md = (art.get("markdown") or "").strip()
    if not md:
        raise ValueError("这一条没读出正文")
    return md, (art.get("site") or "")


def _fulltext_targets(store, ids, limit=0, feed_id=None):
    """从这批 id 里挑出「值得回源」的，返回 (要抓的清单, 没链接直接记失败的)。

    抓取和写盘分开：这一刀只看盘上有什么、不碰网络，所以可以在锁外拿一份只读快照来算。
    id 全局唯一（条目 id 就是 guid 的散列），因此清单里带上 feed_id 只是为了落盘时好定位。
    """
    want = set(ids or [])
    rows, failed, done = [], [], 0
    for fid, e in _scope(store, feed_id):
        if e.get("id") not in want:
            continue
        if limit and done >= limit:
            break
        link = e.get("link") or ""
        if not link:
            failed.append({"id": e.get("id"), "error": "这条没有原文链接"})
            continue
        done += 1
        rows.append({"feed_id": fid, "id": e.get("id"), "link": link})
    return rows, failed


def _fulltext_fetch(rows):
    """锁外一条条回源：网络快慢不该占着写盘的锁，否则刷新期间界面点什么都没反应。"""
    ok, failed = [], []
    for r in rows:
        try:
            md, _site = _fetch_fulltext(r["link"])
        except Exception as ex:
            failed.append({"id": r["id"], "error": str(ex)[:160] or "补全文失败"})
            continue
        ok.append({"feed_id": r["feed_id"], "id": r["id"], "markdown": md})
    return ok, failed


def _fulltext_apply(store, got):
    """把抓回来的正文写进对应条目（存进 content_md），返回补成功的 id。"""
    md = {g["id"]: g["markdown"] for g in got or []}
    done = []
    for _fid, e in _scope(store):
        if e.get("id") in md:
            e["content_md"] = md[e["id"]]
            e["has_content"] = True
            done.append(e["id"])
    return done


def fetch_content(entry_ids=None, feed_id=None, group=None, only_missing=True, limit=50):
    """手动「补全文」：单条（entry_ids 传一个 id）和一批（传列表 / 按源按组圈定）都走这里。

    默认只补「只有摘要」的条目（only_missing），并且最多 50 条：这函数是一次一条请求地
    抓外站，全库无脑补一遍会把对方站点惹毛，也会让界面卡到像是挂了。limit=0 才是不限。
    返回 {ok, failed, skipped, total}。

    抓取全在锁外，最后一次性写回（写之前重读盘上那份）—— 补一批的几十秒里，
    用户标已读、打标签照常落得住。
    """
    data = _load()                      # 只拿来做规划，不当写回的底本
    wanted = set(_as_list(entry_ids))
    rows = _scope(data, feed_id, group)
    if wanted:
        rows = [(fid, e) for fid, e in rows if e.get("id") in wanted]
    if only_missing:
        rows = [(fid, e) for fid, e in rows if not e.get("content_md")]
    total = len(rows)
    cap = int(limit or 0)
    plans, failed = [], []
    for fid, e in rows:
        if cap > 0 and len(plans) + len(failed) >= cap:
            break
        link = e.get("link") or ""
        if not link:
            failed.append({"id": e.get("id"), "error": "这条没有原文链接"})
            continue
        plans.append({"feed_id": fid, "id": e.get("id"), "link": link})
    got, net_failed = _fulltext_fetch(plans)
    ok = _write(lambda store: _fulltext_apply(store, got)) if got else []
    return {"ok": ok, "failed": failed + net_failed, "n": len(ok),
            "skipped": max(0, total - len(ok) - len(failed) - len(net_failed)),
            "total": total}


def fulltext_enabled():
    """刷新时会不会自动回源补全文。"""
    return bool(_settings()["fulltext"])


@_locked
def set_fulltext(on):
    """打开 / 关闭自动补全文（开关存进 settings，默认关）。"""
    return set_settings(fulltext=on)["fulltext"]


# ── 清理 ────────────────────────────────────────────────────────────

def _prune_rows(data, days=0, max_entries=0, keep_starred=True,
                feed_id=None, group=None, dry_run=False):
    """按「条数 / 天数」裁掉旧条目，返回 (删掉多少, 每个源删了几条)。

    裁的是列表尾部：条目在 _merge_entries 里已按发布时间倒排，所以「留前 N 条」=「留最新 N 条」。
    收藏和稍后读默认保命 —— 那两个标记是用户主动按下的，清理不该替他忘掉。
    """
    cutoff = _now() - int(days or 0) * 86400 if days and days > 0 else 0
    cap = int(max_entries or 0) if max_entries and max_entries > 0 else 0
    by_feed = {}
    for f in data.get("feeds") or []:
        fid = f.get("id")
        if feed_id is not None and fid != feed_id:
            continue
        if group is not None and (f.get("group") or UNGROUPED) != (group or UNGROUPED):
            continue
        lst = data.get("entries", {}).get(fid) or []
        dropped = 0
        for i, e in enumerate(lst):
            ts = _entry_ts(e)
            out = bool(cutoff and ts < cutoff) or bool(cap and i >= cap)
            if out and keep_starred and (e.get("starred") or e.get("later")):
                out = False
            if out:
                dropped += 1
        if dropped:
            if not dry_run:
                data["entries"][fid] = [
                    e for i, e in enumerate(lst)
                    if not ((bool(cutoff and _entry_ts(e) < cutoff)
                             or bool(cap and i >= cap))
                            and not (keep_starred and (e.get("starred") or e.get("later"))))]
            by_feed[fid] = dropped
    return sum(by_feed.values()), by_feed


def _apply_policy(data, policy):
    """刷新尾部用的「按设置裁一刀」，就地改 data，返回删掉的条数。"""
    policy = policy or {}
    removed, _ = _prune_rows(data, policy.get("days") or 0, policy.get("max_entries") or 0,
                             policy.get("keep_starred", True))
    return removed


@_locked
def prune(days=None, max_entries=None, keep_starred=None, feed_id=None, group=None,
          dry_run=False, undo=True):
    """清理旧条目。不传参数就按 settings.policy 执行；策略没开（默认）就一条不动。

    dry_run=True 先看会删掉多少，别拿用户的订阅去试参数。
    """
    pol = _settings()["policy"]
    days = pol.get("days", 0) if days is None else days
    max_entries = pol.get("max_entries", 0) if max_entries is None else max_entries
    keep_starred = pol.get("keep_starred", True) if keep_starred is None else keep_starred
    if not (days and int(days) > 0) and not (max_entries and int(max_entries) > 0):
        return {"removed": 0, "by_feed": {}, "dry_run": bool(dry_run), "undo_token": "",
               "note": "没设保留策略（days / max_entries 都是 0），要裁就先在设置里开"}
    token = snapshot("清理旧条目") if undo else ""
    data = _load()
    removed, by_feed = _prune_rows(data, days, max_entries, keep_starred,
                                   feed_id, group, dry_run)
    if removed and not dry_run:
        _save(data)
    return {"removed": removed, "by_feed": by_feed, "dry_run": bool(dry_run),
            "undo_token": token}


# ── OPML ────────────────────────────────────────────────────────────

def _esc(v):
    return html.escape(str(v or ""), quote=True)


def export_opml(title="归藏订阅", include_empty=True):
    """订阅列表 → OPML 2.0 文本：组是嵌套 outline，未分组的源直接挂在 body 下。

    text / title 都写（老阅读器读 text，新阅读器读 title），category 也额外带一份：
    有的导入端把 outline 拍平，只认 category 不认嵌套。
    """
    data = _load()
    rows = _feeds_sorted(data, "manual")
    buckets = {}
    seq = []
    for r in rows:
        g = r.get("group") or UNGROUPED
        if g not in buckets:
            buckets[g] = []
            seq.append(g)
        buckets[g].append(r)
    for g in (data["settings"].get("groups") or []):
        if g and g not in buckets:
            buckets[g] = []
            seq.append(g)
    out = ['<?xml version="1.0" encoding="UTF-8"?>',
           '<opml version="2.0">',
           "  <head>",
           "    <title>%s</title>" % _esc(title),
           "    <dateCreated>%s</dateCreated>" % time.strftime("%a, %d %b %Y %H:%M:%S +0000",
                                                              time.gmtime()),
           "  </head>",
           "  <body>"]
    for g in [x for x in seq if not x]:
        for r in buckets[g]:
            out.append(_outline_feed(r, 2, ""))
    for g in [x for x in seq if x]:
        if not buckets[g] and not include_empty:
            continue
        out.append('    <outline text="%s" title="%s">' % (_esc(g), _esc(g)))
        for r in buckets[g]:
            out.append(_outline_feed(r, 3, g))
        out.append("    </outline>")
    out.append("  </body>")
    out.append("</opml>")
    return "\n".join(out) + "\n"


def _outline_feed(row, indent, group):
    pad = "  " * indent
    return ('%s<outline type="rss" text="%s" title="%s" xmlUrl="%s" htmlUrl="%s"'
            ' category="%s" sort="0"/>'
            % (pad, _esc(row.get("title") or row.get("url")), _esc(row.get("title")),
               _esc(row.get("url")), _esc(row.get("site")), _esc(group)))


def write_opml(path=None, title="归藏订阅"):
    """把 OPML 落到一个文件（默认 <数据目录>/订阅.opml），返回路径。"""
    p = os.path.expanduser(path or os.path.join(DATA_DIR, "订阅.opml"))
    os.makedirs(os.path.dirname(p) or ".", exist_ok=True)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(export_opml(title=title))
    os.replace(tmp, p)
    return p


def _opml_text(xml):
    """接受 bytes / XML 文本 / 文件路径三种输入，统一成文本。"""
    if isinstance(xml, (bytes, bytearray)):
        return _decode(bytes(xml))
    s = str(xml or "").strip()
    if not s:
        raise ValueError("OPML 是空的")
    if "<" in s:
        return s
    try:
        with open(os.path.expanduser(s), "rb") as f:
            return _decode(f.read())
    except Exception as e:
        raise ValueError("这既不像 OPML 文本，也不是一个能打开的文件：%s" % str(e)[:80])


def import_opml(xml, default_group="", dry_run=False):
    """OPML → 批量订阅。返回 {total, added, existed, failed, results, feeds}。

    results 逐条给结论：status 是 added / existed / failed / pending（dry_run），
    失败的带人话原因 —— 导入 200 个源里挂了 3 个时，用户要的是知道挂了哪 3 个。
    嵌套 outline 的组名用「父 / 子」拼出来，和多数阅读器把多层分类拉平的做法一致。
    已存在的源只比 id（URL 的 sha1），不再敲一次对方的门。
    """
    text = _opml_text(xml)
    # 别处下载的 OPML 属于不可信外部文件：xml.etree 不会去取外部实体，但**内部**实体
    # 展开照样能被「十亿笑」把内存撑爆，所以带实体声明的一律不解析（正常 OPML 没有 DTD）。
    low = text[:8192].lower()
    if "<!doctype" in low and "<!entity" in low:
        raise ValueError("这份 OPML 里声明了自定义实体，为安全不解析")
    try:
        root = ET.fromstring(text)
    except ET.ParseError as e:
        raise ValueError("这份 OPML 解析不了：%s" % str(e)[:80])
    cands = []

    def walk(node, path):
        url = (node.get("xmlUrl") or node.get("xmlurl") or node.get("url") or "").strip()
        name = (node.get("title") or node.get("text") or "").strip()
        if url and url.lower() != "file:///":
            cands.append({"url": url, "title": name,
                          "group": _clean_group_name(" / ".join(path))})
        nxt = path + ([name] if name and not url else [])
        for ch in list(node):
            if ch.tag.lower().split("}")[-1] == "outline":
                walk(ch, nxt)

    body = root.find("body")
    for ch in list(body if body is not None else root):
        walk(ch, [])
    if not cands:
        raise ValueError("这份 OPML 里没找到任何订阅地址（xmlUrl）")

    data = _load()
    have = set(f.get("id") for f in data.get("feeds", []))
    results = []
    added = existed = failed = 0
    for c in cands:
        one = {"url": c["url"], "title": c["title"], "group": c["group"],
               "status": "", "feed_id": "", "error": ""}
        try:
            _check_url(c["url"])
        except ValueError as e:
            one.update(status="failed", error=str(e))
            failed += 1
            results.append(one)
            continue
        fid = _new_id(c["url"])
        if fid in have:
            one.update(status="existed", feed_id=fid)
            existed += 1
            results.append(one)
            continue
        if dry_run:
            one.update(status="pending")
            results.append(one)
            continue
        try:
            feed = add(c["url"], group=c["group"] or _clean_group_name(default_group))
            one.update(status="added", feed_id=feed["id"], title=feed.get("title") or c["title"])
            have.add(feed["id"])
            added += 1
        except ValueError as e:
            one.update(status="failed", error=str(e)[:160])
            failed += 1
        except Exception as e:
            one.update(status="failed", error=("订阅失败：%s" % str(e)[:120]))
            failed += 1
        results.append(one)
    return {"total": len(cands), "added": added, "existed": existed, "failed": failed,
            "results": results, "feeds": subs()}


def to_shelf(entry_id, out_dir):
    """一条订阅 → 书库里的一本书。返回 book_import 那套 info。

    三条路，从「已经到手」往「还要再敲门」排：补过全文（content_md）直接用；
    订阅本身给了全文就把 HTML 转成 Markdown；只有摘要碎片才回退去抓原链接，
    走 clip_article.extract 那条现成的正文提取。用户不必知道差别。
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

    if e.get("content_md"):
        body_md = e["content_md"]
    elif e.get("has_content") and e.get("content_html"):
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

    # 回写「已收进书架」也走重读：上面 clip_article 抓取 + 落一本书可能要十几秒，
    # 拿抓取前那一份旧库写回去，会把这期间用户对别的条目做的事整份盖掉。
    def mark_pushed(store):
        n = 0
        for lst in (store.get("entries") or {}).values():
            for it in lst:
                if it.get("id") == entry_id:
                    it["pushed"] = bid
                    it["read"] = True
                    n += 1
        return n

    _write(mark_pushed)
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
