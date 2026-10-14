#!/usr/bin/env python3
"""导出口径里的属性逃逸：图片地址、链接地址、图片 alt 不能被当成「随便塞的字符串」。

为什么单独一份：`book_export.md_to_html` 是**所有成品**（导出的 HTML、EPUB、
交给铬内核打印的 PDF）的唯一渲染器。它先把整篇转义、再替换行内标记，而
`_esc` 走的是 `quote=False` —— 正文里这样正好（双引号在文本节点里没有危害），
可图片地址、链接地址、alt 是**属性值**，得另外把双引号也挡住。

漏了这一道的后果不是「排版歪了」，是**注进来的一段能执行的脚本**：
别人网页里那张图的地址、导入书里写的链接，只要带一个双引号就能从 `src="…"`
里逃出去，往标签上再挂一个 `onerror=…`。剪藏那条线早就是用
`html.escape(href, quote=True)` 写的，这里必须同一把尺子。

钉住三件事：
  A 逃逸：三种写法（地址带引号、链接带引号、alt 带引号）都不能多出任何
    事件属性；同时那个引号要**原样留在属性值里**（是转义，不是悄悄删掉）。
  B 回归：正常的图、链接、表格一个字不改，`&` 仍照老样子转成 `&amp;`。
  C 成品里也一样：真打一个 EPUB，包里那页 XHTML 也解析不出事件属性。

不联网、不起浏览器：解析用标准库的 HTMLParser，EPUB 走临时目录。
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
    """这一段里所有事件属性（onerror / onload / onclick …）名。"""
    out = []
    for _t, attrs in parsed(fragment):
        out += [a for a in attrs if _EVENT_ATTR.match(a)]
    return out


# ---------- A 属性逃逸 ----------
INJ_IMG = '![a](x"onerror="alert(1)")'
INJ_LINK = '[t](x"onclick="evil())'
INJ_ALT = '![alt" onload="x](y.png)'

for 名字, md in (("图片地址带引号", INJ_IMG), ("链接地址带引号", INJ_LINK)):
    out = book_export.md_to_html(md)
    chk("逃逸：%s 不产生事件属性" % 名字, not events_in(out), out)
    chk("逃逸：%s 的引号被转义保留（不是删掉）" % 名字, "&quot;" in out, out)

alt_out = book_export.md_to_html(INJ_ALT)
chk("逃逸：alt 带引号不产生事件属性", not events_in(alt_out), alt_out)
chk("逃逸：alt 的引号转义后仍是原值",
    first("img", alt_out) and first("img", alt_out).get("alt") == 'alt" onload="x', alt_out)
chk("逃逸：alt 里的图地址没被牵连",
    first("img", alt_out) and first("img", alt_out).get("src") == "y.png", alt_out)

# 地址里的引号转义后，作为属性值读回来应当是原样那一串
src_val = first("img", book_export.md_to_html(INJ_IMG))
chk("逃逸：图片地址读回来是原地址", src_val and src_val.get("src") == 'x"onerror="alert(1', src_val)
href_val = first("a", book_export.md_to_html(INJ_LINK))
chk("逃逸：链接地址读回来是原地址", href_val and href_val.get("href") == 'x"onclick="evil(', href_val)

# ---------- B 回归：正常内容一个字不改 ----------
chk("回归：普通图片照旧",
    '<img src="images/a.png" alt="图"/>' in book_export.md_to_html("![图](images/a.png)"))
chk("回归：普通链接照旧，& 仍转义",
    '<a href="https://a.com/b?x=1&amp;y=2">x</a>' in
    book_export.md_to_html("[x](https://a.com/b?x=1&y=2)"))
chk("回归：图片 alt 里的 & 仍转义",
    'alt="c&amp;d"' in book_export.md_to_html("![c&d](images/e&f.png)"))
chk("回归：表格还是表格（没被行内替换带歪）",
    "<table><thead><tr>" in book_export.md_to_html(
        "| 甲 | 乙 |\n| --- | --- |\n| 一 | 二 |"))

# ---------- C 成品里也一样：真打一个 EPUB 再解析包内那一页 ----------
TMP = tempfile.mkdtemp(prefix="guizang-export-safety-")
try:
    book = os.path.join(TMP, "bk")
    os.makedirs(os.path.join(book, "chapters"))
    with open(os.path.join(book, "chapters", "0001.md"), "w", encoding="utf-8") as f:
        f.write("# 第一章\n\n" + INJ_IMG + "\n\n" + INJ_ALT + "\n")
    dest = os.path.join(TMP, "safe.epub")
    book_export.build_epub(book, dest, title="逃逸样张")
    with zipfile.ZipFile(dest) as z:
        sec = [n for n in z.namelist() if n.startswith("OEBPS/sec")][0]
        body = z.read(sec).decode("utf-8")
    chk("EPUB：包里正文不产生事件属性", not events_in(body), body[:400])
    chk("EPUB：包里正文把引号转义了", "&quot;" in body, body[:400])
finally:
    shutil.rmtree(TMP, ignore_errors=True)

print()
print("导出口径取证：" + ("全部通过" if not FAIL else "%d 项未过：%s" % (len(FAIL), FAIL)))
sys.exit(1 if FAIL else 0)
