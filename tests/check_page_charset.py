# -*- coding: utf-8 -*-
"""外站声明的字符集不可信：一个写错的 charset 不许把「取网页」这条路打成异常。

`Content-Type: text/html; charset=…` 里那个名字来自对方站点。`bytes.decode` 除了
「字节解不出来」（UnicodeDecodeError，`errors="replace"` 能兜住）之外，还会因为
**编码名本身不存在**抛 LookupError。实测有页面写 `charset=x-unknown`；`base64` /
`hex` / `rot13` 这类名字 `codecs.lookup` 认得、却不是文本编码，`bytes.decode` 一样拒收。
空字节的探测又会因为短路而放过任何名字，所以判据必须是「真解一个字节」。

原来的两处都是 `blob.decode(charset or "utf-8", errors="replace")`，于是一个手滑的
charset 就能：
  · 剪藏（clip_article.fetch）冒出一句运行期 LookupError，而不是带人话的失败；
  · 内置浏览器（web_browse.shape → browse）在 try 之外炸开，这一屏直接 500。
两条路现在都过 clip_article.safe_charset 收口。这一套纯离线：真机夹具用一个假的
urlopen / opener 顶掉，一次网络都不出。

跑法：.venv/bin/python tests/check_page_charset.py
"""
import os
import pathlib
import sys
import tempfile

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

# 数据目录先指进沙盒再 import：模块级会定下 DATA_DIR，设晚了就写到真实 cache 去了。
ROOT = tempfile.mkdtemp(prefix="guizang-charset-")
os.environ["GUIZANG_DATA"] = os.path.join(ROOT, "cache")
os.environ["GUIZANG_BOOKS"] = os.path.join(ROOT, "books")

import clip_article as ca  # noqa: E402
import web_browse as wb  # noqa: E402

checks = []


def chk(name, ok, extra=""):
    checks.append((bool(ok), name, "" if ok else str(extra)[:300]))
    print(("  ok  " if ok else " FAIL ") + name + ("" if ok else "  <- " + str(extra)[:300]))


# ── A safe_charset：真编码名放行、坏名字与伪编码退回 utf-8 ──────────
chk("正常的 utf-8 原样留下", ca.safe_charset("utf-8") == "utf-8")
chk("GBK 原样留下（大小写归一）", ca.safe_charset("GBK") == "gbk")
chk("gb18030 / big5 / cp936 都认得",
    ca.safe_charset("gb18030") == "gb18030"
    and ca.safe_charset("big5") == "big5" and ca.safe_charset("cp936") == "cp936")
chk("utf-16 是文本编码（单字节探测会 UnicodeDecodeError，不算坏名字）",
    ca.safe_charset("utf-16") == "utf-16")
chk("不存在的名字退回 utf-8", ca.safe_charset("x-unknown") == "utf-8")
chk("不是文本编码的名字（base64 / hex / rot13 / zlib）退回 utf-8",
    ca.safe_charset("base64") == "utf-8" and ca.safe_charset("hex") == "utf-8"
    and ca.safe_charset("rot13") == "utf-8" and ca.safe_charset("zlib") == "utf-8")
chk("空 / None / 纯空白都是 utf-8",
    ca.safe_charset("") == "utf-8" and ca.safe_charset(None) == "utf-8"
    and ca.safe_charset("   ") == "utf-8")
chk("名字里夹引号 / 分号 / 换行的（响应头注入形状）退回 utf-8",
    ca.safe_charset('utf-8"; x="y') == "utf-8"
    and ca.safe_charset("a\r\nX-Evil: 1") == "utf-8")
chk("调用方给了 fallback 就用 fallback", ca.safe_charset("x-unknown", fallback="gbk") == "gbk")
chk("真名字优先于 fallback", ca.safe_charset("big5", fallback="gbk") == "big5")
chk("fallback 本身也不认识时兜回 utf-8",
    ca.safe_charset("x-unknown", fallback="not-a-charset") == "utf-8")

# ── B 镜子：不换掉坏名字，原来那一句确实会炸 ────────────────────────
try:
    b"<html>x</html>".decode("x-unknown", errors="replace")
    _old_raises = False
except LookupError:
    _old_raises = True
chk("镜子：直接 decode(x-unknown) 抛的是 LookupError（errors=replace 兜不住）", _old_raises)


# ── C clip_article.fetch：坏 charset 不抛，好 charset 照旧解对 ──────
class _FakeHeaders:
    def __init__(self, charset):
        self._charset = charset

    def get_content_charset(self):
        return self._charset


class _FakeResp:
    def __init__(self, blob, charset, url):
        self._blob, self._url = blob, url
        self.headers = _FakeHeaders(charset)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def geturl(self):
        return self._url

    def read(self, n=-1):
        return self._blob


def _with_fake_urlopen(blob, charset, url):
    """把 urlopen 换成固定回一份字节的假响应，跑完还原。"""
    def _open(req, timeout=None):
        return _FakeResp(blob, charset, url)

    real = ca.urllib.request.urlopen
    ca.urllib.request.urlopen = _open
    try:
        return ca.fetch("https://example.com/post")
    finally:
        ca.urllib.request.urlopen = real


body = "<html><body>你好，世界</body></html>"

try:
    raw, final = _with_fake_urlopen(body.encode("utf-8"), "x-unknown",
                                    "https://example.com/post")
    _fetched = ("你好" in raw, final)
except Exception as e:                                    # 旧代码在这里抛 LookupError
    _fetched = ("%s: %s" % (type(e).__name__, e), "")
chk("未知 charset：fetch 不抛，正文按 utf-8 解出来",
    _fetched[0] is True and _fetched[1] == "https://example.com/post", _fetched)

raw_gbk, _ = _with_fake_urlopen("<p>中文段落</p>".encode("gbk"), "gbk",
                                "https://example.com/gbk")
chk("真 GBK 页面照旧按 GBK 解（别把好的也退回 utf-8）",
    raw_gbk == "<p>中文段落</p>", raw_gbk[:40])

raw_rep, _ = _with_fake_urlopen(b"<p>caf\xe9</p>", "x-unknown", "https://example.com/latin")
chk("未知 charset 且字节不是 utf-8：替换字符兜底，不抛", "\ufffd" in raw_rep, repr(raw_rep))


# ── D web_browse.shape：坏名字不再炸，回显只带验过的名字 ─────────────
r = wb.shape(body.encode("utf-8"), "text/html", "x-unknown", "https://e.cn/p")
chk("未知 charset 的 HTML：ok 且正文完整转成 UTF-8",
    r["ok"] and "你好" in r["body"].decode("utf-8"), r.get("body", b"")[:60])

r2 = wb.shape(b"plain", "text/plain", "x-unknown", "https://e.cn/a.txt")
chk("非网页文本：未知 charset 回显成验过的 utf-8", r2["ctype"] == "text/plain; charset=utf-8",
    r2["ctype"])

r3 = wb.shape(b"plain", "text/plain", "gbk", "https://e.cn/a.txt")
chk("非网页文本：真 charset 照旧回显", r3["ctype"] == "text/plain; charset=gbk", r3["ctype"])

r4 = wb.shape(b"plain", "text/plain", "a\r\nX-Evil: 1", "https://e.cn/a.txt")
chk("非网页文本：带换行的 charset 不许注入响应头",
    "\r" not in r4["ctype"] and "\n" not in r4["ctype"], r4["ctype"])


# ── E web_browse.browse：整条路走通，坏 charset 换来的是一页、不是异常 ─
class _BrowseHeaders:
    def __init__(self, ctype, charset):
        self._ctype, self._charset = ctype, charset

    def get_content_type(self):
        return self._ctype

    def get_content_charset(self):
        return self._charset


class _BrowseResp:
    def __init__(self, blob, ctype, charset, url):
        self._blob, self._url = blob, url
        self.headers = _BrowseHeaders(ctype, charset)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def geturl(self):
        return self._url

    def read(self, n=-1):
        return self._blob


class _FakeOpener:
    def __init__(self, resp):
        self._resp = resp

    def open(self, *a, **k):
        return self._resp


real_build = wb.urllib.request.build_opener
wb.urllib.request.build_opener = lambda *a, **k: _FakeOpener(
    _BrowseResp(body.encode("utf-8"), "text/html", "x-unknown", "https://e.cn/p"))
try:
    r5 = wb.browse("https://e.cn/p")
finally:
    wb.urllib.request.build_opener = real_build
chk("browse：未知 charset 也回一页正常的（旧代码在这里炸成 500）",
    r5.get("ok") and "你好" in r5["body"].decode("utf-8"), r5.get("url"))


# ── F 接线：两条取网页的路都真的走了这一把尺子 ───────────────────────
CLIP = (REPO / "clip_article.py").read_text(encoding="utf-8")
BROWSE = (REPO / "web_browse.py").read_text(encoding="utf-8")
chk("剪藏那条 decode 走 safe_charset", 'blob.decode(safe_charset(charset)' in CLIP)
chk("内置浏览器那条 decode 走 clip_article.safe_charset",
    "blob.decode(clip_article.safe_charset(charset)" in BROWSE)
chk("两处都不再直接用未验的名字拼编码",
    'charset or "utf-8"' not in CLIP and '%s; charset=%s" % (out, charset)' not in BROWSE)

bad = [c for c in checks if not c[0]]
print("\n取网页字符集体检：%d 项通过，失败 %d 项" % (len(checks) - len(bad), len(bad)))
for _, n, x in bad:
    print("  ✗ " + n + ("  | " + x if x else ""))
sys.exit(1 if bad else 0)
