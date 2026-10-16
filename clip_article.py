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
import ipaddress
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

import book_import
import book_layout

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


def _legacy_ipv4(host):
    """inet_aton 认的那种老式 IPv4 写法，收成一个 32 位整数；不是就回 None。

    纯数字（2130706433）、十六进制（0x7f000001）、八进制（0177.0.0.1）、少写几段
    （127.1）这四种，浏览器和 getaddrinfo 都照样发到那个地址去，但

        urllib.parse 把它当成一个普通主机名，BLOCKED_HOSTS 那条按「点分十进制」
        写的正则一条都匹配不上 —— 内网那道门就从这儿漏过去了。

    域名里不会全是数字，所以「每一段都解析成数字」就可以放心当作 IPv4 收下。
    """
    parts = (host or "").split(".")
    if not 1 <= len(parts) <= 4:
        return None
    nums = []
    for p in parts:
        if not p:
            return None
        try:
            if p[:2].lower() == "0x":
                n = int(p, 16)
            elif len(p) > 1 and p[0] == "0":
                n = int(p, 8)
            else:
                n = int(p, 10)
        except ValueError:
            return None
        # 负数段（`-1.0.0.1`）不是 inet_aton 认的写法，得在这里就回掉：int() 收得下它，
        # 后面按位拼出来的也是个负数，交给 ipaddress.ip_address() 会当场抛 —— 护栏
        # 本该只回「放行 / 拒绝」，一抛就把取页路打成 500 或连接被甩。
        if n < 0:
            return None
        nums.append(n)
    # 前几段各占 8 位，最后一段占满剩下的位（单段写法就是整个 32 位）
    tail_bits = 8 * (5 - len(nums))
    if any(n > 255 for n in nums[:-1]) or nums[-1] > (1 << tail_bits) - 1:
        return None
    val = 0
    for n in nums[:-1]:
        val = (val << 8) | n
    return (val << tail_bits) | nums[-1]


_CGNAT = ipaddress.ip_network("100.64.0.0/10")     # 运营商级 NAT，同样不该由本机去敲


def _ip_blocked(ip):
    """这个 IP 该不该拒绝：回环 / 内网 / 链路本地 / 保留 / 组播 / 未指定一律算。"""
    if ip.version == 4 and ip in _CGNAT:
        return True
    return bool(ip.is_loopback or ip.is_private or ip.is_link_local
                or ip.is_reserved or ip.is_multicast or ip.is_unspecified)


def host_blocked(host):
    """这个主机名是不是本机 / 内网地址（拒绝替用户去敲）。

    先按字面认 IPv4 / IPv6（含各种非常规写法与 IPv4 映射的 IPv6），认不出来再退回
    那把按名字写的正则（localhost 这类）。域名带结尾的点（`localhost.`）也按同一个
    名字判，别让一个点把门绕开。

    百分号编码先解一次：这把尺子量的是**真正会连过去**的那个主机名，而不是
    `urlparse().hostname` 递过来的原样字符串 —— 后者不做解码（`http://%31%32%37.0.0.1/`
    原样留着 `%31%32%37.0.0.1`），而 `urllib.request.Request._parse` 会
    `unquote(self.host)`，socket 连的是 `127.0.0.1`。只认字面量就成了「看的是一串、
    连的是另一串」：`%31%32%37.0.0.1`、`%6c%6f%63%61%6c%68%6f%73%74`、
    `192.168.%31.1`、`169.254.169.%32%35%34` 这些解码后全是本机 / 内网 / 元数据地址，
    从这个口整批漏过去（剪藏取页、内置浏览器、平台解析、首图与封面兜底全走这里）。
    与 urllib 一样只解一次：`%2531…` 解出来仍是 `%31…`，DNS 认不出，不构成通路。

    解编码放在方括号剥离之前：`%5B::1%5D` 先变成 `[::1]`，后面那一步才认得出。
    """
    h = urllib.parse.unquote(str(host or ""))
    h = h.strip().lower()
    if h.startswith("[") and h.endswith("]"):     # 方括号包着的 IPv6 字面量
        h = h[1:-1]
    h = h.rstrip(".")
    if not h:
        return True
    if BLOCKED_HOSTS.match(h):
        return True
    h = h.split("%", 1)[0]          # IPv6 的 zone id（fe80::1%eth0）不算地址本体
    try:
        ip = ipaddress.ip_address(h)
    except ValueError:
        ip = None
    if ip is not None:
        mapped = ip.ipv4_mapped if ip.version == 6 else None
        return _ip_blocked(mapped if mapped is not None else ip)
    legacy = _legacy_ipv4(h)
    if legacy is not None:
        try:
            return _ip_blocked(ipaddress.ip_address(legacy))
        except ValueError:
            # 理论上到不了（_legacy_ipv4 只回 0 .. 2**32-1 的整数）。留着是给这条路钉个
            # 契约：认不出的东西一律当拒，绝不让 host_blocked 自己把异常冒给调用方。
            return True
    return False


def split_url(u):
    """解一个地址 → (scheme, host, path)；解不出来回 None。

    地址护栏自己不许抛：这种地址是要被「拒绝」的，不是要被「解释」的。`urlparse`
    碰到半个 IPv6 方括号（`http://[::1`、`https://[`）会当场抛
    `ValueError: Invalid IPv6 URL` —— 于是「拦下这个地址」变成「界面收到一句 Python
    的英文报错」，甚至把整次剪藏带崩。解不出来的地址在各处都有明确去处，所以这里
    统一收成一个 None，由调用方决定是拒绝、跳过还是当「不认识这一家」：
      · `url_block_reason` → 回一句人话「这个地址解不出来」；
      · `fetch` / `web_parse._guard` → 抛同口径的 ValueError；
      · `first_image` → 跳过这张图，看正文里的下一张；
      · `web_parse.platform_of` → 回「不认识这一家」，由通用剪藏去讲那句话。
    """
    try:
        p = urllib.parse.urlparse((u or "").strip())
        return p.scheme.lower(), (p.hostname or "").lower(), p.path
    except ValueError:
        return None


def url_block_reason(u):
    """这个地址为什么不能替用户去访问（能访问回空串）。

    「发出请求之前」那道闸与「跳转目标」那道闸共用这一处判据：以后加一条拒绝规则，
    只改这里。urlparse 自己也会对半个 IPv6 方括号之类抛 ValueError，一并收成拒绝。
    """
    parts = split_url(u)
    if parts is None:
        return "这个地址解不出来"
    scheme, host = parts[0], parts[1]
    if scheme not in ALLOWED_SCHEMES:
        return "只认 http/https 链接（现在这个是 %s）" % (scheme or "空")
    if not host:
        return "这个链接没有主机名"
    if host_blocked(host):
        return "这是本机或内网地址，归藏不替你访问"
    return ""


def url_allowed(u):
    """这个地址能不能替用户去访问。"""
    return not url_block_reason(u)


class RedirectGuard(urllib.request.HTTPRedirectHandler):
    """跳转目标也要过同一道闸：外站 302 到 192.168.x.x 一样是在替人敲内网的门。

    原先只有内置浏览器那一份自带这道检查（`web_browse._RedirectGuard`）。剪藏的 fetch、
    平台解析的 fetch_json、订阅的 _http、插图与封面下载都直接用
    `urllib.request.urlopen` —— 默认 opener 会自己跟着 `Location` 走，而且**不会再问
    一次**那个地址能不能去。实测拿一个放行的站点 302 到 `http://127.0.0.2/`：正文就是
    内网那一页，内网服务真被敲到了。

    所以把「每一跳都过闸」做成一份共用的 handler，由 `ensure_redirect_guard()` 装进
    默认 opener —— 谁走 urlopen 谁就过闸，不必每处各抄一份。
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        why = url_block_reason(newurl)
        if why:
            raise urllib.error.HTTPError(newurl, code, "跳转被拦下：%s" % why,
                                         headers, fp)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_GUARD_INSTALLED = False


def ensure_redirect_guard():
    """把默认 opener 换成带跳转闸门的那个（一个进程只装一次）。

    装在「默认」这一处而不是每次自己 build_opener：要管的不是某一次请求，而是本进程里
    所有替用户发出的请求 —— 几处取页路都用 `urllib.request.urlopen`，各写一份 opener
    就等于各留一道「将来可能忘了加」的门。
    """
    global _GUARD_INSTALLED
    if _GUARD_INSTALLED:
        return
    urllib.request.install_opener(urllib.request.build_opener(RedirectGuard))
    _GUARD_INSTALLED = True


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


# 这些字样出现在图片地址里，基本可以断定它是页头 logo、作者头像、二维码、
# 表情或追踪像素 —— og:image 缺席时最爱顶到第一张来，拿它们当封面比没封面更难看。
NON_COVER = re.compile(
    r"(avatar|icon|logo|qrcode|qr[-_.]?code|emoji|smiley|badge|watermark|track|"
    r"pixel|spacer|blank|loading|placeholder|signature|account|favicon|"
    r"suggest|ad_|ads\.)", re.I)
# 路径里带扩展名时只认这几种：真配图。svg/ico 一律不要（那是图标，不是文章图）。
COVER_EXT = (".jpg", ".jpeg", ".png", ".webp", ".gif", ".avif", ".bmp")


def first_image(markdown, base):
    """正文里的第一张配图 → 封面兜底。

    为什么在 markdown 里找而不在原始 DOM 里找：md 已经是「正文本身」—— 评论区、
    推荐卡片、页头页脚在抽取时就被丢掉了。DOM 里的第一张 img 往往是站点 logo
    或作者头像，而正文第一张图才是作者自己配的那张题图（用户要的就是这个）。
    扩展名认不出就放行：微信图床的地址长这样 mmbiz.qpic.cn/.../640?wx_fmt=jpeg，
    路径里没有 .jpg，硬要卡扩展名会把公众号题图全数丢掉。
    """
    for u in re.findall(r"!\[[^\]]*\]\(\s*([^)\s]+)", markdown or ""):
        low = u.strip().lower()
        if low.startswith("data:"):
            continue
        if NON_COVER.search(u):
            continue
        parts = split_url(u)
        if parts is None:
            # 半个 IPv6 方括号之类：这条地址本来就取不回图，跳过看下一张。
            # 旧写法直接 urlparse，会把「这一页有一张拼错的图」升级成整次剪藏失败。
            continue
        ext = os.path.splitext(parts[2])[1]
        if ext and ext not in COVER_EXT:
            continue
        return _abs(u, base)
    return ""


def safe_image_url(u):
    """这张图能不能替用户去下载：只认 http/https，本机与内网一律拒绝。

    封面地址是从别人写的页面里读出来的（og:image、正文首图、视频缩略图），
    不像用户粘进来的链接那样有人脑把关。不拦的话，一个恶意页面写个
    og:image="http://192.168.1.1/admin" 就能让归藏替用户去敲内网的门。
    判据与 fetch / 跳转那道闸是同一处（url_allowed）。
    """
    return url_allowed(u)


def pick_cover(og_url, markdown, base):
    """这一篇文章的封面用哪个地址：站点自己指定的题图优先，它不靠谱时退回顾正文首图。

    「不靠谱」有三种：og:image 指到头像 / logo / 二维码（有些站点把站长头像当默认图挂
    在每篇文章上，拿它当封面比没封面还难看）；指到本机或内网（这种地址本来就不该替用户
    去访问，留着它只会挡掉真正能用的那张图）；干脆没有。三种都交给 `first_image`。
    """
    og = _abs(og_url, base)
    if og and not NON_COVER.search(og) and safe_image_url(og):
        return og
    return first_image(markdown, base)


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


def _img_src(node, base):
    """这张 <img> 的真实地址，没有（只剩占位）就返回空串。

    微信的图是懒加载的：src 里往往只是 1px 占位（data:image/…），真地址在
    data-src 一类属性里 —— 所以要在候选里挑第一个「不是 data:」的，而不是
    第一个非空，不然一张真图会被占位符顶掉。
    """
    for key in ("data-src", "data-lazy-src", "data-original", "data-croporisrc", "src"):
        v = (node.attr(key) or "").strip()
        if v and not v.lower().startswith("data:"):
            return _abs(v, base)
    return ""


def _cell_text(cell, base):
    """格子里可能有段落、链接、图片：压成一行，链接与图片保留。

    遍历时把「别处会整块丢掉的标签」（脚本、样式、表单…）连同子树一起跳过，
    否则 <td>x<script>…</script></td> 的脚本文本会被当成正文抄进表格。
    碰到 <a> 只产出一次链接、不再往下走它的子节点 —— 老写法按整棵子树收文本，
    锚文字会先记成 Markdown 链接、再被当纯文本记第二遍。
    """
    parts = []

    def take(node):
        if isinstance(node, _Text):
            t = node.text.strip()
            if t:
                parts.append(t)
            return
        if node.tag in DROP_TAGS or (node.hint and DROP_INSIDE.search(node.hint)):
            return
        if node.tag == "a":
            t = re.sub(r"\s+", " ", node.inner_text()).strip()
            if t:
                href = _safe_href(node.attr("href"), base)
                parts.append("[%s](%s)" % (t, href) if href else t)
            return
        if node.tag == "img":
            src = _img_src(node, base)
            if src:
                alt = re.sub(r"\s+", " ", node.attr("alt") or "").strip()
                parts.append("![%s](%s)" % (alt, src))
            return
        for k in node.kids:
            take(k)

    for k in cell.kids:
        take(k)
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
        else:
            # 只有一行（或根本没有格子）的表格转不出管道表：与其整块丢掉，
            # 不如把格子里的文字当普通段落收进正文，别让内容凭空消失。
            txt = _cell_text(node, base)
            if txt:
                out.append("<p>%s</p>" % html.escape(txt, quote=False))
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
        src = _img_src(node, base)
        if not src:
            return                # 只剩占位图：留个坏链接不如不留
        attrs = {"src": src, "alt": node.attr("alt")}
    if tag in ("img", "hr"):
        out.append("<%s%s>" % (tag, _attr_str(attrs)))
        return
    out.append("<%s>" % tag)
    for k in node.kids:
        _emit(k, out, base, tables)
    out.append("</%s>" % tag)


def _restore_tables(md, tables):
    """占位符换回真表格。占位符单独成段，转换后正好占一行。

    一次性扫描替换，而不是 for 循环 + str.replace：格子里的文字也可能含
    「@@T1@@」这样的字样，逐个替换会让先塞进去的内容被后一轮再替换一遍，
    把两张表串在一起。
    """
    def sub(m):
        i = int(m.group(1))
        return tables[i] if 0 <= i < len(tables) else ""
    return re.sub(r"@@T(\d+)@@", sub, md or "")


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


def safe_charset(charset, fallback="utf-8"):
    """把外站声明的字符集收成一个 Python 真能用的编码名，认不出就退回 fallback。

    这个声明来自对方站点，可能是手滑写错的名字（实测有页面写 `charset=x-unknown`），
    也可能压根不是文本编码（`base64` / `hex`）。`bytes.decode` 对这两类名字抛的是
    **LookupError** —— `errors="replace"` 管的是「有字节解不出来」，管不了「编码名本身
    不存在」。所以不把这一步收口的话，取网页会以异常收场：剪藏冒出一句运行期错误，
    内置浏览器那一屏直接 500，而不是把页面显示出来、或铺一页人话。
    """
    name = str(charset or "").strip().strip("\"'").lower()
    for cand in (name, fallback):
        if cand and _is_text_codec(cand):
            return cand
    return "utf-8"


def _is_text_codec(name):
    """这个名字能不能交给 `bytes.decode` 用：真解一个字节，别只查名字。

    `codecs.lookup` 认得 `base64` 这类「字节↔字节」的编码，可它并不是文本编码，
    `bytes.decode` 照样拒收；空字节的探测又会因为短路而放过任何名字，所以这里用一个
    真的 ASCII 字节去试 —— 抛 UnicodeDecodeError 说明名字认得、只是这一字节不合它的
    规矩（例如 utf-16 要偶数长度），那它仍然是文本编码。
    """
    try:
        b"A".decode(name)
    except UnicodeDecodeError:
        return True
    except (LookupError, ValueError, TypeError):
        return False
    return True


def fetch(url):
    """取回页面原文，返回 (raw_text, final_url)。任何失败都抛带人话的 ValueError。"""
    u = (url or "").strip()
    if not u:
        raise ValueError("链接是空的")
    parts = split_url(u)
    if parts is None:
        # 地址护栏的第一步是先把地址解开；解不出来要回同一句人话，而不是把
        # `Invalid IPv6 URL` 这种运行期错误原样递给界面（见 split_url）。
        raise ValueError("这个地址解不出来")
    scheme, host = parts[0], parts[1]
    if scheme not in ALLOWED_SCHEMES:
        raise ValueError("只认 http/https 链接（现在这个是 %s）" % (scheme or "空"))
    if not host:
        raise ValueError("这个链接没有主机名，打不开")
    if host_blocked(host):
        raise ValueError("这是本机或内网地址，归藏不替你访问")
    # 上面挡的是「粘进来的这个地址」，挡不住站点把我们 302 到内网去 —— 那一跳由默认
    # opener 上的 RedirectGuard 逐一核对（见 ensure_redirect_guard）。
    ensure_redirect_guard()
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
    raw = blob.decode(safe_charset(charset), errors="replace")
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
        # 封面：og:image 优先（那是作者自己在站点里指定的题图），不靠谱时退回顾正文首图
        "cover": pick_cover(_meta(tree, "og:image", "twitter:image"), md, final),
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


def _write_json_atomic(path, obj):
    """剪藏那份 `meta.json` 的落盘口：临时件一次一个名字，写完一次性原子顶替。

    这一份 meta 是「页面提取结果 + 剪藏专属字段」，落盘也是 `ensure_ascii=False`：
    地址 / 站名 / 标题里落单的半个代理码点照样写不进去。正文那一半已经在
    `import_book` 里过过刀了，这里补上同一把尺，免得两个落盘口各防一半。

    临时名带 pid + 一小串随机，与 `book_import._tmp_name` / `flomo_notes._write_text_atomic`
    / `writer._write_text_atomic` 同一把尺子：这份 meta 的两个写侧（`import_book`
    先写一份、这里读回来补剪藏字段再写一遍）今天在同一线程里一前一后，固定名还
    看不出问题；名字一次一个之后，两条写哪怕真撞上也不会互相搬走临时件。现成的
    毛病在失败路径：原先是裸 `open(tmp, "w")` + `os.replace`，写一半断掉就在书目录
    里留一个 `meta.json.tmp`，没有一句清理。
    """
    tmp = "%s.%d.%s.tmp" % (path, os.getpid(), uuid.uuid4().hex[:8])
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(book_layout.drop_lone_surrogates(obj), f,
                      ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except BaseException:
        # 中止信号（SystemExit / KeyboardInterrupt）不继承 Exception，只兜
        # Exception 会在退出路径上留下临时件；收不动也不算错。
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def save_clip(out_dir, url, title="", author="", cover_dir=None, extract_fn=None):
    """剪一篇 → 书库里的一本「文章」。返回 book_import 那套 info（含 id、章数、字数）。

    extract_fn 让调用方换一套提取器：知乎 / 小红书 / X 那几条路在 web_parse 里，
    它们返回的形状和 extract 一模一样，所以这里除了「谁来抓」之外一行都不用分岔。
    不传就是默认的通用网页提取。
    """
    art = (extract_fn or extract)(url)
    # 取回来的标题 / 正文 / 出处都是外站文本，可能夹着落单的半个代理码点：X、知乎、
    # 小红书那几条路拿回来的是 JSON，`"\ud83d"` 这种转义照样能被 `json.loads` 读成
    # 一个落单的码点。下面 `body.encode("utf-8")` 碰到它当场抛 UnicodeEncodeError，
    # 而 clip_many 会把 `str(e)[:160]` 原样递到界面 —— 用户看到的是
    # 「剪不动：'utf-8' codec can't encode character '\ud83d' …」这句 Python 原文，
    # 一篇本来剪得下来的文章被一个坏码挡在门外（正文 / 标题里各一处，都实测过）。
    # 修法不在这一层另写一把尺子：`book_layout.drop_lone_surrogates` 就是「外部文本
    # 要落进 utf-8」那把共用尺（import_book 与下面那份 meta 落盘都走它），编码与拼
    # 文件名之前先过一遍，成对的表情一个不动。
    art = book_layout.drop_lone_surrogates(art)
    body = "# %s\n\n%s\n%s\n" % (art["title"], _front_matter(art), art["markdown"])
    name = re.sub(r'[\\/:*?"<>|]', "", art["title"])[:60] or "clip"

    info = book_import.import_book(
        out_dir, "%s.md" % name, body.encode("utf-8"),
        title=(title or "").strip() or art["title"],
        author=(author or "").strip() or art["author"] or art["site"],
        cover_dir=cover_dir, book_id_prefix="clip", source="clip")

    # meta 里补剪藏专属字段：来源链接要能一键回原文，source=clip 让书架分组显示。
    # 读的是 `book_import` 刚写下的那一份，走 `book_layout.read_meta` 这唯一读口：
    # 它认 BOM（记事本「另存为 UTF-8」多塞的三字节），也不再在这里抄第三份读法。
    # 原先裸 utf-8 读、外面罩 `except Exception: meta = {}` —— 带 BOM 时整份解不动被
    # 当空账，紧接着 `.update()` 再写回，等于**以空账为底重写**，用户改过的其它字段
    # 整份丢掉。坏档 / 缺档仍旧回空 dict，下面的 `.update()` 与落盘一字不变。
    mp = os.path.join(info["dir"], "meta.json")
    meta = book_layout.read_meta(info["dir"])
    meta.update({"source": "clip", "format": "clip", "url": art["url"],
                 "site": art["site"], "date": art["date"], "cover": art["cover"],
                 "words": art["words"],
                 # 字数按「正文字数」记，不按文件长度：Markdown 语法符号、图片地址和
                 # 出处那几行都不该算进「这篇有多少字」，否则预览说 5136 字、书架上
                 # 变 8153 字，用户只会以为程序在瞎报数。
                 "chars": art["words"],
                 "clipped_at": time.strftime("%Y-%m-%d %H:%M:%S")})
    # 落盘走这一族共用的原子写口：临时件一次一个名字、失败先收残件再抛。
    _write_json_atomic(mp, meta)
    info.update({"url": art["url"], "site": art["site"], "date": art["date"],
                 "words": art["words"], "chars": art["words"],
                 "cover": art["cover"], "source": "clip"})
    return info


def clip_many(out_dir, items, cover_dir=None, extract_fn=None):
    """批量剪藏：items = [{url, title?, author?}]（也收裸链接），单条失败不影响其余。"""
    ok, fail = [], []
    for it in (items or []):
        url = (it.get("url") if isinstance(it, dict) else it) or ""
        try:
            ok.append(save_clip(
                out_dir, url,
                title=(it.get("title") or "") if isinstance(it, dict) else "",
                author=(it.get("author") or "") if isinstance(it, dict) else "",
                cover_dir=cover_dir, extract_fn=extract_fn))
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
