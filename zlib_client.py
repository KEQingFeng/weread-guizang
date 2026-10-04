# -*- coding: utf-8 -*-
"""Z-Library 取书（走安卓 App 那套 eAPI，`/eapi/...`）。

为什么是这一套、而不是某个现成的 Python 包：Z-Library 客户端统一走 eAPI —— 登录
用邮箱密码换回一对 `remix_userid` / `remix_userkey` 令牌，之后每个请求把这对值当
cookie 带上；搜索、详情、取下载链接都是它的几个固定端点（口径见
github.com/baroxyton/zlibrary-eapi-documentation，实现参考 github.com/bipinkrish/Zlibrary-API，
两者都只作对照，没有代码进这份仓库）。所以这里就是照着那几个端点数出来，不需要
引第三方库。

代理：Z-Library 的域名在国内被 DNS 污染，直连必失败，所以要借本机代理出去。这里
复用 `platform_compat` 那套「环境里显式给了就用、否则读系统设置并探一下端口通不通」
的逻辑 —— 探通了才挂，探不通当直连（本来就在墙外或局域网里跑的机器不需要它）。

依赖：只用标准库。后端其余部分（sync / clip_article / export_precise）全走
`urllib`，这里也一路 `urllib`，不新增要打包、要在 bootstrap 清单里同步一份的依赖。

隐私：登录只把换回来的 remix 令牌存本机（0600），**不存邮箱密码**；界面回显一律
只给后四位。密码在登录那一次请求里用完即弃。

为什么文件名是 `zlib_client.py` 而不是 `zlib.py`：标准库已经有一个 zlib，本文件
所在的目录一旦进 sys.path（源码直接跑、装成 app 都是这样），`import zlib` 会先命中
标准库那份（`shutil` 等在导入期就把它塞进 sys.modules 了），仓库里这份永远轮不到。
名字错开一位，谁都别想抢。
"""

import json
import os
import re
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request

try:
    import platform_compat as pc
except Exception:                       # 单独跑这个模块做冒烟时不至于起不来
    pc = None

DEFAULT_DOMAIN = "1lib.sk"              # 主域名；被污染也没关系，真正的请求走代理
CRED_NAME = "zlib.json"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
# 能进阅读器的格式（book_import.guess_format 只认 epub/pdf；mobi/azw3 收进来读不了，
# 宁可不列出来，也免得下完打不开）
KEEP_EXT = ("epub", "pdf")


class ZlibError(Exception):
    """这一路所有能说到用户面前的话都从这里出去（不抛底层异常细节）。"""


# ── 凭据：只落本机，只存令牌 ──────────────────────────────────────

def cred_path(data_dir):
    return os.path.join(data_dir, CRED_NAME)


def load_cred(data_dir):
    try:
        with open(cred_path(data_dir), encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def save_cred(data_dir, d):
    os.makedirs(data_dir, exist_ok=True)
    p = cred_path(data_dir)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=2)
    os.chmod(tmp, 0o600)                # 令牌等同账号，别让同机别的用户读得到
    os.replace(tmp, p)
    try:
        os.chmod(p, 0o600)
    except Exception:
        pass


def clear_cred(data_dir):
    for p in (cred_path(data_dir), cred_path(data_dir) + ".tmp"):
        try:
            os.remove(p)
        except OSError:
            pass


# ── 网络：代理探测 + 一个统一的 opener ────────────────────────────

def _proxy_url():
    """该借哪个代理出去：环境里显式给的优先，否则读系统设置并探端口。

    探不通就不挂 —— 用户关了代理却留着系统设置时，硬塞一个死地址会把本来能成的
    直连也一起拖垮。socks 代理 urllib 不带 PySocks 走不通，同样跳过。
    """
    for k in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy"):
        v = (os.environ.get(k) or "").strip()
        if v:
            return v
    try:
        p = urllib.request.getproxies()
    except Exception:
        return ""
    for k in ("https", "http"):
        v = (p.get(k) or "").strip()
        if not v or v.startswith("socks"):
            continue
        if pc is not None and not pc._proxy_alive(v):
            continue
        return v
    return ""


def _opener():
    proxy = _proxy_url()
    handlers = [urllib.request.ProxyHandler(
        {"http": proxy, "https": proxy} if proxy else {})]
    handlers.append(urllib.request.HTTPSHandler(context=ssl.create_default_context()))
    return urllib.request.build_opener(*handlers)


def _cookies(cred):
    ck = {"siteLanguageV2": "en"}
    uid = str(cred.get("remix_userid") or "").strip()
    key = (cred.get("remix_userkey") or "").strip()
    if uid and key:
        ck["remix_userid"] = uid
        ck["remix_userkey"] = key
    return ck


def _base(cred):
    d = (cred.get("domain") or "").strip() or DEFAULT_DOMAIN
    return d if "://" in d else "https://" + d


def _msg_of(d):
    for k in ("error", "message", "msg"):
        v = d.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()[:200]
    return ""


def _net_hint(e):
    """把底层网络错误翻成一句人话，并点出「多半是代理」这个最常见的成因。"""
    s = str(e)
    if isinstance(e, urllib.error.URLError):
        s = str(getattr(e, "reason", e))
    if "timed out" in s.lower():
        return "等超时了。Z-Library 在国内要经代理才能连上，检查代理是否开着。"
    return "连不上 Z-Library：%s（国内需经代理，检查代理是否开着）" % s[:120]


def _call(cred, path, data=None, params=None, cookies=None, timeout=25, opener=None):
    """对 eAPI 发一次请求并解析 JSON。POST 用 urlencoded（eAPI 就是这口径）。"""
    url = _base(cred).rstrip("/") + path
    if params:
        url += "?" + urllib.parse.urlencode(params, doseq=True)
    body = None
    headers = {"User-Agent": UA, "Accept": "application/json, text/plain, */*",
               "Accept-Language": "en-US,en;q=0.9"}
    if data is not None:
        body = urllib.parse.urlencode(data, doseq=True).encode("utf-8")
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    ck = _cookies(cred) if cookies is None else cookies
    if ck:
        headers["Cookie"] = "; ".join("%s=%s" % (k, v) for k, v in ck.items())
    req = urllib.request.Request(url, data=body, headers=headers,
                                 method="POST" if body is not None else "GET")
    op = opener or _opener()
    try:
        with op.open(req, timeout=timeout) as r:
            raw = r.read()
    except urllib.error.HTTPError as e:
        raise ZlibError("Z-Library 回了 %s（%s）" % (e.code, path))
    except Exception as e:
        raise ZlibError(_net_hint(e))
    try:
        return json.loads(raw.decode("utf-8", "replace"))
    except Exception:
        raise ZlibError("Z-Library 没回 JSON —— 多半是被挡了，检查代理。")


def _get_bytes(cred, url, timeout=90, opener=None):
    headers = {"User-Agent": UA, "Accept": "*/*"}
    ck = _cookies(cred)
    if ck:
        headers["Cookie"] = "; ".join("%s=%s" % (k, v) for k, v in ck.items())
    req = urllib.request.Request(url, headers=headers)
    op = opener or _opener()
    try:
        with op.open(req, timeout=timeout) as r:
            return r.read()
    except Exception as e:
        raise ZlibError("文件没下下来：%s" % str(e)[:120])


# ── 端到端几个动作 ────────────────────────────────────────────────

def _fetch_personal_domain(cred, opener=None):
    """问一次个人专属域名。

    Z-Library 现在把下载放在每人一份的域名上，能问到就用它；问不到（接口形状变了、
    或这个账号没有）就返回空串，调用方退回主域名 —— 少一个域名不该让整条线报错。
    """
    try:
        d = _call(cred, "/eapi/info/domains", opener=opener)
    except Exception:
        return ""
    for k in ("personal_domain", "personalDomain", "domain"):
        v = d.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    ds = d.get("domains")
    if isinstance(ds, list):
        for item in ds:
            if isinstance(item, str) and item.strip():
                return item.strip()
            if isinstance(item, dict):
                for k in ("domain", "url"):
                    v = item.get(k)
                    if isinstance(v, str) and v.strip():
                        return v.strip()
    return ""


def login(data_dir, email, password, domain=None, opener=None):
    """用邮箱密码换令牌并存下来。只存令牌，密码不落盘。"""
    email = (email or "").strip()
    if not email or not password:
        raise ZlibError("邮箱和密码都要填")
    cred = load_cred(data_dir)
    if domain:
        cred["domain"] = (domain or "").strip()
    fresh = dict(cred)
    fresh.pop("remix_userid", None)     # 登录请求别带旧令牌，免得过期令牌把新登录搅黄
    fresh.pop("remix_userkey", None)
    d = _call(fresh, "/eapi/user/login",
              data={"email": email, "password": password}, opener=opener)
    u = d.get("user")
    if not d.get("success") or not isinstance(u, dict) or not u.get("remix_userkey"):
        raise ZlibError(_msg_of(d) or "登录没成功（检查邮箱 / 密码）")
    cred.update({
        "email": u.get("email") or email,
        "name": u.get("name") or "",
        "remix_userid": str(u.get("id") or ""),
        "remix_userkey": u.get("remix_userkey") or "",
        "saved_at": int(time.time()),
    })
    pd = _fetch_personal_domain(cred, opener=opener)
    if pd:
        cred["personal_domain"] = pd
    save_cred(data_dir, cred)
    return status(data_dir)


def login_with_token(data_dir, userid, userkey, domain=None, opener=None):
    """已有令牌时直接用它登录（比存密码干净，适合从浏览器 cookie 里抄一份）。

    先用令牌问一次 profile：问得到才存 —— 免得抄错一位也照样「登录成功」，等到
    真下载那一刻才报错。
    """
    userid = str(userid or "").strip()
    userkey = (userkey or "").strip()
    if not userid or not userkey:
        raise ZlibError("userid 与 userkey 都要填")
    cred = load_cred(data_dir)
    if domain:
        cred["domain"] = (domain or "").strip()
    cred["remix_userid"] = userid
    cred["remix_userkey"] = userkey
    d = _call(cred, "/eapi/user/profile", opener=opener)
    u = d.get("user")
    if not d.get("success") or not isinstance(u, dict):
        raise ZlibError(_msg_of(d) or "令牌没通过校验（抄漏了？或已过期）")
    cred["email"] = u.get("email") or cred.get("email") or ""
    cred["name"] = u.get("name") or cred.get("name") or ""
    cred["remix_userid"] = str(u.get("id") or userid)
    pd = _fetch_personal_domain(cred, opener=opener)
    if pd:
        cred["personal_domain"] = pd
    save_cred(data_dir, cred)
    return status(data_dir)


def status(data_dir):
    """本机认得的登录状态。不发网络请求 —— 界面上那行摘要要能秒出。"""
    cred = load_cred(data_dir)
    uid = str(cred.get("remix_userid") or "")
    key = str(cred.get("remix_userkey") or "")
    return {
        "logged_in": bool(uid and key),
        "email": cred.get("email") or "",
        "name": cred.get("name") or "",
        "uid_last4": uid[-4:],
        "key_last4": key[-4:],
        "domain": cred.get("domain") or DEFAULT_DOMAIN,
        "personal_domain": cred.get("personal_domain") or "",
    }


def set_domain(data_dir, domain):
    cred = load_cred(data_dir)
    cred["domain"] = (domain or "").strip() or DEFAULT_DOMAIN
    save_cred(data_dir, cred)
    return status(data_dir)


def _norm(b):
    ext = (b.get("extension") or "").lower()
    return {
        "id": str(b.get("id") or ""),
        "hash": b.get("hash") or "",
        "title": b.get("title") or "",
        "author": b.get("author") or "",
        "year": str(b.get("year") or ""),
        "language": b.get("language") or "",
        "extension": ext,
        "size": b.get("filesizeString") or b.get("size") or "",
        "cover": b.get("cover") or "",
        "publisher": b.get("publisher") or "",
        "readable": ext in KEEP_EXT,     # 能不能收进阅读器（前端据此决定给不给下载钮）
    }


def search(data_dir, message, page=1, limit=20, extensions=None, languages=None,
           order="popular", opener=None):
    """搜书。匿名也能搜；带上令牌时结果里会多出「已收藏」之类的标记。"""
    message = (message or "").strip()
    if not message:
        raise ZlibError("先写个关键词")
    cred = load_cred(data_dir)
    data = {"message": message, "page": int(page), "limit": int(limit)}
    if order:
        data["order"] = order
    if extensions:
        data["extensions[]"] = ([extensions] if isinstance(extensions, str)
                               else list(extensions))
    if languages:
        data["languages"] = languages
    d = _call(cred, "/eapi/book/search", data=data, opener=opener)
    books = d.get("books") if isinstance(d.get("books"), list) else []
    pag = d.get("pagination") if isinstance(d.get("pagination"), dict) else {}
    return {
        "books": [_norm(b) for b in books if isinstance(b, dict)],
        "total": pag.get("total"),
        "page": int(page),
        "logged_in": bool(cred.get("remix_userkey")),
    }


def book_file(data_dir, book_id, book_hash, opener=None):
    """取这本书的下载信息（文件名 / 扩展名 / 下载直链）。"""
    book_id = str(book_id or "").strip()
    book_hash = (book_hash or "").strip()
    if not book_id or not book_hash:
        raise ZlibError("这本书缺少 id 或 hash，下不动")
    cred = load_cred(data_dir)
    if not cred.get("remix_userkey"):
        raise ZlibError("下载要先登录 Z-Library（去「设置 → Z-Library」填账号）")
    d = _call(cred, "/eapi/book/%s/%s/file" % (book_id, book_hash), opener=opener)
    f = d.get("file") if isinstance(d.get("file"), dict) else None
    if not d.get("success") or not f or not f.get("downloadLink"):
        raise ZlibError(_msg_of(d) or "这本书现在下不动（可能今日额度用完了）")
    return f


def download(data_dir, book, opener=None):
    """把一本书下回来，返回 (文件名, 字节)。

    `book` 是 search 结果里的一项（含 id / hash / title / author / extension）。
    """
    if not isinstance(book, dict):
        raise ZlibError("没有指明要下哪本书")
    cred = load_cred(data_dir)
    bid = str(book.get("id") or "").strip()
    bhash = (book.get("hash") or book.get("hashid") or "").strip()
    f = book_file(data_dir, bid, bhash, opener=opener)
    link = f.get("downloadLink") or ""
    ext = (f.get("extension") or book.get("extension") or "epub").lower()
    if ext not in KEEP_EXT:
        raise ZlibError("这本是 %s 格式，阅读器打不开（只收 EPUB / PDF）" % (ext or "未知"))
    name = (f.get("description") or book.get("title") or "book").strip()
    au = (f.get("author") or book.get("author") or "").strip()
    if au:
        name += " (%s)" % au
    name = re.sub(r"[\\/:*?\"<>|\x00-\x1f]+", "_", name).strip()[:120] or "book"
    blob = _get_bytes(cred, link, opener=opener)
    if not blob:
        raise ZlibError("下回来是空的，换一本或稍后再试")
    return name + "." + ext, blob


def downloads_left(data_dir, opener=None):
    """今日还能下几本（去问一次 profile；问不到就回 None，界面不显示这一句）。"""
    cred = load_cred(data_dir)
    if not cred.get("remix_userkey"):
        return None
    try:
        d = _call(cred, "/eapi/user/profile", opener=opener)
        u = d.get("user") if isinstance(d.get("user"), dict) else {}
        lim, used = u.get("downloads_limit"), u.get("downloads_today")
        if lim is None:
            return None
        return max(0, int(lim) - int(used or 0))
    except Exception:
        return None


if __name__ == "__main__":
    # 手动冒烟：python3 zlib_client.py status <数据目录>
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "status":
        print(json.dumps(status(sys.argv[2] if len(sys.argv) > 2 else "."),
                         ensure_ascii=False, indent=2))
    else:
        print("用法：python3 zlib_client.py status <数据目录>")
