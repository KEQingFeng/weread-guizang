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
  3. 送回去。HTML 原样转交（站点自己的脚本照跑），图片 / PDF 这类非网页直接把字节透传，
     让内核自己渲染。

数据落在哪：这一层不写盘、不留 cookie、不建浏览器档案 —— 它就是「取回来给你看一眼」，
所以没有任何用户数据落进仓库或数据目录。真要登录某个站的，走「安娜的档案」那种自己
开窗口的路子，会话留在浏览器档案里，归藏一行不读。

失败也给人话：取不回来的原因（地址不合法 / 内网被拦 / 超时 / 太大）原样带回界面，
由 `error_page()` 铺成一页，而不是让内核甩一张它自己的错误页。
"""

import html as _html
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


def shape(blob, ctype, charset, final_url):
    """把取回来的字节收成 {ok, url, ctype, body}。纯函数，测起来不用连网。

    服务端声明的字符集同样不可信：用的是 `clip_article.safe_charset`，
    认不出的编码名退回 utf-8，而不是让一个手滑的 `charset=…` 把这一屏打成 500。
    """
    ctype = (ctype or "").lower()
    if _looks_html(blob, ctype):
        text = blob.decode(clip_article.safe_charset(charset), errors="replace")
        page = prepare(text, final_url)
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
