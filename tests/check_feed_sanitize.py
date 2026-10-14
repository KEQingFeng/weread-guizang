#!/usr/bin/env python3
"""归藏门禁：订阅正文的清洗（feed.sanitize_html）能不能被绕过。

它守的是一句话：**外站灌进来的正文里，可执行的东西一个都别想留在成品里。**

订阅和剪藏不一样 —— 剪藏是用户主动粘的单篇，订阅是同一批源反复灌进来，而且是
「收进书架」直接落成书的那条路。所以 sanitize_html 是这个模块的安全边界，它自己
的注释也写着「写得更硬」。但原来那把尺子只量原始那几个字节，浏览器却不是这么
解析地址的：

  · `&#106;avascript:alert(1)` —— 属性值先过一遍 HTML 实体解码，浏览器手里就是
    `javascript:...`；
  · `java\\tscript:alert(1)` —— URL 里的 TAB / 换行会被整个删掉，拼回来还是
    `javascript:`；
  · `<script>alert(1)` —— 订阅源常把正文截断，没有闭合标签，浏览器会把后面整段
    都当成脚本内容，原来那条只认「成对闭合」的正则漏掉了它。

这一份不联网、不起服务，直接对着纯函数打；跑法：

    .venv/bin/python tests/check_feed_sanitize.py
"""
import os
import pathlib
import shutil
import sys
import tempfile

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

# GUIZANG_DATA 要在 import feed 之前指到沙盒：feed 在导入时就把 DATA_DIR / STORE
# 定下来了，设晚了它还是认用户真实的数据目录。
SANDBOX = tempfile.mkdtemp(prefix="gz-feed-sanitize-")
os.environ["GUIZANG_DATA"] = os.path.join(SANDBOX, "data")
os.makedirs(os.environ["GUIZANG_DATA"], exist_ok=True)

import feed  # noqa: E402

FAILED = []
PASSED = 0


def chk(name, cond, extra=""):
    global PASSED
    if cond:
        PASSED += 1
    else:
        FAILED.append(name)
    print(("PASS  " if cond else "FAIL  ") + name + ("" if cond else "   " + repr(extra)))


BASE = "https://site.example/post/1"


def clean(raw):
    return feed.sanitize_html(raw, base=BASE)


def no_js(out):
    """清洗结果里不许再留得下可执行的协议（大小写 / 实体都算）。"""
    low = out.lower()
    return ("javascript:" not in low and "vbscript:" not in low
            and "livescript:" not in low and "data:text" not in low
            and "&#106;avascript" not in low and "&#x6a;avascript" not in low
            and "java\tscript" not in low and "java\nscript" not in low)


# ── 1. 协议判定要按浏览器的规矩来（实体 / 空白拆字 / &colon;） ────────────
for label, raw in [
    ("实体：十进制 &#106;avascript:", '<a href="&#106;avascript:alert(1)">x</a>'),
    ("实体：十六进制 &#x6a;avascript:", '<a href="&#x6a;avascript:alert(1)">x</a>'),
    ("实体：大写 &#X6A;AVASCRIPT:", '<a href="&#X6A;AVASCRIPT:alert(1)">x</a>'),
    ("实体：&colon; 冒充冒号", '<a href="javascript&colon;alert(1)">x</a>'),
    ("拆字：java<TAB>script:", '<a href="java\tscript:alert(1)">x</a>'),
    ("拆字：java<LF>script:", '<a href="java\nscript:alert(1)">x</a>'),
    ("拆字：java<CR>script:", '<a href="java\rscript:alert(1)">x</a>'),
    ("拆字：实体换行 java&#x0A;script:", '<a href="java&#x0A;script:alert(1)">x</a>'),
    ("前导空白 + 实体", '<a href=" &#106;avascript:alert(1)">x</a>'),
]:
    got = clean(raw)
    chk("拦截 " + label, '#' in got and no_js(got), got)

for label, raw in [
    ("src 里的实体 javascript:", '<img src="&#106;avascript:alert(1)">'),
    ("formaction 里的 javascript:", '<button formaction="javascript:alert(1)">x</button>'),
]:
    got = clean(raw)
    chk("拦截 " + label, "#" in got and no_js(got), got)

chk("拦截 vbscript:", no_js(clean('<a href="vbscript:msgbox(1)">x</a>')))
chk("拦截 data:text/html（含实体写法）",
    no_js(clean('<a href="data:text/html,<b>x</b>">x</a>'))
    and no_js(clean('<a href="&#100;ata:text/html,x">x</a>')))


# ── 2. 没闭合的 <script> / <style> / <iframe> 必须清到底 ────────────────
for label, raw in [
    ("未闭合 <script>", "<p>hi</p><script>alert(1)"),
    ("未闭合 <script src>", '<p>hi</p><script src="//evil.example/x.js">'),
    ("未闭合 <iframe>", '<p>hi</p><iframe src="//evil.example/x">'),
    ("未闭合 <style>", "<p>hi</p><style>body{display:none}"),
]:
    got = clean(raw)
    chk("清掉 " + label, "script" not in got.lower() and "iframe" not in got.lower()
        and "style" not in got.lower() and "evil.example" not in got and "alert" not in got,
        got)

chk("闭合标签带空格也认（</script >）",
    clean('<p>a</p><script src="x"></script ><p>b</p>') == "<p>a</p><p>b</p>",
    clean('<p>a</p><script src="x"></script ><p>b</p>'))
chk("中间那段成对的 <script> 清掉，两边的正文留住",
    clean("<p>a</p><script>bad()</script><p>b</p>") == "<p>a</p><p>b</p>",
    clean("<p>a</p><script>bad()</script><p>b</p>"))


# ── 3. 回归：正常内容一个字不许动 ─────────────────────────────────────
chk("回归：内联 on* 仍然剥掉",
    "onerror" not in clean('<img src=x onerror="alert(1)">').lower())
chk("回归：data:image 放行",
    clean('<img src="data:image/png;base64,AAAA">').count("data:image/png") == 1)
chk("回归：相对地址补成绝对、&amp; 不重复转义",
    clean('<a href="/a?b=1&amp;c=2">x</a>') ==
    '<a href="https://site.example/a?b=1&amp;c=2">x</a>',
    clean('<a href="/a?b=1&amp;c=2">x</a>'))
chk("回归：绝对地址原样留住",
    clean('<a href="https://e.com/a?b=1&amp;c=2">x</a>') ==
    '<a href="https://e.com/a?b=1&amp;c=2">x</a>')
chk("回归：页内锚点不被改写", 'href="#sec"' in clean('<a href="#sec">x</a>'))
chk("回归：正文文字留着",
    "正文" in clean("<p>正文</p><script>alert(1)"))


# ── 4. 走到「收进书架」那一层也不许留下 javascript: ───────────────────
# to_shelf 用的是 _html_to_md：它先清洗、再把 <a href> 翻成 markdown 链接。
# 清洗这一关漏了，这里就会漂出一串 `[点我](javascript:...)`。
md = feed._html_to_md('<p>正文</p><a href="&#106;avascript:alert(1)">点我</a>', BASE)
chk("集成：翻成 markdown 后没有 javascript: 链接",
    "javascript:" not in md.lower() and "javascript" not in md.lower(), md)
md2 = feed._html_to_md('<p>正文</p><script>alert(1)', BASE)
chk("集成：截断的 <script> 不会把脚本源码留在正文里",
    "alert" not in md2 and "<" not in md2, md2)


shutil.rmtree(SANDBOX, ignore_errors=True)
print("\n订阅清洗体检：%d 项通过，失败 %d 项" % (PASSED, len(FAILED)))
for f in FAILED:
    print("  ✗ " + f)
sys.exit(1 if FAILED else 0)
