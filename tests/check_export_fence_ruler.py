#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""导出的围栏也认 md_fence 那一把尺子：代码块与正文不再各判各的。

背景
    `md_fence` 是仓库里唯一一份「这一行在不在围栏代码块里」的状态机，`check_fence_
    rulers` 已经钉住 writer / book_import / ai_sum / book_notes 四处接线。可导出这一
    路（`book_export.md_to_html`）一直自带一份「行首是不是 ```」的开关，于是同一段稿
    子在这里和在别处有了两副面孔 —— 而且坏在用户最后拿到手的 EPUB / PDF 上：

      · `` ``` a`b ``：CommonMark 规定**反引号起**的围栏，信息串里不许再出现反引号，
        所以这只是一行普通正文。旧写法当它是开栏，把后面真正的标题与正文一路吞进
        `<pre>` —— 导出缺内容，还看不出缺在哪。
      · `~~~` 起的围栏不认：代码块被拆成一堆 `<h1>` 与 `<p>`，程序注释变成了章标题。
      · 收围栏不比字符、不比长度：`~~~` 能把 ``` 起的块收掉，短一行也能收掉。

这一条把「接线 + 四类边角 + 老口径回归 + 成品」一起钉住，纯标准库、不联网、不开浏览器，
所以归 --fast 静态段：

  A 对照：证明旧判据（`strip().startswith("```")`）确实会把 `` ``` a`b `` 当开栏，
    这一组断言不是空转；
  B 一把尺子：book_export 从 md_fence 取尺子，不再自带那份开关；
  C 四类边角：信息串带反引号算正文、波浪号围栏照认、收围栏要同种字符、不短于起围栏、
    收围栏后面不许有别的东西；
  D 老口径回归：正常 ```python / 无语言围栏、代码正文的转义、代码里的反斜杠一个字不改；
  E 成品：真打一个 EPUB，被吞掉的标题必须出现在包里那一页的 `<h1>` 里。
"""
import os
import re
import shutil
import sys
import tempfile
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import book_export        # noqa: E402
import md_fence           # noqa: E402

FAIL = []
PASSED = 0


def chk(name, cond, extra=""):
    global PASSED
    print(("PASS  " if cond else "FAIL  ") + name + ("" if cond else "   " + repr(extra)[:240]))
    if cond:
        PASSED += 1
    else:
        FAIL.append(name)


BT = "`"
FENCE = BT * 3


# ── A 对照：旧判据确实会误判 ────────────────────────────────────────
def naive_open(line):
    """旧 book_export 的判据：去掉缩进后以三个反引号开头就算开栏。"""
    return str(line).strip().startswith(FENCE)


TRICKY = FENCE + " a" + BT + "b"
chk("A1 对照：旧判据把 `` ``` a`b `` 当开栏", naive_open(TRICKY))
chk("A2 尺子：md_fence 认为它不是围栏", md_fence.fence_mark(TRICKY) is None)


# ── B 一把尺子：接线与源码不再自带开关 ──────────────────────────────
with open(os.path.join(ROOT, "book_export.py"), encoding="utf-8") as f:
    SRC = f.read()

chk("B1 book_export 从 md_fence 取尺子", re.search(r"import md_fence\b", SRC) is not None)
chk("B2 块级判定走 md_fence.fence_mark", "md_fence.fence_mark(" in SRC)
chk("B3 抽取转义前的「哪些行在代码块里」走 md_fence.fence_mask",
    "md_fence.fence_mask(" in SRC)
chk("B4 不再自带「行首是不是 ```」的开关",
    'startswith("```")' not in SRC and "startswith('```')" not in SRC)


# ── C 四类边角 ─────────────────────────────────────────────────────
out = book_export.md_to_html(FENCE + " a" + BT + "b\n# 真标题\n正文\n")
chk("C1 信息串带反引号是正文：真标题仍渲染成 h1",
    "<h1>真标题</h1>" in out and "<pre>" not in out, out)

out = book_export.md_to_html("~~~\n# 注释\n~~~\n# 真标题\n正文\n")
chk("C2 波浪号围栏照认：注释在代码块里，不是标题",
    "<pre><code># 注释</code></pre>" in out and "<h1>注释</h1>" not in out, out)
chk("C3 波浪号围栏收尾后，正文里的标题照旧",
    "<h1>真标题</h1>" in out, out)

out = book_export.md_to_html(FENCE + "\n# 注释\n~~~\n# 仍算注释\n" + FENCE + "\n# 出来\n")
chk("C4 收围栏必须同种字符：`~~~` 收不掉 ``` 起的块",
    "# 仍算注释" in out and "<h1>仍算注释</h1>" not in out
    and "<h1>出来</h1>" in out, out)

out = book_export.md_to_html(BT * 4 + "\n# 注释\n" + FENCE + "\n# 还在块里\n"
                             + BT * 4 + "\n# 出来\n")
chk("C5 收围栏不短于起围栏：三个反引号收不掉四个反引号起的块",
    "# 还在块里" in out and "<h1>还在块里</h1>" not in out
    and "<h1>出来</h1>" in out, out)

out = book_export.md_to_html(FENCE + "\n# 注释\n" + FENCE + " trailing\n# 真标题\n" + FENCE)
chk("C6 收围栏后面不许有别的东西",
    "<pre><code># 注释\n``` trailing\n# 真标题</code></pre>" in out, out)

out = book_export.md_to_html("~~~ a" + BT + "b\n# 注释\n~~~\n# 标题\n")
chk("C7 反引号这条限制只对反引号围栏：波浪号的信息串里可以有反引号",
    'class="lang-a`b"' in out and "<h1>标题</h1>" in out, out)


# ── D 老口径回归 ──────────────────────────────────────────────────
chk("D1 回归：```python 一个字不改",
    book_export.md_to_html(FENCE + "python\ncode\n" + FENCE + "\n")
    == '<pre><code class="lang-python">code</code></pre>')
chk("D2 回归：没写语言就没有 class",
    book_export.md_to_html(FENCE + "\ncode\n" + FENCE + "\n")
    == "<pre><code>code</code></pre>")
chk("D3 回归：代码正文里的尖括号照旧转义",
    book_export.md_to_html(FENCE + "python\n<b>x</b>\n" + FENCE + "\n")
    == '<pre><code class="lang-python">&lt;b&gt;x&lt;/b&gt;</code></pre>')
chk("D4 回归：代码里的反斜杠不被当转义吃掉",
    book_export.md_to_html(FENCE + "\n" + r"\d+\." + "\n" + FENCE + "\n")
    == "<pre><code>" + r"\d+\." + "</code></pre>")
chk("D5 回归：正文里的行内码与转义照旧",
    "<code>" + r"\d+\." + "</code>" in book_export.md_to_html(r"正则 `\d+\.` 用来举例"))


# ── E 成品：真打一个 EPUB 再解析包里那一页 ──────────────────────────
TMP = tempfile.mkdtemp(prefix="guizang-export-fence-ruler-")
try:
    book = os.path.join(TMP, "bk")
    os.makedirs(os.path.join(book, "chapters"))
    doc = ("# 第一章\n\n"
           + FENCE + " a" + BT + "b\n# 第二章标题\n正文来了\n\n"
           + "~~~\n# 这行是代码注释\n~~~\n")
    with open(os.path.join(book, "chapters", "0001.md"), "w", encoding="utf-8") as f:
        f.write(doc)
    dest = os.path.join(TMP, "fence-ruler.epub")
    book_export.build_epub(book, dest, title="围栏尺子样张")
    with zipfile.ZipFile(dest) as z:
        secs = [n for n in z.namelist() if n.startswith("OEBPS/sec")]
        body = "\n".join(z.read(n).decode("utf-8") for n in secs)
    chk("E1 EPUB：被误判吞掉的标题出现在 h1 里", "<h1>第二章标题</h1>" in body, body[:500])
    chk("E2 EPUB：波浪号代码块里的注释没有变成标题",
        "<h1>这行是代码注释</h1>" not in body and "这行是代码注释" in body, body[:500])
finally:
    shutil.rmtree(TMP, ignore_errors=True)

print()
if FAIL:
    print("导出围栏尺子：失败 %d 项" % len(FAIL))
    for n in FAIL:
        print("  ✗ " + n)
    sys.exit(1)
print("导出围栏尺子：通过 %d 项，失败 0 项" % PASSED)
