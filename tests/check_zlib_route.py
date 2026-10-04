# -*- coding: utf-8 -*-
"""/api/zlib 这条后端接口的离线走查：下回来的书真的被收进「本地书架」。

跟 check_zlib.py 的分工：那一份只管 zlib_client 这个客户端本身（纯逻辑），
这一份管接口这一层 —— 路由认不认得 mode、登录令牌怎么进来、下载完有没有真的走
`book_import` 落进书库、落进去之后 `book_layout.walk()` 认不认得它是「本地书架」的书。

全程离线：假 eAPI 起在 127.0.0.1，下载直链给的是**一份现编的最小 EPUB**（能过
book_import 的解析），所以「下载 → 收书 → 上架」这一整条是端到端验过的，一个真网
请求都不发。数据目录与书库都指进临时沙盒 —— 开头那句断言先把这件事钉死，免得
哪次改错把用户真实的 cache/、书库动了。
"""
import http.server
import io
import json
import os
import pathlib
import re
import shutil
import sys
import tempfile
import threading
import urllib.parse
import zipfile

HERE = pathlib.Path(__file__).resolve().parent
REPO = HERE.parent
sys.path.insert(0, str(REPO))

# 必须在 import ui_server 之前把两个目录指进沙盒：它在导入时就把 DATA_DIR / OUT_DIR
# 算好了，事后再改环境变量是没用的。
TMP = tempfile.mkdtemp(prefix="gz-zlib-route-")
os.environ["GUIZANG_DATA"] = os.path.join(TMP, "data")
os.environ["GUIZANG_BOOKS"] = os.path.join(TMP, "books")

import ui_server          # noqa: E402
import book_layout        # noqa: E402
import zlib_client as zlib  # noqa: E402

FAIL = []
PASSED = [0]


def chk(name, cond, extra=""):
    if cond:
        PASSED[0] += 1
    else:
        FAIL.append(name)
    print(("PASS  " if cond else "FAIL  ") + name + ("" if cond else "   " + str(extra)[:240]))


# ── 一份现编的最小 EPUB（能过 book_import.read_epub + _chapters_from_docs）──
def mini_epub(title="Z-Library 测试书", author="测试作者", body="正文第一句。第二句。"):
    opf = (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="bid">'
        '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">'
        '<dc:title>%s</dc:title><dc:creator>%s</dc:creator>'
        '<dc:identifier id="bid">urn:uuid:test-0001</dc:identifier></metadata>'
        '<manifest><item id="c1" href="c1.xhtml" media-type="application/xhtml+xml"/></manifest>'
        '<spine><itemref idref="c1"/></spine></package>' % (title, author))
    xhtml = ('<?xml version="1.0" encoding="utf-8"?>'
             '<html xmlns="http://www.w3.org/1999/xhtml"><head><title>第一章</title></head>'
             '<body><h1>第一章</h1><p>%s</p></body></html>' % body)
    container = ('<?xml version="1.0"?><container version="1.0" '
                 'xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles>'
                 '<rootfile full-path="OEBPS/content.opf" '
                 'media-type="application/oebps-package+xml"/></rootfiles></container>')
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("mimetype", "application/epub+zip")
        z.writestr("META-INF/container.xml", container)
        z.writestr("OEBPS/content.opf", opf)
        z.writestr("OEBPS/c1.xhtml", xhtml)
    return buf.getvalue()


EPUB_BYTES = mini_epub()
BOOKS = [
    {"id": 111, "hash": "abc111", "title": "测试书", "author": "测试作者",
     "extension": "epub", "filesizeString": "1.2 MB", "year": "2020"},
    {"id": 222, "hash": "abc222", "title": "第二本", "author": "作者乙",
     "extension": "pdf", "filesizeString": "3 MB"},
    {"id": 333, "hash": "abc333", "title": "第三本", "author": "作者丙",
     "extension": "mobi"},
]
CREDS = {"id": 12345, "key": "tok_KEY_9f3a"}
ST = {"reqs": [], "file": 0, "dl": 0}
PORT = [0]


class Eapi(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self, obj):
        b = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def _bytes(self, blob, ctype):
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(blob)))
        self.end_headers()
        self.wfile.write(blob)

    def _ck(self):
        out = {}
        for part in (self.headers.get("Cookie") or "").split(";"):
            if "=" in part:
                k, v = part.strip().split("=", 1)
                out[k] = v
        return out

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(n)
        ST["reqs"].append(("POST", self.path))
        if self.path == "/eapi/book/search":
            self._json({"success": True, "books": BOOKS, "pagination": {"total": len(BOOKS)}})
            return
        self._json({"success": False, "error": "no such endpoint"})

    def do_GET(self):
        ST["reqs"].append(("GET", self.path))
        ck = self._ck()
        if self.path.startswith("/eapi/user/profile"):
            ok = ck.get("remix_userid") == str(CREDS["id"]) and ck.get("remix_userkey") == CREDS["key"]
            self._json({"success": True, "user": {
                "id": CREDS["id"], "email": "reader@example.com", "name": "测试读者",
                "downloads_limit": 10, "downloads_today": 3}} if ok
                else {"success": False, "error": "unauthorized"})
            return
        if self.path.startswith("/eapi/info/domains"):
            self._json({"success": True, "domains": ["personal.example"]})
            return
        m = re.match(r"^/eapi/book/(\d+)/([0-9a-z]+)/file$", self.path)
        if m:
            ST["file"] += 1
            self._json({"success": True, "file": {
                "description": "测试书", "author": "测试作者", "extension": "epub",
                "downloadLink": "http://127.0.0.1:%d/dl/%s.epub" % (PORT[0], m.group(1))}})
            return
        if self.path.startswith("/dl/"):
            ST["dl"] += 1
            self._bytes(EPUB_BYTES, "application/epub+zip")
            return
        self._json({"success": False, "error": "not found"})


SRV = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Eapi)
PORT[0] = SRV.server_address[1]
threading.Thread(target=SRV.serve_forever, daemon=True).start()
DOMAIN = "http://127.0.0.1:%d" % PORT[0]

# 直连本地假服务：把代理探测钉成「没有代理」，免得机器上的系统代理把 127.0.0.1 也绕出去
_orig_proxy_url = zlib._proxy_url
zlib._proxy_url = lambda: ""

try:
    chk("沙盒到位（数据目录与书库都在临时目录里）",
        TMP in ui_server.DATA_DIR and TMP in ui_server.OUT_DIR,
        (ui_server.DATA_DIR, ui_server.OUT_DIR))

    # ── 1 起手未登录 ─────────────────────────────────────────────
    r = ui_server.zlib_do({"mode": "status"})
    chk("status：回 ok 且未登录", r.get("ok") and not r["zlib"]["logged_in"], r)
    chk("status：没登录时问不出额度（None，界面不显示）", r["zlib"]["downloads_left"] is None, r)

    # ── 2 令牌登录（不下密码这条路）───────────────────────────────
    r = ui_server.zlib_do({"mode": "login", "userid": str(CREDS["id"]),
                           "userkey": CREDS["key"], "domain": DOMAIN})
    chk("login：令牌登录成功", r.get("ok") and r["zlib"]["logged_in"], r)
    chk("login：回显只给后四位", r["zlib"]["uid_last4"] == "2345"
        and r["zlib"]["key_last4"] == "9f3a", r["zlib"])
    chk("login：凭据落在数据目录里", os.path.exists(zlib.cred_path(ui_server.DATA_DIR)))
    chk("login：凭据文件是 0600",
        os.stat(zlib.cred_path(ui_server.DATA_DIR)).st_mode & 0o777 == 0o600)

    # ── 3 搜书（缺省只列阅读器收得下的）──────────────────────────
    r = ui_server.zlib_do({"mode": "search", "q": "测试书"})
    chk("search：回了结果且认得出可读", r.get("ok") and r["books"]
        and r["books"][0]["readable"], r)
    chk("search：空关键词被拦下并说明", (lambda x: not x.get("ok") and "关键词" in x.get("msg", ""))
        (ui_server.zlib_do({"mode": "search", "q": "  "})))

    # ── 4 下载 → 自动收进本地书架 ────────────────────────────────
    r = ui_server.zlib_do({"mode": "download", "book": {"id": "111", "hash": "abc111",
                                                        "title": "测试书", "author": "测试作者",
                                                        "extension": "epub"}})
    chk("download：下回来并收了书", r.get("ok") and r.get("book"), r)
    info = r.get("book") or {}
    chk("download：书号带 zlib_ 前缀", str(info.get("id", "")).startswith("zlib_"), info)
    chk("download：解析出了章节", int(info.get("chapters") or 0) >= 1, info)
    chk("download：真的取过下载直链", ST["file"] == 1 and ST["dl"] == 1, ST)

    shelf = book_layout.walk(ui_server.OUT_DIR)
    locals_ = [(bid, d, mod) for bid, d, mod in shelf if mod == "local"]
    chk("上架：walk 里认得出它属于「本地书架」", len(locals_) == 1, shelf)
    if locals_:
        chk("上架：书名对得上", book_layout.read_meta(locals_[0][1]).get("title") == "测试书",
            book_layout.read_meta(locals_[0][1]))
        chk("上架：确实落在「本地书架」文件夹下",
            os.path.basename(os.path.dirname(locals_[0][1])) == "本地书架", locals_[0][1])

    # ── 5 坏输入不断线 ───────────────────────────────────────────
    chk("download：没指明书就拒绝", not ui_server.zlib_do({"mode": "download"}).get("ok"))
    chk("未知动作：回一句「不认识」而不是崩",
        ui_server.zlib_do({"mode": "乱七八糟"}).get("msg") == "不认识这个动作")

    # ── 6 登出：清干净，之后下载被拦 ─────────────────────────────
    r = ui_server.zlib_do({"mode": "logout"})
    chk("logout：清掉凭据且状态转未登录",
        r.get("ok") and not r["zlib"]["logged_in"]
        and not os.path.exists(zlib.cred_path(ui_server.DATA_DIR)), r)
    r = ui_server.zlib_do({"mode": "download", "book": {"id": "111", "hash": "abc111"}})
    chk("登出后下载：被拦下并说明要登录",
        not r.get("ok") and "登录" in r.get("msg", ""), r)

    # ── 7 全程只对本机发请求 ─────────────────────────────────────
    paths = [p for _, p in ST["reqs"]]
    chk("所有请求都落在本机假服务上（有请求、路径全相对）",
        len(paths) >= 4 and all(p.startswith("/") for p in paths), (len(paths), paths))
finally:
    zlib._proxy_url = _orig_proxy_url
    SRV.shutdown()
    shutil.rmtree(TMP, ignore_errors=True)

print()
if FAIL:
    print("/api/zlib 离线走查有 %d 处不对：%s" % (len(FAIL), "、".join(FAIL)))
else:
    print("/api/zlib：状态 / 登录 / 搜书 / 下载入库 / 登出 / 坏输入，整条都对，"
          "且下回来的书真的进了「本地书架」。")
sys.exit(len(FAIL))
