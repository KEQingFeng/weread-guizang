# -*- coding: utf-8 -*-
"""外站声明的字符集不可信（第三条取页路）：X 的 syndication JSON 也要过同一把尺子。

tests/check_page_charset.py 钉住了剪藏（clip_article.fetch）与内置浏览器（web_browse）
两条路，两条现在都过 `clip_article.safe_charset` 收口。但平台解析（web_parse）里取 X
的那条路走的是自己的 `fetch_json`，绕开了 clip_article.fetch（后者有一条「里面必须有
`<`，否则不像网页」的检查，会把正常回纯 JSON 的 syndication 接口一票否决）—— 于是它
那份 `charset or "utf-8"` 一直没被尺子量过。

坏名声（实测有页面写 `charset=x-unknown`）或压根不是文本编码的名字（`base64` / `hex`）
让 `bytes.decode` 抛的是 **LookupError**，`errors="replace"` 管字节不管名字。落到用户
身上：内置剪藏（platform_of 认成 x）本该回一句人话，却把 `'x-unknown' is not a valid
encoding` 这种运行期错误原样递出去 —— 上层那句「抓取失败全是 ValueError 带的人话」
当场破了。

这一套纯离线：真机夹具用一个假的 urlopen 顶掉网络，一次请求都不出。

跑法：.venv/bin/python tests/check_charset_json.py
"""
import os
import pathlib
import re
import sys
import tempfile

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

# 数据目录先指进沙盒再 import：模块级会定下 DATA_DIR，设晚了就写到真实 cache 去了。
ROOT = tempfile.mkdtemp(prefix="guizang-charsetjson-")
os.environ["GUIZANG_DATA"] = os.path.join(ROOT, "cache")
os.environ["GUIZANG_BOOKS"] = os.path.join(ROOT, "books")

import clip_article as ca  # noqa: E402
import web_parse  # noqa: E402

checks = []


def chk(name, ok, extra=""):
    checks.append((bool(ok), name, "" if ok else str(extra)[:300]))
    print(("  ok  " if ok else " FAIL ") + name + ("" if ok else "  <- " + str(extra)[:300]))


# ── A 镜子：不换掉坏名字，原来那一句确实会炸 ────────────────────────
try:
    b'{"ok":1}'.decode("x-unknown", errors="replace")
    _old_raises = False
except LookupError:
    _old_raises = True
chk("镜子：decode(x-unknown) 抛 LookupError（errors=replace 兜不住）", _old_raises)


# ── B web_parse.fetch_json：坏 charset 不抛，好 charset 照旧解对 ─────
class _FakeHeaders:
    def __init__(self, charset):
        self._charset = charset

    def get_content_charset(self):
        return self._charset


class _FakeResp:
    def __init__(self, blob, charset):
        self._blob, self._charset = blob, charset
        self.headers = _FakeHeaders(charset)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def geturl(self):
        return "https://cdn.syndication.twimg.com/tweet-result?id=1"

    def read(self, n=-1):
        return self._blob


def _with_fake_urlopen(blob, charset):
    """把 urlopen 换成固定回一份字节的假响应，跑完还原；返回 fetch_json 的结果。"""
    def _open(req, timeout=None):
        return _FakeResp(blob, charset)

    real = ca.urllib.request.urlopen
    ca.urllib.request.urlopen = _open
    try:
        return web_parse.fetch_json("https://cdn.syndication.twimg.com/tweet-result?id=1")
    finally:
        ca.urllib.request.urlopen = real


_json = '{"text":"你好，X"}'.encode("utf-8")
try:
    raw, final = _with_fake_urlopen(_json, "x-unknown")
    _got = ("你好" in raw, "syn=twimg.com" in final)
except Exception as e:                                    # 旧代码在这里抛 LookupError
    _got = ("%s: %s" % (type(e).__name__, e), "")
chk("未知 charset：fetch_json 不抛，正文按 utf-8 解出来", _got[0] is True, _got)

try:
    raw_none, _ = _with_fake_urlopen(_json, None)
    _got_none = "你好" in raw_none
except Exception as e:
    _got_none = "%s: %s" % (type(e).__name__, e)
chk("没有 charset 声明：照旧按 utf-8 解", _got_none is True, _got_none)

try:
    raw_gbk, _ = _with_fake_urlopen('{"text":"中文段落"}'.encode("gbk"), "gbk")
    _got_gbk = raw_gbk
except Exception as e:
    _got_gbk = "%s: %s" % (type(e).__name__, e)
chk("真 GBK 照旧按 GBK 解（别把好的也退回 utf-8）",
    _got_gbk == '{"text":"中文段落"}', repr(_got_gbk)[:120])

try:
    raw_rep, _ = _with_fake_urlopen(b'{"t":"caf\xe9"}', "x-unknown")
    _got_rep = raw_rep
except Exception as e:
    _got_rep = "%s: %s" % (type(e).__name__, e)
chk("未知 charset 且字节不是 utf-8：替换字符兜底，不抛",
    isinstance(_got_rep, str) and "\ufffd" in _got_rep, repr(_got_rep)[:120])


# ── C 端到端：X 整条 extract 遇到坏 charset 也回得出正文 ──────────────
# 只换掉 urlopen（网络那一层），fetch_json 用的是真的 —— 这样验的正是它那句 decode。
X_TWEET_ID = "1712749080000000000"
X_URL = "https://x.com/demo_user/status/" + X_TWEET_ID
_x_json = ('{"text":"这是一条用来验证坏 charset 不影响 X 解析的推文正文，长度足够。",'
           '"user":{"name":"示例作者","screen_name":"demo_user"},'
           '"created_at":"2024-01-08T04:58:00.000Z","photos":[]}').encode("utf-8")


def _with_fake_urlopen_run(fn, blob, charset):
    """urlopen 回固定字节，期间跑 fn()；跑完还原。"""
    real = ca.urllib.request.urlopen
    ca.urllib.request.urlopen = lambda req, timeout=None: _FakeResp(blob, charset)
    try:
        return fn()
    finally:
        ca.urllib.request.urlopen = real


try:
    art = _with_fake_urlopen_run(lambda: web_parse.extract(X_URL), _x_json, "x-unknown")
    _art = (art.get("title"), art.get("author"), art.get("site"))
except Exception as e:
    _art = ("%s: %s" % (type(e).__name__, e), "", "")
chk("X 端到端：坏 charset 也解析出正文（不再是 LookupError）",
    isinstance(_art[0], str) and "坏 charset" in _art[0] and _art[1] == "示例作者",
    _art)


# ── D extract_or_none：坏 charset 不该把「试一下」变成异常穿透 ────────
try:
    got = _with_fake_urlopen_run(lambda: web_parse.extract_or_none(X_URL), _x_json, "x-unknown")
    _none_ok = got is not None and got.get("author") == "示例作者"
except Exception as e:
    _none_ok = "%s: %s" % (type(e).__name__, e)
chk("extract_or_none：坏 charset 也拿得到结果（不被异常穿透）", _none_ok is True, _none_ok)


# ── E 接线：web_parse 那条 decode 真的走了 safe_charset ──────────────
WP = (REPO / "web_parse.py").read_text(encoding="utf-8")
chk("web_parse 的 decode 走 clip_article.safe_charset",
    "blob.decode(clip_article.safe_charset(charset)" in WP)
chk("web_parse 不再直接用未验的名字拼编码（无 charset or \"utf-8\"）",
    not re.search(r'charset\s+or\s+["\']utf-8["\']', WP))


bad = [c for c in checks if not c[0]]
print("\n平台解析字符集体检：%d 项通过，失败 %d 项" % (len(checks) - len(bad), len(bad)))
for _, n, x in bad:
    print("  ✗ " + n + ("  | " + x if x else ""))
sys.exit(1 if bad else 0)
