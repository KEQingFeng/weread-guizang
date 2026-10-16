#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""内置浏览器：把外部网页取回来，改一改再由本机送进界面里那个 iframe。

为什么要有这一层：归藏的壳本来就是网页内核（Windows 是 Edge WebView2 / Chromium，
macOS 是 WKWebView），可外层直接给 iframe 一个外站地址照样是一片空白 —— 对方响应头
写着 `X-Frame-Options: SAMEORIGIN` 或 CSP 的 `frame-ancestors`，浏览器只认「谁在嵌」，
不认这是不是本机界面。由本机把页面取回来、再从同一处送出去，这些头就管不着了。

这一层只干三件事，越少越好：

  1. 取回来。本机与内网地址一个不放，用的是剪藏那一把尺子（`clip_article.host_blocked`）
     ——「替用户去敲内网的门」这条风险不分功能，剪藏不该犯，内置浏览器同样不该犯。
  2. 改一改。补一条 `<base>`，让相对路径的图片、样式、脚本还有一个根可循；页面末尾接
     一小段脚本，把「点链接 / 提交表单」翻译成给父页面的一条消息，父页面收到再让这一层
     取下一页。拦的是 click 冒泡，所以站点用脚本生成的链接也逃不掉。
     遇到 YouTube 这类「HTML 里根本没有正文、内容全在一坨 JSON 里由站点自己的脚本现渲染」
     的站，改的那一下换成读那坨 JSON、铺一页我们自己的静态目录（`yt_static`）——因为壳里
     那些脚本在 iframe 的不透明源上跑不动，原样重送只会得一片空白。它未登录时的首页 /
     feed 更绝：连那坨 JSON 里都不放内容（2026-10-16 实测：950KB 的首页里 `videoId`
     出现 0 次，只有一句「搜索视频，开启 YouTube 之旅」）。所以认不出卡片时再换一处
     有货的落脚点（`YT_LANDINGS`），别把一页空的端给用户。
  3. 送回去。HTML 原样转交（站点自己的脚本照跑），图片 / PDF 这类非网页直接把字节透传，
     让内核自己渲染。

数据落在哪：这一层不写盘、不留 cookie、不建浏览器档案 —— 它就是「取回来给你看一眼」，
所以没有任何用户数据落进仓库或数据目录。真要登录某个站的，走「安娜的档案」那种自己
开窗口的路子，会话留在浏览器档案里，归藏一行不读。

失败也给人话：取不回来的原因（地址不合法 / 内网被拦 / 超时 / 太大）原样带回界面，
由 `error_page()` 铺成一页，而不是让内核甩一张它自己的错误页。
"""

import html as _html
import json
import math
import re
import urllib.error
import urllib.parse
import urllib.request

import clip_article

# 与剪藏共用同一套判据：scheme 只认 http/https，本机与内网地址一律拒绝。
ALLOWED_SCHEMES = clip_article.ALLOWED_SCHEMES
UA = clip_article.UA

# 内置浏览器不是下载器：超过这个量级的多半是拿链接当文件下，别把界面顶死。
MAX_BYTES = 4 * 1024 * 1024
TIMEOUT = 20

# 「AI 功能导航」那一栏的落脚点。放在这里当默认首页，界面与测试都读这一处。
HOME = "https://ai-bot.cn/"

_CSP_META = re.compile(
    r'<meta[^>]+http-equiv\s*=\s*["\']?content-security-policy["\']?[^>]*>', re.I)
_HEAD_OPEN = re.compile(r"<head[^>]*>", re.I)
_BODY_CLOSE = re.compile(r"</body\s*>", re.I)

# 页面末尾接的这段脚本：把点链接 / 提交表单变成给父页面的一条消息。
# 用 parent.postMessage('*') 是必须的 —— iframe 带着 sandbox（不给 allow-same-origin），
# 在浏览器看来它是个不透明源，写死父页面地址反而会被挡。
_LINK_SCRIPT = """
<script>
/* 归藏内置浏览器补的一段。这个页面由本机这一层送出，点链接不能让 iframe 自己跨出去
   （跨出去就绕过了这一层、也回不到这一层），所以把点击翻成一条给父页面的消息。 */
(function(){
  function abs(u){
    try { return new URL(u, document.baseURI).href; } catch(e){ return u; }
  }
  function tell(u){ try { parent.postMessage({gzBrowse: abs(u)}, '*'); } catch(e){} }
  document.addEventListener('click', function(e){
    var n = e.target;
    while (n && n.tagName !== 'A') n = n.parentElement;
    if (!n) return;
    var h = n.getAttribute('href') || '';
    if (!h || h.charAt(0) === '#' || /^javascript:/i.test(h)) return;
    e.preventDefault();
    // 带 data-gz-use 的那颗不是「再取一页」，而是「把这条链接交给界面办别的事」——
    // 内置浏览器里认出的视频就是这么回到「视频转笔记」那一屏的。
    if (n.hasAttribute('data-gz-use')) {
      try { parent.postMessage({gzUseLink: abs(h)}, '*'); } catch(err){}
      return;
    }
    tell(h);
  }, true);
  document.addEventListener('submit', function(e){
    var f = e.target;
    if (!f || !f.getAttribute) return;
    e.preventDefault();
    var act = f.getAttribute('action') || location.href;
    var m = (f.getAttribute('method') || 'get').toLowerCase();
    if (m !== 'get') { tell(act); return; }
    var q = new URLSearchParams(new FormData(f)).toString();
    tell(act + (act.indexOf('?') >= 0 ? '&' : '?') + q);
  }, true);
})();
</script>
"""


class Blocked(ValueError):
    """地址不合法 / 被内网拦截 —— 与一般的网络失败分开，界面好说人话。"""


_SCHEME = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.\-]*:")


def normalize(raw):
    """把用户敲进来的地址收成一条能发的 URL；不合法就抛 Blocked（带人话）。"""
    u = (raw or "").strip()
    if not u:
        raise Blocked("地址是空的")
    # 先认 scheme 再决定要不要补 https：不能只看有没有 "://"，否则 `javascript:alert(1)`
    # 会被补成 `https://javascript:alert(1)`，把一段脚本当成主机名放行。
    if not _SCHEME.match(u):
        u = "https://" + u
    # urlparse 自己会对半个 IPv6 方括号（`http://[::1`、`https://[`）当场抛 ValueError。
    # 它不是 Blocked，`browse()` 只接 Blocked —— 异常会一路穿出 /api/browse，客户端拿到
    # 的是一条空响应（连接被掐），而不是 error_page 铺的那页人话。地址解不出来就是拒绝，
    # 与剪藏 clip_article.url_block_reason 同一口径：不往外抛。
    try:
        p = urllib.parse.urlparse(u)
        host = (p.hostname or "").lower()
    except ValueError:
        raise Blocked("这个地址解不出来")
    if p.scheme.lower() not in ALLOWED_SCHEMES:
        raise Blocked("只认 http/https 链接（现在这个是 %s）" % (p.scheme or "空"))
    if not host:
        raise Blocked("这个链接没有主机名，打不开")
    if clip_article.host_blocked(host):
        raise Blocked("这是本机或内网地址，归藏不替你访问")
    return urllib.parse.urlunparse(p)


class _RedirectGuard(urllib.request.HTTPRedirectHandler):
    """跳转也要过同一道闸：外站 302 到 192.168.x.x 一样是在替人敲内网的门。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        try:
            newurl = normalize(newurl)
        except Blocked as e:
            raise urllib.error.HTTPError(newurl, code, str(e), headers, fp)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def prepare(html, base):
    """给取回来的 HTML 补 <base> 与点链接脚本，并摘掉会误伤那段脚本的 CSP meta。

    这一页已经是「本机重新送」的一份，原站 CSP 管的是原站，搬到这里只会拦住我们补的
    脚本，所以整条 meta 摘掉；不改动站点自己的样式与外链。
    """
    html = _CSP_META.sub("", html)
    base_tag = '<base href="%s">' % _html.escape(base or "", quote=True)
    m = _HEAD_OPEN.search(html)
    if m:
        html = html[:m.end()] + base_tag + html[m.end():]
    else:
        html = base_tag + html
    m = _BODY_CLOSE.search(html)
    if m:
        html = html[:m.start()] + _LINK_SCRIPT + html[m.start():]
    else:
        html += _LINK_SCRIPT
    return html


def _looks_html(blob, ctype):
    if ctype == "text/html" or ctype == "application/xhtml+xml":
        return True
    head = blob[:2048].lower()
    return b"<html" in head or b"<!doctype" in head


# ── YouTube：这一站不给「一页能看的 HTML」──────────────────────
# 用户报的是「油管打开无法显示」。取回这一步其实成了（首页 950KB、八秒多就回来），
# 黄掉的是后面两段：
#   ① youtube.com 是一台纯客户端应用 —— 返回的 HTML 里只有 <ytd-app> 那副空壳，
#      正文全在 `var ytInitialData = {…}` 这一坨 JSON 里，由站点自己的脚本再渲染。
#   ② 我们的 iframe 带 `sandbox` 且**故意不给** allow-same-origin（不给它读归藏的
#      localStorage），在浏览器看来这就是个不透明源：脚本一碰 localStorage 就抛
#      SecurityError，油管那段引导脚本当场断在这里 —— 于是壳渲染不出来，一片空白。
# 与其为了油管放宽 sandbox（那是拿用户的界面换一页视频），不如把这条 JSON 自己读了：
# 卡片、频道、时长、缩略图都在里面，用我们自己的话铺成一页静态目录，链接照旧走
# /api/browse。看列表、挑一支、把链接送回「视频转笔记」这三件事全都在，缺的只是
# 播放器本身 —— 播放器是流式媒体，这一层从来也不代理（见 docs/交接说明.md 的风险段）。
YT_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com",
            "music.youtube.com", "youtu.be"}
# 缩略图只认这两台站自己的图床，别的域名一律不往这一页里引。
_YT_IMG_HOSTS = ("i.ytimg.com", "s.ytimg.com", "yt3.googleusercontent.com",
                 "yt3.ggpht.com")
_YT_ID = re.compile(r"^[A-Za-z0-9_\-]{6,20}$")
_YT_CARDS = 24
# 一条视频的时长上限（秒）：一年。`lengthSeconds` 是外站 JSON 里的一个读数，可能被写成
# `1e400`（解出来是 inf）或一长串数字（Python 大整数），也可能只是坏数据；比一年还长的
# 「视频」不是视频，当认不出。留着它只会在卡片角上印出一串三百位的数字。
_YT_LEN_MAX = 366 * 24 * 3600

# 未登录时 youtube.com 的首页与几个 feed 页在 `ytInitialData` 里一张卡都不铺。2026-10-16
# 实测：首页 950KB、`videoId` 出现 0 次，正文只有一句「搜索视频，开启 YouTube 之旅」；
# `/feed/trending`、`/gaming` 同样（真浏览器打开也是一样，不是我们取漏了）。取回来是成了，
# 可用户看到的是一页空的 —— 报的就是「打不开」。空着手时按这个次序换一处有货的落脚点，
# 第一个能出卡的就用它。放搜索结果在前：它的内容跟搜索词走，比 feed 那种随 IP 地区变的稳。
YT_LANDINGS = ("https://www.youtube.com/results?search_query=%E7%83%AD%E9%97%A8",
               "https://www.youtube.com/music")
_YT_EMPTY_NOTE = ("这一页要油管自己的脚本才画得出内容，那套脚本在这个沙箱里跑不动。"
                  "先在上面搜，搜到的结果就能挑。")
_YT_FILL_NOTE = ("油管的首页 / feed 在未登录时一张卡都不铺（它只留一句「搜索视频，开启 "
                 "YouTube 之旅」），所以这一页换了一处有内容的落脚点。想找别的，在上面搜。")


def is_youtube(url):
    try:
        host = (urllib.parse.urlparse(url or "").hostname or "").lower()
    except ValueError:
        return False
    return host in YT_HOSTS or host.endswith(".youtube.com")


def _json_after(html, marker):
    """从 `marker` 后面那第一个左花括号起，按配对扫出一整个 JSON 对象。

    不能用贪婪正则：`{.*};` 会在字符串里的那半个 `}` 上断错，也会在几百 KB 的页面上
     backtrack 到没脾气。这里手写一遍扫描（字符串内的花括号不算数），一次线性扫完。
    """
    i = html.find(marker)
    if i < 0:
        return None
    j = html.find("{", i)
    if j < 0:
        return None
    depth, instr, esc = 0, False, False
    for k in range(j, len(html)):
        c = html[k]
        if instr:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                instr = False
            continue
        if c == '"':
            instr = True
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(html[j:k + 1])
                except ValueError:
                    return None
    return None


def _collect(node, key, out):
    """深度收集某个 key 下的所有 dict。

    直接按「已知路径」取是取不动的：油管把这些 renderer 埋在多少层包装里随版本变。
    认 key 不认路径，它加一层壳我们不用改。注意只收 dict 值 —— 「lockupViewModel」
    这种名字在页面配置里也会以字符串形式出现，按值类型筛一道就不会拿错东西。
    """
    if isinstance(node, dict):
        for k, v in node.items():
            if k == key and isinstance(v, dict):
                out.append(v)
            _collect(v, key, out)
    elif isinstance(node, list):
        for v in node:
            _collect(v, key, out)


def _runs(node):
    """`{runs:[{text}]}` 与 `{simpleText}` 两种都见过，收成一条字符串。"""
    if not isinstance(node, dict):
        return ""
    if node.get("simpleText"):
        return str(node["simpleText"])
    return "".join(str(r.get("text") or "") for r in (node.get("runs") or [])
                   if isinstance(r, dict))


def _yt_num(v):
    """外站 JSON 里的一个读数收成「非负整数」；认不出回 0。

    为什么不能直接 `int(v)`：这一页的数是油管页面自带 JSON 里的，浏览器那边怎么写的
    我们管不着。`json` 认 `Infinity` / `NaN` / `1e400`（后两个都是 inf / nan），也认
    一长串数字（解出来是 Python 大整数）—— 而 `int(inf)` 与 `float(10**400)` 抛的是
    `OverflowError`（它不在 `ValueError` 里）。原先 `_yt_img` 只兜
    `except (TypeError, ValueError)`，于是一个 `"width": 1e400` 就把整屏静态目录
    打成 500（`shape` 那条路上没有别的兜底）。口径与 login / readplan / mindmap 一致：
    不有限当认不出，别的卡片照常铺。
    """
    if isinstance(v, bool):
        return 0
    try:
        f = float(v)
    except (TypeError, ValueError, OverflowError):
        return 0
    if not math.isfinite(f):
        return 0
    return max(0, int(f))


def _yt_img(sources):
    """从一组同源缩略图里挑最宽的那张。

    油管按 `sources` 从低清到高清排（168×94 在前、336×188 在后），照第一个取会拿到
    糊的那张；只认自家图床的 https 地址，别的域名一律不往这一页里引。`width` 走
    `_yt_num`：认不出的当 0，仍然会选中第一个合法图床的地址。
    """
    best, best_w = "", -1
    for s in sources or []:
        if not isinstance(s, dict):
            continue
        u = str(s.get("url") or "")
        try:
            host = (urllib.parse.urlparse(u).hostname or "").lower()
        except ValueError:
            continue
        if not u.startswith("https://") or host not in _YT_IMG_HOSTS:
            continue
        w = _yt_num(s.get("width"))
        if w > best_w:
            best, best_w = u, w
    return _html.escape(best, quote=True) if best else ""


def _yt_cards(data):
    """从 ytInitialData 里认出「一堆视频」，新旧两代 renderer 都认。

    旧版是 `videoRenderer`（搜索结果、首页那一批），新版是 `lockupViewModel`
    （watch 页的相关列表、频道页）。两代的字段路径完全不同，所以各读各的，
    统一收成 {id,title,channel,length,when,thumb}。
    """
    out = []
    renderers = []
    _collect(data, "videoRenderer", renderers)
    for r in renderers:
        vid = str(r.get("videoId") or "")
        if not _YT_ID.match(vid):
            continue
        card = {
            "id": vid,
            "title": _runs(r.get("title")),
            "channel": _runs(r.get("ownerText")) or _runs(r.get("longBylineText")),
            "length": _runs(r.get("lengthText")),
            "when": _runs(r.get("publishedTimeText")),
            "thumb": _yt_img((r.get("thumbnail") or {}).get("thumbnails") or []),
        }
        if card["title"]:
            out.append(card)
    if not out:
        lockups = []
        _collect(data, "lockupViewModel", lockups)
        for x in lockups:
            vid = str(x.get("contentId") or "")
            if not _YT_ID.match(vid):
                continue
            if "VIDEO" not in str(x.get("contentType") or ""):
                continue
            md = (x.get("metadata") or {}).get("lockupMetadataViewModel") or {}
            parts = []
            meta = (md.get("metadata") or {}).get("contentMetadataViewModel") or {}
            for row in meta.get("metadataRows") or []:
                for p in (row.get("metadataParts") or []):
                    txt = str(((p or {}).get("text") or {}).get("content") or "")
                    if txt:
                        parts.append(txt)
            image = (x.get("contentImage") or {}).get("thumbnailViewModel") or {}
            length = ""
            for ov in image.get("overlays") or []:
                for badge in (ov.get("thumbnailBottomOverlayViewModel") or {}).get("badges") or []:
                    length = length or str(
                        (badge.get("thumbnailBadgeViewModel") or {}).get("text") or "")
            out.append({"id": vid,
                        "title": str((md.get("title") or {}).get("content") or ""),
                        "channel": parts[0] if parts else "",
                        "length": length,
                        "when": parts[-1] if len(parts) > 1 else "",
                        "thumb": _yt_img((image.get("image") or {}).get("sources") or [])})
    return [c for c in out if c.get("title")][:_YT_CARDS]


_YT_WHY = (
    "为什么是这一页：youtube.com 的正文不在 HTML 里，而在页面自带的一坨 JSON 里，"
    "由它自己的脚本现渲染；归藏的 iframe 是不透明源，那些脚本一碰本地存储就断，"
    "所以原样送回来只会是一片空白。这里读的是同一坨 JSON，铺的是自己的版式 —— "
    "标题、频道、时长、缩略图都是油管给的原文。想整本转成笔记，点卡片上那颗"
    "「转成笔记」，链接会送回「视频转笔记」那一屏。")

_YT_STYLE = """
html{color-scheme:light dark}
body{margin:0;padding:16px 18px 30px;background:#f6f7f9;color:#3c4657;
  font:14px/1.65 -apple-system,BlinkMacSystemFont,'Segoe UI','PingFang SC',sans-serif}
a{color:inherit;text-decoration:none}
.gz-yt h2{font-size:15px;margin:2px 0 3px}
.gz-yt .sub{font-size:12px;color:#6b7688;margin:0 0 14px}
.gz-yt .why{font-size:12px;color:#6b7688;margin:0 0 16px;padding:11px 13px;border-radius:12px;
  background:#fff;border:1px solid rgba(127,140,160,.24)}
.gz-yt form{display:flex;gap:8px;margin:0 0 16px}
.gz-yt input{flex:1;min-width:0;height:34px;padding:0 12px;border-radius:10px;
  border:1px solid rgba(127,140,160,.32);background:#fff;color:inherit;font:inherit;
  font-size:13px;outline:none}
.gz-yt button{height:34px;padding:0 15px;border-radius:10px;border:1px solid rgba(47,127,240,.42);
  background:rgba(47,127,240,.1);color:#2f6fd0;font:inherit;font-size:13px;cursor:pointer;
  white-space:nowrap}
.gz-yt .grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(232px,1fr));gap:14px}
.gz-yt .card{display:flex;flex-direction:column;background:#fff;border-radius:14px;
  border:1px solid rgba(127,140,160,.24);overflow:hidden}
.gz-yt .th{position:relative;display:block;background:#e7eaef}
.gz-yt .th img{display:block;width:100%;height:auto;aspect-ratio:16/9;object-fit:cover}
.gz-yt .lg{position:absolute;right:7px;bottom:7px;padding:2px 6px;border-radius:6px;
  background:rgba(12,16,22,.78);color:#fff;font-size:11px;font-variant-numeric:tabular-nums}
.gz-yt .bd{padding:10px 12px 11px;display:flex;flex-direction:column;gap:6px;flex:1}
.gz-yt .ti{font-size:13px;line-height:1.5;font-weight:600;display:-webkit-box;
  -webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
.gz-yt .me{font-size:11.5px;color:#6b7688;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.gz-yt .go{display:flex;gap:7px;margin-top:auto;flex-wrap:wrap}
.gz-yt .go a{height:28px;line-height:28px;padding:0 11px;border-radius:8px;font-size:12px;
  border:1px solid rgba(127,140,160,.3);background:rgba(127,140,160,.09);white-space:nowrap}
.gz-yt .go a.use{border-color:rgba(47,127,240,.42);background:rgba(47,127,240,.12);color:#2f6fd0}
.gz-yt .card.hero{grid-column:1/-1;flex-direction:row}
.gz-yt .card.hero .th{width:min(340px,46%);flex:none}
.gz-yt .card.hero .th img{height:100%;aspect-ratio:auto}
.gz-yt .card.hero .ti{font-size:16px;-webkit-line-clamp:3}
.gz-yt .none{padding:22px 0;font-size:13px;color:#6b7688}
@media (prefers-color-scheme:dark){
  body{background:#12161d;color:#d6dce6}
  .gz-yt .why,.gz-yt .card{background:#1a1f28;border-color:rgba(160,175,200,.22)}
  .gz-yt input{background:#12161d;border-color:rgba(160,175,200,.28);color:#d6dce6}
  .gz-yt .sub,.gz-yt .why,.gz-yt .me,.gz-yt .none{color:#93a0b3}
  .gz-yt .th{background:#232a35}
}
"""


def _yt_page(cards, note):
    """把认出来的那一堆视频铺成静态目录页。链接全用绝对地址，交给补的那段脚本回传。"""
    watch = "https://www.youtube.com/watch?v=%s"
    items = []
    for c in cards:
        href = _html.escape(watch % urllib.parse.quote(c["id"]), quote=True)
        bits = [b for b in (c.get("channel"), c.get("when")) if b]
        img = ("<img src=\"%s\" alt=\"\" loading=\"lazy\" decoding=\"async\">" % c["thumb"]
               if c.get("thumb") else "")
        lg = ("<span class=\"lg\">%s</span>" % _html.escape(c["length"], quote=True)
              if c.get("length") else "")
        items.append(
            "<article class=\"card%s\"><a class=\"th\" href=\"%s\">%s%s</a>"
            "<div class=\"bd\"><b class=\"ti\">%s</b>"
            "<span class=\"me\">%s</span>"
            "<span class=\"go\"><a href=\"%s\">打开这支</a>"
            "<a class=\"use\" href=\"%s\" data-gz-use"
            " title=\"把这条链接送回「视频转笔记」，回去点手指就开始转\">转成笔记</a>"
            "</span></div></article>"
            % (" hero" if c.get("hero") else "", href, img, lg,
               _html.escape(c["title"], quote=True),
               _html.escape(" · ".join(bits), quote=True), href, href))
    grid = ("<div class=\"grid\">%s</div>" % "".join(items)) if items \
        else "<p class=\"none\">这一页里没认出能列的视频。</p>"
    return ("<!doctype html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\">"
            "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
            "<title>YouTube · 归藏里的静态目录</title><style>%s</style></head><body>"
            "<div class=\"gz-yt\"><h2>YouTube · 静态目录</h2>"
            "<p class=\"sub\">%s</p>"
            "<form method=\"get\" action=\"https://www.youtube.com/results\">"
            "<input name=\"search_query\" placeholder=\"搜点什么？回车就搜（这一页由本机铺，"
            "油管自己的播放器不在这里跑）\" autocomplete=\"off\">"
            "<button type=\"submit\">搜索</button></form>"
            "%s<p class=\"why\">%s</p></div></body></html>"
            ) % (_YT_STYLE, _html.escape(note, quote=True), grid, _YT_WHY)


def _yt_len(sec):
    """`lengthSeconds` 是秒数，翻成「1 小时 4 分」这种看得懂的话。

    这个数来自页面自带的那坨 JSON（外站说了算）：`Infinity` / `1e400`（`json` 都认，
    读回来是 inf）与一长串数字（Python 大整数）到 `int()` / `float()` 那一步抛的是
    `OverflowError`，不在原先那个 `except (TypeError, ValueError)` 里 —— 一个越界的
    时长读数就能把整屏静态目录打成 500。与 `_yt_num` 同一把尺子：不有限、认不出、
    比 `_YT_LEN_MAX` 还长的都当「不知道时长」，回空串，卡片照常铺。
    """
    if isinstance(sec, bool):
        return ""
    try:
        f = float(sec)
    except (TypeError, ValueError, OverflowError):
        return ""
    if not math.isfinite(f) or f <= 0 or f > _YT_LEN_MAX:
        return ""
    s = int(f)
    h, m, r = s // 3600, s % 3600 // 60, s % 60
    if h:
        return "%d:%02d:%02d" % (h, m, r)
    return "%d:%02d" % (m, r)


def _yt_hero(text):
    """watch 那一页的主角不在 ytInitialData 的卡片里，在播放器配置里：单独读出来。

    回一个卡片 dict 或 None。省得用户打开一支视频却只看到「相关」。
    """
    player = _json_after(text, "ytInitialPlayerResponse") or {}
    vd = player.get("videoDetails") or {}
    hero = str(vd.get("videoId") or "")
    if not (vd.get("title") and hero and _YT_ID.match(hero)):
        return None
    return {"id": hero, "hero": True,
            "title": str(vd.get("title") or ""),
            "channel": str(vd.get("author") or ""),
            "length": _yt_len(vd.get("lengthSeconds")),
            "when": "",
            "thumb": _yt_img((vd.get("thumbnail") or {}).get("thumbnails") or [])}


def yt_static(text, url, note=None):
    """YouTube 的一条路：认得出卡片就铺静态页，认不出也给它一页能搜的壳。

    回 None 表示「这跟 YouTube 无关」，由 `shape()` 走原来那条原样重送的路。
    `note` 给了就用它当副标题（空壳换了落脚点时由调用方说明为什么换）。
    """
    if not is_youtube(url):
        return None
    data = _json_after(text, "ytInitialData")
    cards = _yt_cards(data) if data else []
    hero = _yt_hero(text)
    if hero:
        cards.insert(0, hero)
    if note is None:
        note = "从这页挑一支，点「转成笔记」把它送回工作台。"
        if not cards:
            note = _YT_EMPTY_NOTE
    return _yt_page(cards, note)


def _yt_fill(url):
    """空壳页（未登录首页 / feed）换一处有货的落脚点；回 None 表示这次也没找到。

    `url` 里带着用户敲的搜索词时不换 —— 那是他要的东西，换掉等于替他改主意。
    换到的那一页再走一遍 `yt_static`，认得出卡片才用它。

    空搜索（`?search_query=`）算换：那一页跟首页一样没内容，我们自己的搜索框敲空了
    提交出来的也是这个地址。
    """
    if not is_youtube(url):
        return None
    try:
        query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
    except ValueError:
        query = {}
    if (query.get("search_query") or [""])[0].strip():
        return None
    for landing in YT_LANDINGS:
        try:
            req = urllib.request.Request(landing, headers={
                "User-Agent": UA,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "zh-CN,zh;q=0.9",
            })
            with urllib.request.build_opener(_RedirectGuard).open(req, timeout=TIMEOUT) as r:
                blob = r.read(MAX_BYTES + 1)
                if len(blob) > MAX_BYTES:
                    continue
                text = blob.decode(clip_article.safe_charset(r.headers.get_content_charset()),
                                   errors="replace")
                final = r.geturl()
        except Exception:
            continue
        if _yt_cards(_json_after(text, "ytInitialData") or {}):
            return yt_static(text, final, note=_YT_FILL_NOTE)
    return None


def shape(blob, ctype, charset, final_url):
    """把取回来的字节收成 {ok, url, ctype, body}。除了油管那一条空壳兜底，全程不联网。

    服务端声明的字符集同样不可信：用的是 `clip_article.safe_charset`，
    认不出的编码名退回 utf-8，而不是让一个手滑的 `charset=…` 把这一屏打成 500。
    """
    ctype = (ctype or "").lower()
    if _looks_html(blob, ctype):
        text = blob.decode(clip_article.safe_charset(charset), errors="replace")
        # YouTube 那一站原样重送是一屏空白（见上面那段注释），先给它换成本机自己铺的静态页。
        static = yt_static(text, final_url)
        # 未登录的首页 / feed 连那坨 JSON 里都不放内容 —— 那一页是空的。空着手时再换一处
        # 有货的落脚点，别把一页空的端给用户（用户报的「打不开」就是这一页）。
        if static is not None and "class=\"card" not in static:
            static = _yt_fill(final_url) or static
        page = prepare(static if static else text, final_url)
        return {"ok": True, "url": final_url,
                "ctype": "text/html; charset=utf-8", "body": page.encode("utf-8")}
    out = ctype or "application/octet-stream"
    if charset and out.startswith("text/"):
        out = "%s; charset=%s" % (out, clip_article.safe_charset(charset))
    return {"ok": True, "url": final_url, "ctype": out, "body": blob}


def browse(raw):
    """内置浏览器取一页的入口。永远回一个 dict，失败也带人话，不抛。"""
    try:
        u = normalize(raw)
    except Blocked as e:
        return {"ok": False, "url": raw or "", "msg": str(e)}
    req = urllib.request.Request(u, headers={
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/*,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9",
    })
    opener = urllib.request.build_opener(_RedirectGuard)
    try:
        with opener.open(req, timeout=TIMEOUT) as r:
            final = r.geturl()
            ctype = r.headers.get_content_type()
            charset = r.headers.get_content_charset()
            blob = r.read(MAX_BYTES + 1)
    except Exception as e:
        return {"ok": False, "url": u, "msg": "打不开这个页面：%s" % str(e)[:120]}
    if len(blob) > MAX_BYTES:
        return {"ok": False, "url": final,
                "msg": "这个页面超过 4MB，内置浏览器先不装进来"}
    return shape(blob, ctype, charset, final)


def error_page(msg, url=""):
    """取不回来时铺的一页。样子与界面一致，不再由内核甩自己的错误页。"""
    safe_msg = _html.escape(msg or "这一页没取回来", quote=True)
    safe_url = _html.escape(url or "", quote=True)
    return ("<!doctype html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\">"
            "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
            "<style>"
            "html,body{height:100%%;margin:0}"
            "body{display:grid;place-items:center;background:#f6f7f9;color:#3c4657;"
            "font:14px/1.7 -apple-system,BlinkMacSystemFont,'Segoe UI','PingFang SC',sans-serif}"
            ".bz-fail{max-width:420px;padding:26px 28px;border-radius:14px;background:#fff;"
            "border:1px solid rgba(127,140,160,.24);box-shadow:0 12px 34px -22px rgba(20,30,50,.5)}"
            ".bz-fail b{display:block;font-size:15px;margin-bottom:8px}"
            ".bz-fail p{margin:0;color:#6b7688;word-break:break-all}"
            ".bz-fail u{display:block;margin-top:10px;color:#9aa4b4;font-size:12px;"
            "text-decoration:none;word-break:break-all}"
            "@media (prefers-color-scheme:dark){"
            "body{background:#12161d;color:#d6dce6}"
            ".bz-fail{background:#1a1f28;border-color:rgba(160,175,200,.22)}"
            ".bz-fail p{color:#93a0b3}.bz-fail u{color:#6f7c90}}"
            "</style></head><body>"
            "<div class=\"bz-fail\"><b>这一页没取回来</b><p>%s</p><u>%s</u></div>"
            "</body></html>") % (safe_msg, safe_url)
