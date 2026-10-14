#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""围栏代码块的语言串也是「外部内容」，往属性里塞同样得走引号那道闸。

与 check_export_safety 配成一对：那一份管图片地址 / 链接地址 / alt，这一份管
``` 后面跟着的那串语言名。它取自导入书、剪藏正文里的原文，最终落进
`<pre><code class="lang-%s">` —— 用的却还是 `_esc`（quote=False）。一个双引号
就能关掉 `class="…"`、再往标签上挂一个 onmouseover=…，导出的 HTML / EPUB /
交给铬内核打印的 PDF 里就是能跑的脚本。`_attrq` 的注释写着「凡是往属性里塞的，
都必须再过这一道」，这一处漏了。

钉住三件事：
  A 逃逸：语言串带引号时不许多出任何事件属性，且那个引号要**原样留在 class
    值里**（是转义，不是悄悄删掉）；
  B 回归：正常的 ```python 一个字不改，尖括号与 & 照旧转义，代码正文不受牵连；
  C 成品：真打一个 EPUB，包里那页 XHTML 也解析不出事件属性。

不联网、不起浏览器：解析用标准库 HTMLParser，EPUB 走临时目录。
"""
import html.parser
import os
import re
import shutil
import sys
import tempfile
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import book_export          # noqa: E402

FAIL = []
_EVENT_ATTR = re.compile(r"^on", re.I)


def chk(name, cond, extra=""):
    print(("PASS  " if cond else "FAIL  ") + name + ("" if cond else "   " + repr(extra)[:220]))
    if not cond:
        FAIL.append(name)


class _Attrs(html.parser.HTMLParser):
    """把渲染结果拆成 [(标签, {属性名: 值})]，按浏览器的方式解析。"""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tags = []

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))

    handle_startendtag = handle_starttag


def parsed(fragment):
    p = _Attrs()
    p.feed(fragment)
    return p.tags


def first(tag, fragment):
    for t, attrs in parsed(fragment):
        if t == tag:
            return attrs
    return None


def events_in(fragment):
    """这一段里所有事件属性（onerror / onload / onmouseover …）名。"""
    out = []
    for _t, attrs in parsed(fragment):
        out += [a for a in attrs if _EVENT_ATTR.match(a)]
    return out


def fence(lang, body="code"):
    return "```%s\n%s\n```\n" % (lang, body)


# ---------- A 属性逃逸 ----------
INJ_LANG = 'js" onmouseover="alert(1)'
out = book_export.md_to_html(fence(INJ_LANG))
chk("逃逸：语言串带引号不产生事件属性", not events_in(out), out)
code = first("code", out)
chk("逃逸：引号转义后 class 读回来还是原串",
    code is not None and code.get("class") == "lang-" + INJ_LANG, code)
chk("逃逸：代码正文没被牵连", "code</code>" in out, out)

# < / > / & 在语言串里也一律转义：它们关不掉属性，但也不能原样进成品
esc = book_export.md_to_html(fence("a<b&c"))
chk("转义：读回来的 class 与原文一致",
    first("code", esc) is not None and first("code", esc).get("class") == "lang-a<b&c", esc)
chk("转义：渲染结果里没有生标签 <b>",
    "<b>" not in esc and "&lt;b" in esc, esc)

# ---------- B 回归：正常围栏一个字不改 ----------
chk("回归：```python 照旧",
    book_export.md_to_html(fence("python")) ==
    '<pre><code class="lang-python">code</code></pre>')
chk("回归：没写语言就没有 class",
    book_export.md_to_html(fence("")) == "<pre><code>code</code></pre>")
chk("回归：正文里的尖括号照旧转义",
    book_export.md_to_html(fence("python", "<b>x</b>")) ==
    '<pre><code class="lang-python">&lt;b&gt;x&lt;/b&gt;</code></pre>')

# ---------- C 成品里也一样：真打一个 EPUB 再解析包内那一页 ----------
TMP = tempfile.mkdtemp(prefix="guizang-export-fence-")
try:
    book = os.path.join(TMP, "bk")
    os.makedirs(os.path.join(book, "chapters"))
    with open(os.path.join(book, "chapters", "0001.md"), "w", encoding="utf-8") as f:
        f.write("# 第一章\n\n" + fence(INJ_LANG) + "\n")
    dest = os.path.join(TMP, "fence.epub")
    book_export.build_epub(book, dest, title="围栏样张")
    with zipfile.ZipFile(dest) as z:
        sec = [n for n in z.namelist() if n.startswith("OEBPS/sec")][0]
        body = z.read(sec).decode("utf-8")
    chk("EPUB：包里正文不产生事件属性", not events_in(body), body[:400])
    chk("EPUB：包里把语言串的引号转义了", "&quot;" in body, body[:400])
finally:
    shutil.rmtree(TMP, ignore_errors=True)

print()
print("围栏语言串逃逸取证：" + ("全部通过" if not FAIL else "%d 项未过：%s" % (len(FAIL), FAIL)))
sys.exit(1 if FAIL else 0)
