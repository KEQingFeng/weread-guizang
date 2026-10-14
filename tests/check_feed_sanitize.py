# -*- coding: utf-8 -*-
"""订阅正文清洗（feed.sanitize_html）的离线验收：只许留可读内容，不许留可执行的口子。

订阅是「同一批源反复灌进来」，正文 HTML 直接进阅读器渲染，所以这批用例盯的是
**浏览器认、而过滤没认**的那几种写法 —— 它们看上去像没生效，实际点一下就执行：

  A `/` 当属性分隔符：`<a/href="javascript:…">`、`<img src=x /onerror=…>`。
    属性正则原来只认 `\\s` 边界，这类写法的 href / onerror 整个漏在外头。
  B 字符引用包起来的协议：`java&#115;cript:`、`jav&#9;ascript:`、`javascript&colon;`。
    浏览器先把 `&#…;` 解回来再当 URL 用，只在原文里找「javascript」会漏。
  C 没配对的 `<script src=…>`：成对的整块会被删，落单的开口标签仍会执行、还会吞正文。
  D 两份「正当内容」必须原样活着：相对链接补绝对、data:image 放行、引号里的 `/src=`
    是属性值的一部分、正文里普通的 `/src=1` 是可读文字 —— 加固不能把它们一起改坏。

不联网、不开浏览器、不写盘。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import feed  # noqa: E402

BASE = "https://ex.com/p/"
FAIL = []
PASSED = 0


def chk(name, ok, extra=""):
    global PASSED
    print(("  ok  " if ok else " FAIL ") + name + ("" if ok else "  <- " + repr(extra)))
    if ok:
        PASSED += 1
    else:
        FAIL.append(name)


def clean(raw, base=BASE):
    return feed.sanitize_html(raw, base)


# ── A `/` 当属性分隔符 ────────────────────────────────────────────────
a1 = clean('<a/href="javascript:alert(1)">点我</a>')
chk("A1 `<a/href=…>` 的 javascript: 被换成 #", 'href="#"' in a1 and "javascript:" not in a1.lower(), a1)

a2 = clean('<a/href=javascript:alert(1)>点我</a>')
chk("A2 同一个写法不带头引号也挡住", 'href="#"' in a2 and "javascript:" not in a2.lower(), a2)

a3 = clean("<img src=x /onerror=alert(1)>")
chk("A3 `<img src=x /onerror=…>` 的 onerror 被删",
    "onerror" not in a3.lower() and "alert(1)" not in a3.lower(), a3)

a4 = clean("<img/src=x/onerror=alert(1)>")
chk("A4 `<img/src=x/onerror=…>` 两条口子一起堵住",
    "onerror" not in a4.lower() and "javascript:" not in a4.lower(), a4)

a5 = clean("<a /onclick=alert(1)>点我</a>")
chk("A5 `<a /onclick=…>` 的事件属性被删", "onclick" not in a5.lower(), a5)

a6 = clean('<a href="/rel">x</a>')
chk("A6 斜杠分隔的正当相对链接仍补成绝对", a6 == '<a href="https://ex.com/rel">x</a>', a6)

# ── B 字符引用包起来的协议 ────────────────────────────────────────────
for tag, body in (
        ("B1 十进制 `java&#115;cript:`", '<a href="java&#115;cript:alert(1)">x</a>'),
        ("B2 十六进制 `java&#x73;cript:`", '<a href="java&#x73;cript:alert(1)">x</a>'),
        ("B3 夹了制表符 `jav&#9;ascript:`", '<a href="jav&#9;ascript:alert(1)">x</a>'),
        ("B4 实体冒号 `javascript&colon;`", '<a href="javascript&colon;alert(1)">x</a>'),
        ("B5 实体包住首字母 `&#106;avascript:`", '<a href="&#106;avascript:alert(1)">x</a>'),
):
    got = clean(body)
    chk(tag + " 照样被挡住", 'href="#"' in got, got)

# base 为空也不能漏（解析时 base 由源地址补，但不该赌它永远非空）
for name, body in (
        ("B6 base 为空：实体协议仍挡住", '<a href="java&#115;cript:alert(1)">x</a>'),
        ("B7 base 为空：斜杠分隔的 onerror 仍挡住", "<img src=x /onerror=alert(1)>"),
):
    got = feed.sanitize_html(body)
    bad = "javascript" in got.lower() or "onerror" in got.lower() or "alert(1)" in got
    chk(name, not bad, got)

# ── C 没配对的 script ─────────────────────────────────────────────────
c1 = clean("<script>bad()</script>后面还有正文")
chk("C1 配对的 <script> 整块删掉、正文留着", "script" not in c1.lower() and "后面还有正文" in c1, c1)

c2 = clean("<script src=//evil.example/x.js>")
chk("C2 没配对的 <script src=…> 被删", "script" not in c2.lower(), c2)

c3 = clean("留意 <script src=//evil.example/x.js> 这种写法")
chk("C3 落单的开口 script 删了、门外的字还在", "script" not in c3.lower() and "这种写法" in c3, c3)

c4 = clean("<iframe src=//evil.example/></iframe>正文")
chk("C4 iframe 也一并清掉", "iframe" not in c4.lower() and "正文" in c4, c4)

# ── D 正当内容不许被改坏 ─────────────────────────────────────────────
d1 = clean('<a title="a/src=b" href="/x">t</a>')
chk("D1 引号里的 /src= 是属性值，原样留着",
    'title="a/src=b"' in d1 and 'href="https://ex.com/x"' in d1, d1)

d2 = clean("正文里的 https://ex.com/a/src=1 是可读文字，不该被动")
chk("D2 正文里的 /src= 不被当属性", d2 == "正文里的 https://ex.com/a/src=1 是可读文字，不该被动", d2)

d3 = clean('<img src="data:image/png;base64,AAA">')
chk("D3 data:image 仍放行", 'src="data:image/png;base64,AAA"' in d3, d3)

d4 = clean('<img src="data:text/html,<script>x</script>">')
chk("D4 data:text/html 仍拒收", "data:text/html" not in d4, d4)

d5 = clean("<br/>换行前的自闭合斜杠不该被改")
chk("D5 <br/> 保持自闭合写法", "<br/>" in d5, d5)

d6 = clean("纯正文没有任何标签")
chk("D6 没有标签时原样返回", d6 == "纯正文没有任何标签", d6)

d7 = clean('<a href="x>y" onclick="a">t</a>')
chk("D7 引号里的 > 不截断标签，onclick 照样被删",
    "onclick" not in d7.lower() and "x>y" in d7, d7)

d8 = clean("")
chk("D8 空串回空串", d8 == "", repr(d8))

print()
print("订阅正文清洗：通过 %d 项，失败 %d 项" % (PASSED, len(FAIL)))
if FAIL:
    for f in FAIL:
        print("  ✗ " + f)
    sys.exit(1)
print("ok  浏览器认的几种属性写法与实体协议都被挡住，正当内容原样保留")
