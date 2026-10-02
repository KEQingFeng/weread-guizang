# -*- coding: utf-8 -*-
"""知乎 / 小红书 / X 这三个站的正文抽取：不用登录，能取多少取多少。

为什么单独一个文件：剪藏（clip_article.py）走的是「取 HTML → 挑正文容器 → 转
Markdown」这条通用路，对博客、公众号这类把正文写在 HTML 里的站点够用。但这三家
不是：X 的正文根本不在页面里（页面是个 JS 壳，得问 syndication 那个公开接口）、
小红书把正文塞进 <script> 里的一坨 window.__INITIAL_STATE__、知乎对未登录读者
时而给正文、时而给验证页。这些差异属于「同一个站一种取法」，应该各自成块，
而不是往通用解析器里塞三家特例——那样改坏的是所有站点的剪藏。

对外只给两个函数：extract() 取一篇（拿不到就说人话），extract_or_none() 是它的
不抛版本，给「试着解析，失败就走老路」的调用方用。返回的字典跟
clip_article._extract_once 完全同形，上层（预览、入库、书架）不用区分来源。

三家的取法都依赖对方「现在」怎么发页面，随时可能失效。哪一条失效了，就在
_zhihu / _xhs / _x 里那一段的注释里写清楚现状，别留下一个看起来正常、其实永远
抛错的空壳——用户看到的是「归藏解析不了这个链接」，而不是「这里本来能解析」。
"""

import datetime
import html
import json
import math
import re
import time
import urllib.parse
import urllib.request

import book_import
import clip_article

# 复用剪藏那套：同一份浏览器 UA、同一个超时、同一个体积上限。
# 不为这个文件再造一套，否则两家站点策略一改就得改两处。
UA = clip_article.UA
TIMEOUT = clip_article.TIMEOUT
MAX_BYTES = clip_article.MAX_BYTES

# 正文最少要这么多字才算「读出来了」。比 clip_article.MIN_WORDS（60）低：
# 推文和「想法」本来就短，拿 60 去卡会把正常内容判成空页。
MIN_WORDS = 12

_X_HOSTS = ("x.com", "twitter.com", "mobile.twitter.com", "www.twitter.com", "m.twitter.com")
_XHS_HOSTS = ("xiaohongshu.com", "xhslink.com")

# 知乎的登录墙/反爬页。分开写是因为知乎给未登录读者的拦截页有好几种：
# 「安全验证」是风控、「意见反馈」是它把请求打成异常流量后的提示页、
# unhuman 是它前端拦截脚本的标记。
_ZHIHU_WALL = re.compile(
    r"(安全验证|意见反馈|系统监测到|异常流量|验证码|unhuman|"
    r"扫码登录|请登录后查看|登录知乎|账号存在异常)")
_XHS_WALL = re.compile(r"(扫码查看|登录后查看|请先登录|扫码登录|打开小红书App|"
                       r"你访问的页面不见了|笔记不存在|当前笔记暂时无法浏览)")


def platform_of(url):
    """这个链接属于哪个站：zhihu / xhs / x / 空串（不认识）。

    只认主机名，不看路径——路径的解析各站自己再做一次，这里判错会连带
    「要按哪家的护栏取」一起错。
    """
    host = (urllib.parse.urlparse((url or "").strip()).hostname or "").lower()
    if not host:
        return ""
    if host in ("zhihu.com", "www.zhihu.com", "zhuanlan.zhihu.com") or host.endswith(".zhihu.com"):
        return "zhihu"
    if host in _XHS_HOSTS or host.endswith(".xiaohongshu.com"):
        return "xhs"
    if host in _X_HOSTS:
        return "x"
    return ""


def _guard(url):
    """沿用剪藏那套地址护栏，并在真正发请求之前跑。

    剪藏的 fetch() 自己也会挡一次，但它只覆盖走 HTML 的那两条路；X 是直接问
    cdn.syndication.twimg.com 的，绕开了 fetch，护栏就得在这儿统一补上。
    """
    u = (url or "").strip()
    if not u:
        raise ValueError("链接是空的")
    p = urllib.parse.urlparse(u)
    if p.scheme.lower() not in clip_article.ALLOWED_SCHEMES:
        raise ValueError("只认 http/https 链接（现在这个是 %s）" % (p.scheme or "空"))
    host = (p.hostname or "").lower()
    if not host:
        raise ValueError("这个链接没有主机名，打不开")
    if clip_article.BLOCKED_HOSTS.match(host):
        raise ValueError("这是本机或内网地址，归藏不替你访问")


# 取 HTML 的那一步。做成模块级名字是为了让离线套件能把它换成夹具服务：
# 把请求地址换到 127.0.0.1 的临时 http.server 上，就不用真去敲知乎和小红书。
fetch = clip_article.fetch


def fetch_json(url):
    """取一个 JSON（或它的 HTML 错误页）回来，返回 (文本, 最终地址)。

    不走 clip_article.fetch 是因为它带一条「里面必须有 <，否则不像网页」的检查——
    syndication 接口正常时回的正是没有尖括号的纯 JSON，会被它一票否决。
    """
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept": "application/json,text/plain,*/*",
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
        raise ValueError("取回来的东西太大了（超过 6MB），不像一条正文")
    return blob.decode(charset or "utf-8", errors="replace"), final


def _fetch_page(url, wall_msg):
    """取 HTML 页面；401/403 一律翻成「要登录」那句话。

    知乎和小红书对未登录读者常常直接回 403，连页面正文都传不过来——这时候
    clip_article.fetch 抛的是「打不开这个页面：HTTP Error 403: Forbidden」，
    对用户等于没说。401/403 在这两家只有一个含义，替它们讲明白（实测 2025 年
    知乎 /hot 未登录就是 403）。其余错误原样抛出，不硬套。
    """
    try:
        return fetch(url)
    except ValueError as e:
        if re.search(r"(401|403|Forbidden|Unauthorized)", str(e), re.I):
            raise ValueError(wall_msg)
        raise


_B36 = "0123456789abcdefghijklmnopqrstuvwxyz"


def _base36(value):
    """双精度浮点 → 36 进制字符串，口径对齐 JS 的 Number.prototype.toString(36)。

    为什么要逐位手写：react-tweet 的 token 就是 (id/1e15*π).toString(36) 去掉 0 和
    小数点，而 V8 这个转换既不是「截断到某个精度」也不是「最短可回读表示」——
    它是「按半个 ULP 决定写到第几位，然后对最后一位四舍五入并处理进位」。
    Python 的 format/round 都不是这个口径，差一位 Twitter 就回错误页，
    所以照 V8（src/numbers/conversions.cc 的 DoubleToRadixStringView）的循环抄一遍。
    """
    neg = value < 0
    val = abs(value)
    integer = math.floor(val)
    fraction = val - integer
    delta = 0.5 * (math.nextafter(val, math.inf) - val)
    if delta <= 0:
        delta = 5e-324                    # 次正规数：半个 ULP 会下溢成 0，用最小的正数顶上
    digits = []
    dropped = False
    if fraction >= delta:
        digits.append(".")
        while True:
            fraction *= 36
            delta *= 36
            digit = int(fraction)
            digits.append(_B36[digit])
            fraction -= digit
            # 四舍五入：正好卡在 .5 时往偶数靠，跟 V8 一样
            if fraction > 0.5 or (fraction == 0.5 and (digit & 1)):
                if fraction + delta > 1:
                    # 进位：从最后写下的那位往回退，直到有位能加一
                    while True:
                        if digits[-1] == ".":
                            integer += 1
                            dropped = True
                            break
                        prev = _B36.index(digits[-1])
                        if prev + 1 < 36:
                            digits[-1] = _B36[prev + 1]
                            break
                        digits.pop()
                    break
            if fraction < delta:
                break
    if dropped:
        digits = []
    head = int(integer)
    if head == 0:
        out = "0"
    else:
        buf = []
        while head > 0:
            buf.append(_B36[head % 36])
            head //= 36
        out = "".join(reversed(buf))
    out += "".join(digits)
    return ("-" + out) if neg else out


def _tweet_token(tweet_id):
    """react-tweet 那套 token：id / 1e15 × π，转 36 进制，去掉 0 与小数点。"""
    return _base36((int(tweet_id) / 1e15) * math.pi).replace("0", "").replace(".", "")


def _fmt_epoch(ts):
    """秒/毫秒时间戳 → 本地时间文本。两家的字段单位不一样，靠量级自己判。"""
    try:
        n = float(ts)
    except (TypeError, ValueError):
        return ""
    if n > 1e11:                       # 十一位以上是毫秒（小红书给的就是毫秒）
        n /= 1000.0
    try:
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(n))
    except (ValueError, OSError, OverflowError):
        return ""


def _fmt_iso(text):
    """ISO8601（X 的 created_at）→ 本地时间文本。解不动就原样留着，别丢信息。"""
    t = (text or "").strip()
    if not t:
        return ""
    try:
        d = datetime.datetime.fromisoformat(t.replace("Z", "+00:00"))
        if d.tzinfo is not None:
            d = d.astimezone()
        return d.strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return t[:19]


def _one_line(text, limit=0):
    s = re.sub(r"\s+", " ", text or "").strip()
    return s[:limit] if limit else s


def _images_md(urls):
    """图片一律用 Markdown 引用接在正文后面：阅读器认得这个写法，入库也走同一条路。"""
    return "".join("\n\n![](%s)" % u for u in urls if u)


def _result(title, author, site, date, cover, url, markdown, words):
    """统一出口：键必须跟 clip_article._extract_once 一模一样，上层才能一视同仁。"""
    return {
        "title": (title or "")[:120],
        "author": (author or "")[:60],
        "site": (site or "")[:60],
        "date": date or "",
        "cover": cover or "",
        "url": url,
        "markdown": markdown,
        "words": words,
    }


# ─────────────────────────────── X / Twitter ───────────────────────────────

_X_STATUS = re.compile(r"/status(?:es)?/(\d+)")
# i/web/status/<id> 也带 /status/，上面那条就能捞到；这里只兜住没有 /status 的短链形态
_X_BARE = re.compile(r"/(\d{10,25})(?:[/?#]|$)")


def _tweet_id(url):
    path = urllib.parse.urlparse(url).path
    m = _X_STATUS.search(path)
    if m:
        return m.group(1)
    m = _X_BARE.search(path)
    return m.group(1) if m else ""


def _x(url):
    tid = _tweet_id(url)
    if not tid:
        raise ValueError("这个 X 链接里没有推文编号，归藏认不出是哪条")

    # 公开的 syndication 接口，不需要登录也不需要 key，但 token 必须按公式自己算。
    # 这是 2025 年仍在用的取法；哪天它改了，这里拿回的就是一个 HTML 错误页（被删、
    # 设了保护、账号被封都是这个形态），下面按「打不开」报实话，不当成解析成功。
    endpoint = ("https://cdn.syndication.twimg.com/tweet-result?id=%s&token=%s&lang=en"
                % (urllib.parse.quote(tid), _tweet_token(tid)))
    raw, final = fetch_json(endpoint)
    body = raw.lstrip()
    if not body or body.startswith("<"):
        raise ValueError("这条推现在打不开（删了、或设了保护），归藏取不到")
    try:
        data = json.loads(raw)
    except ValueError:
        raise ValueError("这条推现在打不开（删了、或设了保护），归藏取不到")

    user = data.get("user") if isinstance(data.get("user"), dict) else {}
    text = (data.get("text") or "").strip()
    photos = []
    for p in (data.get("photos") or []):
        url_p = (p.get("url") if isinstance(p, dict) else "") or ""
        if url_p:
            photos.append(url_p)

    # 没有作者、也没有文字和配图，等于接口给回一个空壳：被删、设了保护、或纯视频
    if not user or (not text and not photos):
        if not text and not photos and data.get("video"):
            raise ValueError("这条推只有视频，归藏读不出文字")
        raise ValueError("这条推现在打不开（删了、或设了保护），归藏取不到")

    md = clip_article._clean(text + _images_md(photos))
    words = clip_article._word_count(md)

    screen = (user.get("screen_name") or "").strip()
    display = _one_line(user.get("name") or "")
    handle = ("@" + screen) if screen else ""
    title = _one_line(text, 40) or ("%s 的推文" % handle if handle else "X 上的推文")
    author = display or handle
    site = "X（推特）"
    cover = photos[0] if photos else ""
    return _result(title, author, site, _fmt_iso(data.get("created_at")),
                   cover, url, md, words)


# ─────────────────────────────── 小红书 ───────────────────────────────

_XHS_NOTE = re.compile(r"/(?:explore|discovery/item|item)/([0-9a-zA-Z]+)")


def _xhs_note_id(url):
    m = _XHS_NOTE.search(urllib.parse.urlparse(url).path)
    return m.group(1) if m else ""


def _json_blob(text, marker):
    """从页面里抠出 marker 后面那一段 JSON，按花括号配对收尾（字符串里的括号不算）。

    不能用「匹配到 </script>」这种偷懒写法：__INITIAL_STATE__ 里的正文本身就可能
    带尖括号，而且这坨 JSON 有嵌套对象，正则数不清层。
    """
    at = text.find(marker)
    if at < 0:
        return ""
    start = text.find("{", at)
    if start < 0:
        return ""
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    return ""


# __INITIAL_STATE__ 不是合法 JSON：它是 JS 字面量，值里会出现裸的 undefined。
# 只在「该出现值」的位置替换（前面是 : , [ ），别把正文里真的是 undefined 这个词的
# 字符串一起改掉。
_UNDEFINED = re.compile(r"(?<=[:,\[])\s*undefined\s*(?=[,}\]])")


def _xhs_pick_note(state, note_id):
    """从 state 里取那条笔记。老页面放 noteDetailMap，新页面结构可能挪窝，
    所以找不到指定 id 时退而取第一个值——总比直接判死刑好。"""
    holder = state.get("note") if isinstance(state, dict) else None
    if not isinstance(holder, dict):
        return {}
    mapping = holder.get("noteDetailMap")
    if not isinstance(mapping, dict) or not mapping:
        return {}
    entry = mapping.get(note_id)
    if not isinstance(entry, dict):
        entry = next((v for v in mapping.values() if isinstance(v, dict)), None)
    if not isinstance(entry, dict):
        return {}
    note = entry.get("note")
    if isinstance(note, dict):
        return note
    # 少数版本直接把 note 的字段摊在 entry 里
    return entry if entry.get("desc") or entry.get("title") else {}


def _xhs(url):
    raw, final = _fetch_page(url, "小红书这一页要登录（扫码）才给正文，归藏拿不到")
    note_id = _xhs_note_id(url) or _xhs_note_id(final)
    blob = _json_blob(raw, "window.__INITIAL_STATE__")
    state = {}
    if blob:
        try:
            state = json.loads(_UNDEFINED.sub(" null ", blob))
        except ValueError:
            state = {}
    note = _xhs_pick_note(state, note_id)

    if not note:
        # 登录墙的小红书是被拦在 __INITIAL_STATE__ 之前的那一步，
        # 所以「抠不到 note」既可能是要登录，也可能是笔记删了，按页面痕迹分着说
        if not blob or _XHS_WALL.search(raw) or "__INITIAL_STATE__" not in raw:
            raise ValueError("小红书这一页要登录（扫码）才给正文，归藏拿不到")
        raise ValueError("这一页里没读出笔记正文（可能要登录，或笔记已经删了）")

    title = _one_line(note.get("title") or "")
    desc = (note.get("desc") or "").strip()
    user = note.get("user") if isinstance(note.get("user"), dict) else {}
    author = _one_line((user or {}).get("nickname") or "")

    images = []
    for img in (note.get("imageList") or []):
        if not isinstance(img, dict):
            continue
        u = (img.get("urlDefault") or img.get("url") or "").strip()
        if u:
            images.append(u)

    if not desc and not images:
        raise ValueError("这一页里没读出笔记正文（可能要登录，或笔记已经删了）")

    md = clip_article._clean(desc + _images_md(images))
    words = clip_article._word_count(md)
    if not title:
        title = _one_line(desc, 40) or (("%s 的笔记" % author) if author else "小红书笔记")
    return _result(title, author, "小红书", _fmt_epoch(note.get("time")),
                   images[0] if images else "", final or url, md, words)


# ─────────────────────────────── 知乎 ───────────────────────────────

# 正文容器，从最外层往里排：先捞外层，免得只取到内层的半截
_ZHIHU_BODY = ("RichContent-inner", "QuestionAnswer-content", "RichText", "Post-RichText")


def _find_by_class(tree, name):
    for node in tree.root.walk():
        if node.tag != "#doc" and name in (node.attr("class") or ""):
            return node
    return None


def _zhihu(url):
    raw, final = _fetch_page(url, "知乎这一页现在要登录或过安全验证才给正文，归藏拿不到")
    tree = clip_article.parse_html(raw)

    body = None
    for name in _ZHIHU_BODY:
        node = _find_by_class(tree, name)
        if node is not None and len(node.inner_text()) >= 20:
            body = node
            break

    if body is None:
        # 先试正文、再判墙：知乎正常页面的页脚里也挂着「意见反馈」，
        # 一上来就按关键词判会把好页面当拦截页。反过来只有正文真的没捞到时，
        # 这些标记才说明它是被拦了（而不是这一页内容结构变了）。
        if _ZHIHU_WALL.search(raw) or clip_article.BLOCKED_PAGE.search(raw):
            raise ValueError("知乎这一页现在要登录或过安全验证才给正文，归藏拿不到")
        raise ValueError("这一页里没读出正文（可能要登录，或这一页本来就是回答列表）")

    tables = []
    fragment = clip_article._serialized(body, final, tables)
    md = clip_article._clean(
        clip_article._restore_tables(book_import._md_from_xhtml(fragment), tables))
    words = clip_article._word_count(md)
    if words < MIN_WORDS:
        if _ZHIHU_WALL.search(raw):
            raise ValueError("知乎这一页现在要登录或过安全验证才给正文，归藏拿不到")
        raise ValueError("这一页里没读出正文（可能要登录，或这一页本来就是回答列表）")

    title = clip_article._meta(tree, "og:title", "twitter:title")
    if not title:
        title = clip_article._first_text(tree.root,
                                         lambda n: n.tag == "h1" and len(n.inner_text()) < 200)
    if not title:
        title = _one_line(tree.title_text(), 80)

    author = clip_article._meta(tree, "name")
    if not author:
        author = clip_article._first_text(
            tree.root, lambda n: "AuthorInfo-name" in (n.attr("class") or ""))
    if not author:
        author = clip_article._meta(tree, "author")
    # 知乎的署名常带「等 xxx 人赞同了该回答」这类尾巴，截到第一个空格前更像人名
    author = _one_line(author, 40)

    date = clip_article._meta(tree, "datepublished", "datemodified",
                              "article:published_time", "og:time")
    if not date:
        date = clip_article._publish_time(tree, raw)

    cover = clip_article._abs(clip_article._meta(tree, "og:image", "twitter:image"), final)
    if not title:
        title = "%s %s" % (author or "知乎", time.strftime("%m-%d"))
    return _result(title, author, "知乎", date, cover, final or url, md, words)


_EXTRACTORS = {"x": _x, "xhs": _xhs, "zhihu": _zhihu}


def extract(url):
    """链接 → 与 clip_article._extract_once 同形的字典。

    拿不到就说清楚为什么（要登录、是视频、还是没读出正文）——「解析失败」这四个字
    对用户没有任何用，他需要知道的是改用什么办法看这篇。
    """
    _guard(url)
    kind = platform_of(url)
    if not kind:
        raise ValueError("这个站归藏还不会解析（目前只认知乎、小红书、X）")
    return _EXTRACTORS[kind](url)


def extract_or_none(url):
    """extract() 的不抛版本。给「先试试这家、不行再走通用剪藏」的调用方用。"""
    try:
        return extract(url)
    except Exception:
        return None


if __name__ == "__main__":      # 手动冒烟：python3 web_parse.py <链接>
    import sys
    if len(sys.argv) < 2:
        print("用法: python3 web_parse.py <url>")
        sys.exit(1)
    link = sys.argv[1]
    print("platform_of:", platform_of(link) or "(不认识)")
    try:
        art = extract(link)
    except ValueError as err:
        print("取不到：%s" % err)
        sys.exit(2)
    print(json.dumps({k: v for k, v in art.items() if k != "markdown"},
                     ensure_ascii=False, indent=2))
    print("---- 正文前 1200 字 ----")
    print(art["markdown"][:1200])
