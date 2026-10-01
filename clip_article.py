# -*- coding: utf-8 -*-
"""把一篇网页文章（尤其微信公众号推文）剪成一本能读的书。

为什么单独一个文件：抓取与解析是「外部不可信内容」的入口，跟取书（走浏览器
抓 Canvas）完全不是一条路，混在 ui_server 里会把两件事的复杂度叠在一起。

产出直接复用 book_import：正文转成 Markdown 之后当成一份 .md 导进书库，于是
章节切分、图片引用归一、_catalog.json、meta.json 全都沿用现成那套，阅读器、
导出 EPUB/PDF、定位文件这些能力一行都不用改就能用在剪藏进来的文章上。

正文是不可信的外部内容：这里只把它拆成 Markdown 文本，不执行、不解释里面的
任何东西；脚本与样式整块丢掉，javascript: / data: / vbscript: 这类链接一律丢，
相对地址补成绝对（否则存下来的书点链接是坏的）。
"""

import html
import html.parser
import json
import os
import re
import time
import urllib.parse
import urllib.request

import book_import

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
      "(KHTML, like Gecko) Version/17.0 Safari/605.1.15")
MAX_BYTES = 6 * 1024 * 1024          # 一篇推文不至于更大；超了就是抓错了东西
TIMEOUT = 20
MIN_WORDS = 60                       # 少于这么多字，基本可以断定没抓到正文

# 只认这两种 scheme：挡掉 file://、data:、gopher:// 之类被喂进来的地址
ALLOWED_SCHEMES = ("http", "https")
# 本机与内网地址一律拒绝。服务只听 127.0.0.1，但这一跳是我们替用户主动发出的
# 请求，不拦的话粘一个 http://192.168.1.1/admin 就等于替用户去敲内网的门。
BLOCKED_HOSTS = re.compile(
    r"^(localhost|0\.0\.0\.0|127\.|10\.|192\.168\.|169\.254\."
    r"|172\.(1[6-9]|2\d|3[01])\.|::1|\[::1\])", re.I)
# 站点反爬/要客户端的几种典型页面，认出来了直接说人话，别把提示页当正文剪进书库
BLOCKED_PAGE = re.compile(r"(当前环境异常|请在微信客户端打开|完成验证后即可继续访问|"
                          r"该公众号因违规已停止使用|链接已经过期|参数错误)")

# 正文锚点，从最具体到最通用，命中即用
BODY_ANCHORS = (
    ("id", "js_content"),                    # 微信公众号正文容器
    ("class", "rich_media_content"),
    ("id", "article-content"),
    ("class", "article-content"),
    ("class", "post-content"),
    ("class", "entry-content"),
    ("class", "markdown-body"),              # GitHub / 各类 README
    ("id", "content"),
    ("tag", "article"),
    ("tag", "main"),
)
# 这些标签整块不要：导航、评论、推荐、广告、表单、媒体外壳、脚本样式
DROP_TAGS = {"nav", "footer", "aside", "header", "form", "iframe", "noscript",
             "svg", "canvas", "button", "input", "select", "textarea", "video",
             "audio", "source", "style", "script", "head", "title", "meta",
             "link", "template"}
DROP_HINT = re.compile(r"(comment|respond|related|recommend|sidebar|aside|nav-|"
                       r"menu|ad-|advert|share|footer|crumb|toolbar|qrcode|"
                       r"paywall|subscribe|origin-tracker)", re.I)
# 正文容器里面也会夹着这些区块（推荐、赞赏、二维码、评论、公众号名片），挑块时已经
# 过滤过一次，落进正文里还得再挖掉 —— 这里用窄口径，免得把正常段落误伤
DROP_INSIDE = re.compile(r"(comment|respond|related|recommend|appreciate|paywall|"
                         r"subscribe|qr.?code|origin-tracker|mpprofile|rich_media_tool|"
                         r"js_pc_qr_code|reward|ad-widget)", re.I)

_VOID = {"br", "hr", "img", "meta", "link", "input", "source", "col", "area", "base"}
# 结构分：这些标签出现一次，说明这块更像「有组织的文章」而不是链接列表
_STRUCT = {"p", "li", "h2", "h3", "h4", "blockquote", "pre", "td"}


class _Node:
    """够用的极简 DOM：只存标签、属性、子节点，不做样式与命名空间处理。"""

    __slots__ = ("tag", "attrs", "kids", "parent")

    def __init__(self, tag, attrs=None, parent=None):
        self.tag = tag
        self.attrs = attrs or {}
        self.kids = []
        self.parent = parent

    def add_text(self, s):
        self.kids.append(_Text(s, self))

    def walk(self):
        yield self
        for k in self.kids:
            if isinstance(k, _Node):
                yield from k.walk()

    def attr(self, key):
        return self.attrs.get(key) or ""

    @property
    def hint(self):
        """id + class 拼一起，用来判断这块像不像评论区/侧栏。"""
        return (self.attr("id") + " " + self.attr("class")).strip()

    def text(self):
        return "".join(t.text for t in _walk_all(self) if isinstance(t, _Text))

    def inner_text(self):
        return re.sub(r"[ \t]+", " ", self.text()).strip()

    def score(self):
        return _score(self)


class _Text:
    __slots__ = ("text", "parent")

    def __init__(self, text, parent=None):
        self.text = text
        self.parent = parent


def _walk_all(node):
    """深度遍历：元素与文本都产出。"""
    yield node
    if isinstance(node, _Node):
        for k in node.kids:
            yield from _walk_all(k)


def _score(node):
    """这块子树「像正文」的程度：文字量为主，段落结构加分。

    命中 DROP_HINT 的子块（评论区、相关推荐）整块不计分 —— 微信页面里那些「read
    的人也在看」的推荐卡片字数不少，不扣掉会抢走真正的正文。
    """
    if isinstance(node, _Text):
        return len(node.text.strip())
    n = 0
    if node.tag in _STRUCT:
        n += 12
    elif node.tag == "img":
        n += 8
    for k in node.kids:
        if isinstance(k, _Node) and DROP_HINT.search(k.hint):
            continue
        n += _score(k)
    return n


class _TreeBuilder(html.parser.HTMLParser):
    """按 HTMLParser 的事件流搭出 _Node 树；未闭合标签靠栈收敛，容错优先。"""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = _Node("#doc")
        self.stack = [self.root]
        self.title_parts = []
        self.metas = []
        self._in_title = 0

    @property
    def cur(self):
        return self.stack[-1]

    def handle_starttag(self, tag, attrs):
        d = {}
        for k, v in attrs:
            v = "" if v is None else v
            key = k.lower()
            if key == "class":
                d[key] = (d.get(key, "") + " " + v).strip()
            elif key not in d:        # 同名属性重复出现时取第一个
                d[key] = v
        node = _Node(tag, d, self.cur)
        self.cur.kids.append(node)
        if tag == "title":
            self._in_title += 1
        if tag == "meta":
            self.metas.append(d)
        if tag not in _VOID:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in _VOID and self.cur.tag == tag:
            self.stack.pop()

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = max(0, self._in_title - 1)
        # HTML 允许漏写闭合标签：往栈里找同名标签，找不到就当我们没看见这个结束
        for i in range(len(self.stack) - 1, 0, -1):
            if self.stack[i].tag == tag:
                del self.stack[i:]
                return

    def handle_data(self, data):
        if self._in_title:
            self.title_parts.append(data)
            return
        if not data.strip():
            return
        self.cur.add_text(data)

    def title_text(self):
        return re.sub(r"\s+", " ", "".join(self.title_parts)).strip()


def parse_html(raw):
    b = _TreeBuilder()
    try:
        b.feed(raw)
        b.close()
    except Exception:
        pass
    return b


def _meta(tree, *keys):
    """从 <meta> 里按 name/property 挑一个 content（og:title / description 之类）。"""
    want = [k.lower() for k in keys]
    for m in tree.metas:
        ident = (m.get("property") or m.get("name") or m.get("itemprop") or "").strip().lower()
        if ident in want:
            v = (m.get("content") or "").strip()
            if v:
                return v
    return ""


def _abs(u, base):
    if not u or not base:
        return u
    try:
        return urllib.parse.urljoin(base, u.strip())
    except Exception:
        return u


def _safe_href(href, base):
    """脚本式 URL 一律丢；相对地址补成绝对，否则存下来的书点链接是坏的。"""
    low = (href or "").strip().lower()
    if not low or low.startswith(("javascript:", "data:", "vbscript:")):
        return ""
    return _abs(href, base)


def _attr_str(d):
    parts = []
    for k, v in d.items():
        if v is None or k not in ("src", "href", "alt", "title"):
            continue        # 只带对渲染有意义的几个；style / class / 事件属性一概不留
        parts.append('%s="%s"' % (k, html.escape(str(v), quote=True)))
    return (" " + " ".join(parts)) if parts else ""


def _cell_text(cell, base):
    """格子里可能有段落与链接：压成一行，链接保留。"""
    parts = []
    for n in _walk_all(cell):
        if isinstance(n, _Text):
            parts.append(n.text.strip())
        elif n.tag == "a":
            t = re.sub(r"\s+", " ", n.inner_text()).strip()
            href = _safe_href(n.attr("href"), base)
            if t:
                parts.append("[%s](%s)" % (t, href) if href else t)
    return " ".join(p for p in parts if p).replace("|", "\\|")   # 竖线会切断表格列


def _table_to_md(node, base):
    """表格直接转 Markdown 管道表（book_import 那套只把 <table> 当换行，会摊成一坨）。

    第一行当表头；列数按最宽的一行对齐；合并单元格摊平 —— 对「剪下来能读」这个
    目标，摊平比整块丢掉好。
    """
    rows = []
    for tr in node.walk():
        if tr.tag != "tr":
            continue
        cells = []
        for c in tr.kids:
            if isinstance(c, _Node) and c.tag in ("td", "th"):
                try:
                    span = max(1, min(8, int(c.attr("colspan") or 1)))
                except ValueError:
                    span = 1
                cells.append(_cell_text(c, base))
                cells.extend([""] * (span - 1))
        if cells:
            rows.append(cells)
    if len(rows) < 2:
        return ""
    width = max(len(r) for r in rows)
    rows = [r + [""] * (width - len(r)) for r in rows]
    lines = ["| " + " | ".join(c or " " for c in rows[0]) + " |",
             "|" + "|".join([" --- "] * width) + "|"]
    for r in rows[1:]:
        lines.append("| " + " | ".join(c or " " for c in r) + " |")
    return "\n" + "\n".join(lines) + "\n"


def _serialized(node, base, tables):
    """把选中的子树还原成 HTML 片段，交给 book_import 那套 xhtml → Markdown。

    顺手做三件对「文章」有用的修正：微信图片是懒加载的（真地址在 data-src，
    不搬就一张都剩不下）；相对链接补绝对；表格抽出来换成占位符，转换完再塞回。
    """
    out = []
    _emit(node, out, base, tables)
    return "".join(out)


def _emit(node, out, base, tables):
    if isinstance(node, _Text):
        out.append(html.escape(node.text, quote=False))
        return
    tag = node.tag
    if tag in ("#doc", "#fragment"):
        for k in node.kids:
            _emit(k, out, base, tables)
        return
    if tag in DROP_TAGS:
        return
    if node.hint and DROP_INSIDE.search(node.hint):
        return
    if tag == "table":
        md = _table_to_md(node, base)
        if md:
            out.append("<p>@@T%d@@</p>" % len(tables))
            tables.append(md)
        return
    if tag == "br":
        out.append("<br>")
        return
    if tag == "a":
        href = _safe_href(node.attr("href"), base)
        if not href:
            # 没有可用链接的 <a>（微信拿它当锚点用）：只留文字，否则转出来是一对空方括号
            for k in node.kids:
                _emit(k, out, base, tables)
            return
        out.append('<a href="%s">' % html.escape(href, quote=True))
        for k in node.kids:
            _emit(k, out, base, tables)
        out.append("</a>")
        return
    attrs = {}
    if tag == "img":
        # 懒加载的图，src 里往往是 1px 占位（data:image/…），真地址在 data-src ——
        # 所以要在候选里挑第一个「不是 data:」的，而不是第一个非空
        src = ""
        for key in ("data-src", "data-lazy-src", "data-original", "data-croporisrc", "src"):
            v = (node.attr(key) or "").strip()
            if v and not v.lower().startswith("data:"):
                src = v
                break
        if not src:
            return                # 只剩占位图：留个坏链接不如不留
        attrs = {"src": _abs(src, base), "alt": node.attr("alt")}
    if tag in ("img", "hr"):
        out.append("<%s%s>" % (tag, _attr_str(attrs)))
        return
    out.append("<%s>" % tag)
    for k in node.kids:
        _emit(k, out, base, tables)
    out.append("</%s>" % tag)


def _restore_tables(md, tables):
    """占位符换回真表格。占位符单独成段，转换后正好占一行，直接串替换。"""
    for i, t in enumerate(tables):
        md = md.replace("@@T%d@@" % i, t)
    return re.sub(r"@@T\d+@@", "", md)


def _pick_body(tree):
    """挑出「这篇文章的正文」那一块：先按已知锚点找，找不到就按字数最多的一块。"""
    nodes = [n for n in tree.root.walk() if n.tag != "#doc"]
    for kind, val in BODY_ANCHORS:
        for n in nodes:
            if kind == "tag" and n.tag == val and not DROP_HINT.search(n.hint):
                return n
            if kind == "id" and n.attr("id") == val:
                return n
            if kind == "class" and val in n.attr("class"):
                return n
    best, best_score = None, 0
    for n in nodes:
        if n.tag not in ("div", "section", "article", "main"):
            continue
        if DROP_HINT.search(n.hint):
            continue
        s = n.score()
        if s > best_score:
            best, best_score = n, s
    return best or tree.root


def _clean(md):
    """把转换结果收干净：行尾空格、连续空行、堆叠的分隔线。"""
    md = re.sub(r"[ \t]+\n", "\n", md or "")
    md = re.sub(r"\n{3,}", "\n\n", md)
    md = re.sub(r"\n?(?:---\n){2,}", "\n---\n\n", md)
    return md.strip()


def _first_text(root, pred):
    """在子树里找第一个满足条件、且有文字的元素，返回它的纯文本。"""
    for n in root.walk():
        if isinstance(n, _Node) and pred(n):
            t = n.inner_text()
            if t:
                return t
    return ""


def _publish_time(tree, raw):
    """微信的时间藏在几处 JS 变量里，其次才是 meta；都认一遍。"""
    for pat in (r"var\s+createTime\s*=\s*'([^']+)'",
                r'var\s+ct\s*=\s*"?(\d{10})"?',
                r"var\s+publish_time\s*=\s*'([^']+)'",
                r'"publish_time"\s*:\s*"([^"]+)"',
                r'content="([\d\-T:+. ]{10,25})"\s+property="article:published_time"',
                r'property="article:published_time"\s+content="([^"]+)"'):
        m = re.search(pat, raw)
        if not m:
            continue
        v = m.group(1).strip()
        if v.isdigit() and len(v) == 10:
            return time.strftime("%Y-%m-%d %H:%M", time.localtime(int(v)))
        if v:
            return v
    return _meta(tree, "article:published_time", "og:time")


def fetch(url):
    """取回页面原文，返回 (raw_text, final_url)。任何失败都抛带人话的 ValueError。"""
    u = (url or "").strip()
    if not u:
        raise ValueError("链接是空的")
    p = urllib.parse.urlparse(u)
    if p.scheme.lower() not in ALLOWED_SCHEMES:
        raise ValueError("只认 http/https 链接（现在这个是 %s）" % (p.scheme or "空"))
    host = (p.hostname or "").lower()
    if not host:
        raise ValueError("这个链接没有主机名，打不开")
    if BLOCKED_HOSTS.match(host):
        raise ValueError("这是本机或内网地址，归藏不替你访问")
    req = urllib.request.Request(u, headers={
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9",
    })
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            final = r.geturl()
            blob = r.read(MAX_BYTES + 1)
            charset = r.headers.get_content_charset()
    except Exception as e:
        raise ValueError("打不开这个页面：%s" % str(e)[:120])
    if len(blob) > MAX_BYTES:
        raise ValueError("页面太大了（超过 6MB），不像是一篇文章")
    raw = blob.decode(charset or "utf-8", errors="replace")
    if "<" not in raw:
        raise ValueError("取回来的东西不像网页")
    return raw, final


def _word_count(md):
    """字数：去掉 Markdown 语法符号、链接地址与图片引用之后，剩下的非空白字符数。"""
    t = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", md)             # 图片
    t = re.sub(r"\[([^{\]]*)\]\([^)]*\)", r"\1", t)          # 链接只留文字
    t = re.sub(r"<[^>]+>", "", t)
    t = re.sub(r"[#>*`_~\-|=\[\]()!]", "", t)
    return len(re.sub(r"\s", "", t))


_PREVIEW_CACHE = {}          # url -> (抓到的时刻, 文章)
_PREVIEW_TTL = 600           # 十分钟内的预览直接复用
_CACHE_MAX = 40              # 一次剪二十篇也够放，再多就淘汰最早那批


def extract(url):
    """链接 → {title, author, site, date, cover, url, markdown, words}。

    界面上的顺序是「先解析看一眼正文，再收进书架」，两次都指向同一个链接。
    没有这层缓存就是把对方站点抓两遍：公众号常常第一次给正文、第二次就跳验证页，
    看起来像「预览明明有，一按收藏就抓不到」，其实是自己多敲了一次门。
    """
    hit = _PREVIEW_CACHE.get(url)
    if hit and time.time() - hit[0] < _PREVIEW_TTL:
        return hit[1]
    art = _extract_once(url)
    if len(_PREVIEW_CACHE) >= _CACHE_MAX:
        for k in sorted(_PREVIEW_CACHE, key=lambda x: _PREVIEW_CACHE[x][0])[:10]:
            _PREVIEW_CACHE.pop(k, None)
    _PREVIEW_CACHE[url] = (time.time(), art)
    return art


def _extract_once(url):
    raw, final = fetch(url)
    tree = parse_html(raw)
    body = _pick_body(tree)
    is_wx = "mp.weixin.qq.com" in (final or "")

    tables = []
    fragment = _serialized(body, final, tables)
    md = _clean(_restore_tables(book_import._md_from_xhtml(fragment), tables))
    words = _word_count(md)

    # 标题：微信有专门的 #activity-name；其次 og:title；最后页面标题去掉「- 公众号」尾巴
    title = ""
    if is_wx:
        title = _first_text(tree.root, lambda n: n.attr("id") == "activity-name")
    if not title:
        title = _meta(tree, "og:title", "twitter:title")
    if not title:
        title = tree.title_text()
        if len(title) > 20:
            title = re.sub(r"\s*[|｜–—-]\s*[^|｜]{2,24}$", "", title)
        title = title.strip()

    site, author = "", ""
    m = re.search(r"var\s+nickname\s*=\s*'([^']+)'", raw)
    if m:
        site = html.unescape(m.group(1)).strip()
    if is_wx:
        # 公众号名在 #js_name；正文里也可能被塞进 rich_media_meta_nickname 链接
        acct = _first_text(tree.root, lambda n: n.attr("id") == "js_name")
        if not acct:
            acct = _first_text(body,
                               lambda n: "rich_media_meta_nickname" in n.attr("class"))
        site = acct or site
        author = _first_text(body, lambda n: (
            "rich_media_meta_text" in n.attr("class") or "author" in n.attr("id").lower())
            and 0 < len(n.inner_text()) <= 30
            and n.inner_text() not in ("作者", "原创", "正文人"))

    if not site:
        site = _meta(tree, "og:site_name") or urllib.parse.urlparse(final).netloc
    if not author:
        a = _meta(tree, "article:author", "author")
        if not a:
            by = re.search(r'"author"\s*:\s*"([^"]{1,40})"', raw)
            a = html.unescape(by.group(1)) if by else ""
        author = a
    if author == site:
        author = ""                 # 署名和公众号同名时别在两处重复一遍

    if not title:
        title = "%s %s" % (site or "剪藏", time.strftime("%m-%d"))

    if words < MIN_WORDS:
        if BLOCKED_PAGE.search(raw):
            raise ValueError("这一页要验证、或只能在微信里打开，剪不出正文")
        raise ValueError("这一页里没读出正文（可能要登录，或本来就是图片卡片）")

    return {
        "title": title[:120],
        "author": (author or "")[:60],
        "site": (site or "")[:60],
        "date": _publish_time(tree, raw),
        "cover": _abs(_meta(tree, "og:image", "twitter:image"), final),
        "url": final,
        "markdown": md,
        "words": words,
    }


def _front_matter(art):
    """正文前面那段出处说明：来源、日期、原文链接，读的时候一眼能回溯。"""
    line = "> 剪自 %s" % (art["site"] or "网页")
    if art.get("date"):
        line += "　·　%s" % art["date"]
    url = art.get("url") or ""
    shown = (url[:60] + "…") if len(url) > 60 else url
    return "%s\n> 原文：[%s](%s)\n" % (line, shown, url)


def save_clip(out_dir, url, title="", author="", cover_dir=None):
    """剪一篇 → 书库里的一本「文章」。返回 book_import 那套 info（含 id、章数、字数）。"""
    art = extract(url)
    body = "# %s\n\n%s\n%s\n" % (art["title"], _front_matter(art), art["markdown"])
    name = re.sub(r'[\\/:*?"<>|]', "", art["title"])[:60] or "clip"

    info = book_import.import_book(
        out_dir, "%s.md" % name, body.encode("utf-8"),
        title=(title or "").strip() or art["title"],
        author=(author or "").strip() or art["author"] or art["site"],
        cover_dir=cover_dir, book_id_prefix="clip", source="clip")

    # meta 里补剪藏专属字段：来源链接要能一键回原文，source=clip 让书架分组显示
    mp = os.path.join(info["dir"], "meta.json")
    meta = {}
    try:
        with open(mp, encoding="utf-8") as f:
            meta = json.load(f)
    except Exception:
        meta = {}
    meta.update({"source": "clip", "format": "clip", "url": art["url"],
                 "site": art["site"], "date": art["date"], "cover": art["cover"],
                 "words": art["words"],
                 # 字数按「正文字数」记，不按文件长度：Markdown 语法符号、图片地址和
                 # 出处那几行都不该算进「这篇有多少字」，否则预览说 5136 字、书架上
                 # 变 8153 字，用户只会以为程序在瞎报数。
                 "chars": art["words"],
                 "clipped_at": time.strftime("%Y-%m-%d %H:%M:%S")})
    tmp = mp + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)
    os.replace(tmp, mp)
    info.update({"url": art["url"], "site": art["site"], "date": art["date"],
                 "words": art["words"], "chars": art["words"],
                 "cover": art["cover"], "source": "clip"})
    return info


def clip_many(out_dir, items, cover_dir=None):
    """批量剪藏：items = [{url, title?, author?}]（也收裸链接），单条失败不影响其余。"""
    ok, fail = [], []
    for it in (items or []):
        url = (it.get("url") if isinstance(it, dict) else it) or ""
        try:
            ok.append(save_clip(
                out_dir, url,
                title=(it.get("title") or "") if isinstance(it, dict) else "",
                author=(it.get("author") or "") if isinstance(it, dict) else "",
                cover_dir=cover_dir))
        except Exception as e:
            fail.append({"url": str(url)[:200], "msg": str(e)[:160] or "剪不动这一篇"})
    return {"ok": ok, "fail": fail, "done": len(ok), "failed": len(fail)}


if __name__ == "__main__":      # 手动冒烟：python3 clip_article.py <文章链接> [--save]
    import sys
    if len(sys.argv) < 2:
        print("用法: python3 clip_article.py <url> [--save]")
        sys.exit(1)
    if "--save" in sys.argv:
        print(json.dumps(save_clip("output_clip_test", sys.argv[1]),
                         ensure_ascii=False, indent=2))
    else:
        a = extract(sys.argv[1])
        print(json.dumps({k: v for k, v in a.items() if k != "markdown"},
                         ensure_ascii=False, indent=2))
        print("---- 正文前 1200 字 ----")
        print(a["markdown"][:1200])
