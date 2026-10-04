# -*- coding: utf-8 -*-
"""Z-Library 取书（zlib_client.py）的离线自测。

为什么单开一份、且全程离线：Z-Library 的域名在国内被 DNS 污染，真机走一遍要用户
的账号 + 本机代理，两样都不该进门禁；而这一路的规矩（登录只存令牌不存密码、回显
只给后四位、搜索结果的「能不能读」判定、下载前必须登录、文件名清理）全是纯逻辑，
用一个本机假 eAPI 完全验得实。所以这里起一个 127.0.0.1 上的假服务充当 Z-Library：
它会数每一次请求，于是「把请求发给哪儿了」「有没有把密码写进磁盘」这两件最要紧的
事，都能断成可失败的检查 —— 全程一个真网请求都不发。

真正会读盘、会写书库的那一步（下载完自动进本地书架）归 ui_server 的接口，见
tests/smoke_api.py 与真机套件。
"""
import http.server
import json
import os
import pathlib
import re
import shutil
import sys
import tempfile
import threading
import urllib.parse
import urllib.request

HERE = pathlib.Path(__file__).resolve().parent
REPO = HERE.parent
sys.path.insert(0, str(REPO))
# 模块名刻意避开 `zlib`：标准库那个 zlib 会被 shutil 之类的链先导入进 sys.modules，
# 于是仓库根下真叫 zlib.py 的这份永远 import 不到（而且反过来还有可能被 zipfile 抢去用）。
import zlib_client as zlib  # noqa: E402

FAIL = []
PASSED = [0]


def chk(name, cond, extra=""):
    if cond:
        PASSED[0] += 1
    else:
        FAIL.append(name)
    print(("PASS  " if cond else "FAIL  ") + name + ("" if cond else "   " + str(extra)[:240]))


def _raises(fn, needle=""):
    """fn 抛 ZlibError、且（给了 needle 时）话里含它，才算对。

    needle 只用来断「我们自己写的那句话」；服务端回的文本（如 unauthorized）不当锚点，
    那种情形留空 needle、只要求它抛 ZlibError 即可。
    """
    try:
        fn()
    except zlib.ZlibError as e:
        return (needle in str(e)) if needle else True
    except Exception as e:                  # 抛了别的类型 = 没按约定收口
        print("       （抛的不是 ZlibError：%r）" % e)
        return False
    return False


# ── 假 eAPI ──────────────────────────────────────────────────────
CREDS = {"email": "reader@example.com", "password": "s3cret-pass",
         "id": 12345, "key": "tok_KEY_9f3a"}
# 每本书的真实格式：下载端点得按 bookId 回各自的格式，否则「mobi 该被挡住」这条
# 就没有东西可打（下载里是先问 file 拿到格式，才决定收不收）。
META_OF = {"111": ("第一本", "作者甲", "epub"),
           "222": ("第二本", "作者乙", "pdf"),
           "333": ("第三本", "作者丙", "mobi")}
ST = {"login": 0, "file": 0, "dl": 0, "reqs": []}
PORT = [0]


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self, obj, status=200):
        b = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def _cookies(self):
        out = {}
        for part in (self.headers.get("Cookie") or "").split(";"):
            if "=" in part:
                k, v = part.strip().split("=", 1)
                out[k] = v
        return out

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        form = dict(urllib.parse.parse_qsl(self.rfile.read(n).decode("utf-8", "replace")))
        ST["reqs"].append(("POST", self.path, form, self._cookies()))
        if self.path == "/eapi/user/login":
            ST["login"] += 1
            if form.get("email") == CREDS["email"] and form.get("password") == CREDS["password"]:
                self._json({"success": True, "user": {
                    "id": CREDS["id"], "email": CREDS["email"], "name": "测试读者",
                    "remix_userkey": CREDS["key"], "kindle_email": ""}})
            else:
                self._json({"success": False, "error": "Wrong email or password"})
            return
        if self.path == "/eapi/book/search":
            self._json({"success": True, "books": [
                {"id": 111, "hash": "abc111", "title": "第一本", "author": "作者甲",
                 "extension": "epub", "filesizeString": "1.2 MB", "year": "2020"},
                {"id": 222, "hash": "abc222", "title": "第二本", "author": "作者乙",
                 "extension": "pdf", "filesizeString": "3 MB"},
                {"id": 333, "hash": "abc333", "title": "第三本", "author": "作者丙",
                 "extension": "mobi"},
            ], "pagination": {"total": 3}})
            return
        self._json({"success": False, "error": "no such endpoint"}, 404)

    def do_GET(self):
        ck = self._cookies()
        ST["reqs"].append(("GET", self.path, {}, ck))
        if self.path.startswith("/eapi/user/profile"):
            ok = (ck.get("remix_userid") == str(CREDS["id"])
                  and ck.get("remix_userkey") == CREDS["key"])
            self._json({"success": True, "user": {
                "id": CREDS["id"], "email": CREDS["email"], "name": "测试读者",
                "downloads_limit": 10, "downloads_today": 3}} if ok
                else {"success": False, "error": "unauthorized"})
            return
        if self.path.startswith("/eapi/info/domains"):
            self._json({"success": True, "domains": ["personal.example"]})
            return
        m = re.match(r"^/eapi/book/(\d+)/([0-9a-z]+)/file$", self.path)
        if m:
            ST["file"] += 1
            bid = m.group(1)
            title, author, ext = META_OF.get(bid, ("未知", "", "epub"))
            self._json({"success": True, "file": {
                "description": title, "author": author, "extension": ext,
                "downloadLink": "http://127.0.0.1:%d/dl/%s.%s" % (PORT[0], bid, ext)}})
            return
        if self.path.startswith("/dl/"):
            ST["dl"] += 1
            blob = b"PK\x03\x04fake-epub-bytes"
            self.send_response(200)
            self.send_header("Content-Type", "application/epub+zip")
            self.send_header("Content-Length", str(len(blob)))
            self.end_headers()
            self.wfile.write(blob)
            return
        self._json({"success": False, "error": "not found"}, 404)


SRV = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
PORT[0] = SRV.server_address[1]
threading.Thread(target=SRV.serve_forever, daemon=True).start()
DOMAIN = "http://127.0.0.1:%d" % PORT[0]

# 集成用例一律走直连：把代理探测钉成「没有代理」，免得跑门禁的机器上系统代理把
# 发往 127.0.0.1 的假请求也绕出去。代理探测本身另有一条用例单独验 —— 那一节用
# 存下来的真函数 _orig_proxy_url，而不是被钉住的这个壳。
_orig_proxy_url = zlib._proxy_url
zlib._proxy_url = lambda: ""

DATA = tempfile.mkdtemp(prefix="gz-zlib-")
FRESH = tempfile.mkdtemp(prefix="gz-zlib-fresh-")   # 全程当「从没登录过」的那只账号

try:
    # ── 1 登录：存令牌、不存密码 ─────────────────────────────────
    st = zlib.login(DATA, CREDS["email"], CREDS["password"], domain=DOMAIN)
    chk("登录成功并认出账号", st["logged_in"] and st["email"] == CREDS["email"], st)
    chk("回显只给后四位（userid）", st["uid_last4"] == "2345", st["uid_last4"])
    chk("回显只给后四位（userkey）", st["key_last4"] == "9f3a", st["key_last4"])
    chk("登录后问到了个人域名", st["personal_domain"] == "personal.example", st)

    raw = open(zlib.cred_path(DATA), encoding="utf-8").read()
    chk("密码没有落盘", CREDS["password"] not in raw, raw[:160])
    chk("令牌落盘了", CREDS["key"] in raw)
    mode = os.stat(zlib.cred_path(DATA)).st_mode & 0o777
    chk("凭据文件是 0600", mode == 0o600, oct(mode))
    post_ck = [ck for m, p, f, ck in ST["reqs"] if m == "POST"]
    chk("登录请求没带旧令牌（干净登录）",
        all("remix_userkey" not in ck and "remix_userid" not in ck for ck in post_ck),
        post_ck)

    # ── 2 搜索：归一化 + 能不能读的判定 ──────────────────────────
    d = zlib.search(DATA, "第一本")
    books = d["books"]
    chk("搜到三本", len(books) == 3 and d["total"] == 3, d)
    chk("epub 可读", books[0]["readable"] and books[0]["extension"] == "epub", books[0])
    chk("pdf 可读", books[1]["readable"], books[1])
    chk("mobi 不可读（阅读器打不开）", not books[2]["readable"], books[2])
    chk("搜索带上了登录令牌", bool(d["logged_in"]))

    # ── 3 下载：先问 file 再取直链 ───────────────────────────────
    name, blob = zlib.download(DATA, books[0])
    chk("下载文件名挑出书名与作者", name == "第一本 (作者甲).epub", name)
    chk("下回来的是文件字节", blob.startswith(b"PK"), blob[:12])
    chk("取了下载链接（file 端点）", ST["file"] == 1, ST["file"])
    chk("真的下了一次（直链端点）", ST["dl"] == 1, ST["dl"])
    chk("mobi 拒绝下载并说明原因",
        _raises(lambda: zlib.download(DATA, books[2]), "格式"))

    # ── 4 没登录不许下 ───────────────────────────────────────────
    chk("没登录时下载被拦下并说明",
        _raises(lambda: zlib.download(FRESH, books[0]), "登录"))
    chk("没登录时状态是未登录", not zlib.status(FRESH)["logged_in"])

    # ── 5 令牌登录：抄错要被挡，抄对要过 ─────────────────────────
    chk("错误令牌被 profile 挡下（且没存下来）",
        _raises(lambda: zlib.login_with_token(FRESH, "999", "wrong"))
        and not os.path.exists(zlib.cred_path(FRESH)))
    st = zlib.login_with_token(FRESH, CREDS["id"], CREDS["key"], domain=DOMAIN)
    chk("正确令牌登录通过", st["logged_in"] and st["uid_last4"] == "2345", st)

    # ── 6 登出清干净 ─────────────────────────────────────────────
    zlib.clear_cred(FRESH)
    chk("登出后凭据文件没了", not os.path.exists(zlib.cred_path(FRESH)))
    chk("登出后状态是未登录", not zlib.status(FRESH)["logged_in"])

    # ── 7 代理探测：显式给优先、socks 跳过、探不通不挂 ───────────
    saved_env = {k: os.environ.get(k) for k in
                 ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy")}
    saved_gp = urllib.request.getproxies
    for k in saved_env:
        os.environ.pop(k, None)
    try:
        os.environ["HTTPS_PROXY"] = "http://127.0.0.1:7890"
        chk("环境里显式给了代理就照用", _orig_proxy_url() == "http://127.0.0.1:7890")
        os.environ.pop("HTTPS_PROXY", None)

        urllib.request.getproxies = lambda: {"https": "socks5://127.0.0.1:1080"}
        chk("socks 代理跳过（urllib 走不通）", _orig_proxy_url() == "")

        urllib.request.getproxies = lambda: {"https": "http://127.0.0.1:59999"}
        chk("系统代理探不通就不挂", _orig_proxy_url() == "")
    finally:
        urllib.request.getproxies = saved_gp
        for k, v in saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    # ── 8 全程只对本机发请求（没有把任何东西发给真网） ───────────
    paths = [p for _, p, _, _ in ST["reqs"]]
    chk("登录确实发生了", ST["login"] == 1, ST["login"])
    chk("所有请求都落在本机假服务上（有请求、路径全相对）",
        len(paths) >= 6 and all(p.startswith("/") for p in paths),
        (len(paths), paths))
finally:
    zlib._proxy_url = _orig_proxy_url
    SRV.shutdown()
    shutil.rmtree(DATA, ignore_errors=True)
    shutil.rmtree(FRESH, ignore_errors=True)

print()
if FAIL:
    print("Z-Library 离线自测有 %d 处不对：%s" % (len(FAIL), "、".join(FAIL)))
else:
    print("Z-Library 取书这一路：登录只存令牌、回显只给后四位、下载先登录、"
          "不可读格式挡住、代理探测各情形都对。")
sys.exit(len(FAIL))
