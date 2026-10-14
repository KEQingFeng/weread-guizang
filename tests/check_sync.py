# -*- coding: utf-8 -*-
"""云同步（sync.py）的离线取证：配置怎么合、密码会不会回显、路径怎么拼、传输怎么收尾。

为什么单独一份：sync.py 是本仓唯一一条「把用户文件写到本机之外」的通路，
写错方向不是界面难看，是别人的笔记被覆盖 / 丢失。它此前一条测试都没有 ——
下面这几条不需要网络，全部靠一个记账的假 `_http` 顶掉真实请求，于是
「发出去的是哪个 URL、算成功还是失败」都当场断言得出来。

重点钉住的一条是真机上会犯的错：WebDAV 建目录拼出 `…/files/u//guizang`
（base 后面补一个斜杠，路径自己再带一个），而 PUT 落在 `…/files/u/guizang/…`。
双斜杠多数服务端不当同一个目录，于是「测一下」报建目录失败，上传的文件
又落在另一个位置。所以这里不光验拼出来的 URL 逐级正确，还核对 MKCOL 的
每一级与 PUT/GET 用的前缀是同一个。
"""
import base64
import io
import json
import os
import shutil
import sys
import tempfile
import urllib.error
import urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import sync as S  # noqa: E402

FAIL = []


def chk(name, cond, extra=""):
    print(("PASS  " if cond else "FAIL  ") + name + ("" if cond else "   " + repr(extra)))
    if not cond:
        FAIL.append(name)


ORIG_HTTP = S._http
BASE = "https://dav.example.com/remote.php/dav/files/u"


class FakeResponse:
    def __init__(self, data=b""):
        self._data = data

    def read(self, *_a):
        return self._data

    def __enter__(self):
        return self

    def __exit__(self, *_a):
        return False


class Transport:
    """记账的假 HTTP：不联网，记下每次 (方法, URL, 请求头)，可预置远端文件与错误。"""

    def __init__(self):
        self.calls = []
        self.remote = {}          # url -> bytes（GET/PUT 命中时用它）
        self.reply = {}           # url -> bytes（POST 这类「回一段 JSON」用它）
        self.raise_on = {}        # url -> (code, body)

    def __call__(self, method, url, data=None, headers=None, timeout=60):
        self.calls.append((method, url, headers or {}))
        if url in self.raise_on:
            code, body = self.raise_on[url]
            raise urllib.error.HTTPError(url, code, "err", None, io.BytesIO(body or b""))
        if url in self.reply:
            return FakeResponse(self.reply[url])
        if method == "MKCOL":
            return FakeResponse()
        if method == "PUT":
            self.remote[url] = data
            return FakeResponse()
        if method == "GET":
            if url in self.remote:
                return FakeResponse(self.remote[url])
            raise urllib.error.HTTPError(url, 404, "Not Found", None, io.BytesIO(b""))
        return FakeResponse()

    def urls(self, method=None):
        return [u for m, u, _h in self.calls if method is None or m == method]

    def headers_for(self, method):
        for m, _u, h in self.calls:
            if m == method:
                return h
        return {}

    def clear(self):
        self.calls = []


def dav_cfg(**what):
    w = {"time": True, "records": False, "books": False}
    w.update(what)
    return S._norm({"enabled": True, "provider": "webdav", "what": w,
                    "webdav": {"url": BASE, "user": "u", "pass": "p"}})


# ─────────────────────────── 配置归一 ───────────────────────────
n = S._norm(None)
chk("_norm：空配置补全默认（关着 / 三样默认值 / 上次记录归零）",
    n["enabled"] is False and n["provider"] == ""
    and n["what"] == {"time": True, "records": True, "books": False}
    and n["last"] == {"at": 0, "msg": ""}, n)

n = S._norm("坏形状")
chk("_norm：不是 dict 的配置不炸，照默认走", n["provider"] == "" and n["what"]["time"] is True, n)

n = S._norm({"enabled": 1, "provider": 7,
             "webdav": {"url": "  https://dav.example.com/x/  ", "user": 12, "pass": None},
             "onedrive": {"access_exp": "not-a-number"},
             "what": {"books": True, "time": 0},
             "last": {"at": "12", "msg": "x" * 300}})
chk("_norm：地址去空白与尾斜杠、非字符串字段退回空",
    n["webdav"]["url"] == "https://dav.example.com/x" and n["webdav"]["user"] == ""
    and n["webdav"]["pass"] == "", n["webdav"])
chk("_norm：what 只认三把开关，缺的保持默认",
    n["what"] == {"time": False, "records": True, "books": True}, n["what"])
chk("_norm：last.at 取整、last.msg 夹到 200 字",
    n["last"]["at"] == 12 and len(n["last"]["msg"]) == 200, n["last"])
chk("_norm：坏数字退 0 而不是抛异常", n["onedrive"]["access_exp"] == 0, n["onedrive"])

# ─────────────────────────── 回给界面的一份 ───────────────────────────
st = S.state({"enabled": True, "provider": "webdav",
              "webdav": {"url": BASE, "user": "u", "pass": "topsecret9"},
              "onedrive": {"client_id": "client-abc", "refresh_token": "refresh-xyz"}})
blob = json.dumps(st, ensure_ascii=False)
chk("state：密码与令牌只回「填没填」，绝不回值",
    "topsecret9" not in blob and "refresh-xyz" not in blob
    and st["webdav"]["pass_set"] is True and st["onedrive"]["authed"] is True
    and st["onedrive"]["client_set"] is True, st)

# ─────────────────────────── 保存合并 ───────────────────────────
old = {"enabled": True, "provider": "webdav",
       "webdav": {"url": BASE, "user": "u", "pass": "oldpass1"}}
ok, msg, c = S.save(old, {"enabled": True, "provider": "webdav", "webdav": {"pass": ""}})
chk("save：口令留空＝不改（只动别的字段不被逼重输密码）",
    ok and c["webdav"]["pass"] == "oldpass1", (ok, msg, c["webdav"]))

ok, msg, _ = S.save(old, {"enabled": True, "provider": ""})
chk("save：开着同步却把服务选空 → 拦下并说清原因",
    not ok and "云服务" in msg, (ok, msg))

ok, msg, c = S.save({}, {"enabled": True, "provider": "ftp"})
chk("save：不认的服务名一律归空，不给「配了个不认识的」留后门",
    not ok and c["provider"] == "" and "云服务" in msg, (ok, msg, c["provider"]))

ok, msg, _ = S.save({}, {"enabled": True, "provider": "webdav"})
chk("save：WebDAV 缺地址 → 拦下", not ok and "地址" in msg, (ok, msg))

ok, msg, _ = S.save({}, {"enabled": True, "provider": "webdav",
                         "webdav": {"url": "dav.example.com/dav"}})
chk("save：WebDAV 地址不以 http(s) 开头 → 拦下",
    not ok and "http" in msg, (ok, msg))

ok, msg, _ = S.save({}, {"enabled": True, "provider": "onedrive"})
chk("save：OneDrive 缺应用 ID → 拦下", not ok and "应用 ID" in msg, (ok, msg))

ok, msg, c = S.save({}, {"provider": "onedrive", "onedrive": {"client_id": "cid"},
                         "enabled": False})
chk("save：没开同步时不校验，但填进去的项要留下",
    ok and c["provider"] == "onedrive" and c["onedrive"]["client_id"] == "cid", (ok, c))

ok, msg, c = S.save({"what": {"time": False}}, {"what": {"books": True}})
chk("save：三把开关各自合并（没提的那把保持原样）",
    c["what"] == {"time": False, "records": True, "books": True}, c["what"])

chk("_basic：用户名密码按 UTF-8 打包成 Basic 头",
    S._basic("u", "p") == "Basic " + base64.b64encode(b"u:p").decode("ascii"))

chk("_remote_dir：取上一级，根目录返回空串",
    S._remote_dir("guizang/cache/x.json") == "guizang/cache"
    and S._remote_dir("guizang") == "" and S._remote_dir("a/b") == "a")

# ─────────────────────────── 要同步哪些文件 ───────────────────────────
TMP = tempfile.mkdtemp(prefix="guizang-sync-")
CACHE = os.path.join(TMP, "cache")
BOOKS = os.path.join(TMP, "books")
os.makedirs(CACHE)
os.makedirs(os.path.join(BOOKS, "bk1", "images"))


def put(path, text):
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


put(os.path.join(CACHE, "readstat.json"), "{}")
put(os.path.join(CACHE, "library.json"), "{}")
put(os.path.join(CACHE, "notes_index.json"), "{}")
put(os.path.join(BOOKS, "bk1", "chapters.md"), "hi")
put(os.path.join(BOOKS, "bk1", "images", "a.png"), "png")

pairs = dict(S._collect(CACHE, BOOKS, {"time": True, "records": False, "books": False}))
chk("_collect：只勾时长就只列 readstat.json",
    list(pairs) == ["guizang/cache/readstat.json"], pairs)
chk("_collect：本地路径按 cache 目录拼绝对路径",
    pairs.get("guizang/cache/readstat.json") == os.path.join(CACHE, "readstat.json"), pairs)

pairs = dict(S._collect(CACHE, BOOKS, {"time": False, "records": True, "books": False}))
chk("_collect：记录＝书架账本 + 划线索引两件",
    set(pairs) == {"guizang/cache/library.json", "guizang/cache/notes_index.json"}, pairs)

pairs = dict(S._collect(CACHE, BOOKS, {"time": False, "records": False, "books": True}))
chk("_collect：书库文件按子目录铺开、远端一律正斜杠",
    set(pairs) == {"guizang/books/bk1/chapters.md", "guizang/books/bk1/images/a.png"}
    and all("\\" not in k for k in pairs), pairs)
pairs = S._collect(CACHE, BOOKS, {"time": True, "records": True, "books": False})
chk("_collect：没勾书库时不列书库文件（小账本 3 件）",
    set(dict(pairs)) == {"guizang/cache/readstat.json", "guizang/cache/library.json",
                         "guizang/cache/notes_index.json"}, dict(pairs))

# ─────────────────────────── WebDAV：URL 拼法与报错 ───────────────────────────
tr = Transport()
S._http = tr
c = dav_cfg()
S._dav_mkdir(c, "guizang/cache")
mkdirs = tr.urls("MKCOL")
chk("_dav_mkdir：逐级建目录，第一级是 base/guizang（不许双斜杠）",
    mkdirs == [BASE + "/guizang", BASE + "/guizang/cache"], mkdirs)
chk("_dav_mkdir：每一级路径里都不出现连续斜杠",
    all(urllib.parse.urlparse(u).path.count("//") == 0 for u in mkdirs), mkdirs)
chk("_dav_mkdir：建目录的前缀与 PUT 用的前缀是同一个（否则文件落在别处）",
    (BASE + "/" + "guizang/cache/readstat.json").startswith(mkdirs[-1] + "/"), mkdirs)

tr.clear()
S._dav_test(c)
chk("_dav_test：只建远端根，地址同样不带双斜杠",
    tr.urls("MKCOL") == [BASE + "/" + S.REMOTE_ROOT], tr.urls("MKCOL"))

tr = Transport()
S._http = tr
tr.raise_on[BASE + "/guizang"] = (405, b"")
try:
    S._dav_mkdir(c, "guizang")
    chk("_dav_mkdir：405「目录已存在」当通过，不当错", True)
except Exception as e:
    chk("_dav_mkdir：405「目录已存在」当通过，不当错", False, e)

tr = Transport()
S._http = tr
tr.raise_on[BASE + "/guizang"] = (401, b"")
try:
    S._dav_mkdir(c, "guizang")
    chk("_dav_mkdir：401 说人话（用户名或密码不对）", False, "没抛")
except RuntimeError as e:
    chk("_dav_mkdir：401 说人话（用户名或密码不对）", "密码" in str(e), str(e))

tr = Transport()
S._http = tr
tr.raise_on[BASE + "/guizang"] = (409, b"")
try:
    S._dav_mkdir(c, "guizang")
    chk("_dav_mkdir：409 等别的错照实报，不吞", False, "没抛")
except RuntimeError as e:
    chk("_dav_mkdir：409 等别的错照实报，不吞", "409" in str(e), str(e))

# ─────────────────────────── WebDAV：跑一趟 ───────────────────────────
readstat = os.path.join(CACHE, "readstat.json")
tr = Transport()
S._http = tr
ok, msg = S.run(c, CACHE, BOOKS)
chk("run：本地有、远端没有 → 上传，且 PUT 落在 MKCOL 建出来的目录里",
    ok and "上传 1" in msg
    and tr.urls("PUT") == [BASE + "/guizang/cache/readstat.json"], (ok, msg, tr.urls("PUT")))

os.remove(readstat)
tr = Transport()
S._http = tr
tr.remote[BASE + "/guizang/cache/readstat.json"] = b'{"from":"cloud"}'
ok, msg = S.run(c, CACHE, BOOKS)
chk("run：本地没有、远端有 → 取回来落到本机",
    ok and "取回 1" in msg and open(readstat, "rb").read() == b'{"from":"cloud"}', (ok, msg))

tr = Transport()
S._http = tr
os.remove(readstat)
ok, msg = S.run(c, CACHE, BOOKS)
chk("run：两边都没有 → 不算失败（404 是「还没有」，不是错）",
    ok and msg == "没有变化", (ok, msg))

tr = Transport()
S._http = tr
tr.raise_on[BASE + "/guizang/cache/readstat.json"] = (500, b"")
ok, msg = S.run(c, CACHE, BOOKS)
chk("run：真出错才算失败，并在汇总里带上第一条原因",
    not ok and "失败 1" in msg and "500" in msg, (ok, msg))

ok, msg = S.run(S._norm({}), CACHE, BOOKS)
chk("run：没开同步时直接回人话，不发任何请求", not ok and "还没开云同步" in msg, (ok, msg))

# ─────────────────────────── OneDrive：同一条路 ───────────────────────────
od = S._norm({"enabled": True, "provider": "onedrive",
              "what": {"time": True, "records": False, "books": False},
              "onedrive": {"client_id": "cid", "access_token": "tok",
                           "access_exp": S._now() + 3600}})
put(readstat, "{}")
tr = Transport()
S._http = tr
ok, msg = S.run(od, CACHE, BOOKS)
chk("run/OneDrive：本地有就按 Graph 路径上传，凭证不过期时不换令牌",
    ok and "上传 1" in msg and len(tr.urls("PUT")) == 1
    and tr.urls("PUT")[0].startswith(S.GRAPH + "/me/drive/special/approot:/guizang/cache/"),
    (ok, msg, tr.urls("PUT")))
chk("run/OneDrive：授权头带的是配置里的 access_token",
    tr.headers_for("PUT").get("Authorization") == "Bearer tok",
    tr.headers_for("PUT"))

# ─────────────────────────── 设备码授权 ───────────────────────────
dstart = Transport()
S._http = dstart
dstart.raise_on[S.MS_DEVICECODE] = (400, b'{"error":"bad"}')
ok, msg, info = S.device_start({"onedrive": {"client_id": ""}})
chk("device_start：没填应用 ID 时不发请求，直接回人话",
    not ok and info is None and dstart.calls == [], (ok, msg, dstart.calls))

for err, expect in (("authorization_pending", "pending"),
                    ("slow_down", "pending"),
                    ("expired_token", "expired")):
    tr = Transport()
    S._http = tr
    tr.raise_on[S.MS_TOKEN] = (400, json.dumps({"error": err}).encode())
    status, _msg, _upd = S.device_poll({"onedrive": {"client_id": "cid"}}, "dev")
    chk("device_poll：%s → %s（没点确认不该报错）" % (err, expect), status == expect, status)

tr = Transport()
S._http = tr
tr.reply[S.MS_TOKEN] = json.dumps(
    {"refresh_token": "rt", "access_token": "at", "expires_in": 3600}).encode()
status, _msg, upd = S.device_poll({"onedrive": {"client_id": "cid"}}, "dev")
chk("device_poll：拿到令牌就算成，并把刷新令牌写回配置（下次不用再授权）",
    status == "ok" and upd["onedrive"]["refresh_token"] == "rt"
    and upd["onedrive"]["access_token"] == "at", (status, upd["onedrive"]))

S._http = ORIG_HTTP
shutil.rmtree(TMP, ignore_errors=True)
print()
print("云同步体检：" + ("全部通过" if not FAIL else "%d 项未过：%s" % (len(FAIL), FAIL)))
sys.exit(1 if FAIL else 0)
