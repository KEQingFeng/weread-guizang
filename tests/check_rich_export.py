#!/usr/bin/env python3
"""写作页工具条产出的那些标记，能不能**活着**走出导出。

为什么要单独一份：工具条一改富文本，稿子里就会出现以前只有取书正文才有的东西
—— 内联颜色、底色、下划线、删除线、待办勾、表格。这些标记在界面上看着对，
不代表导出的 HTML / 图片 / PDF / 电子书里也在：正文一律走 `book_export.md_to_html`，
它没学过的语法会被**原样转义**，导出来就是一行看得见的 `<span style=...>`。
这类 bug 在预览里看不见（预览用另一套渲染器），只在成品上暴露，最难查。

钉住四件事：
  A 白名单：工具条真会写的三种内联标记要还原成真的 HTML
  B 白名单之外：手打的 <script> / <div class=..> / 非十六进制颜色，必须留在转义态
  C 块级新语法：待办勾变成带 class 的 li，表格变成 thead/tbody，且段落不许吞掉表格
  D 成品里也有：三份样式表（电子书 / 打印 / 排版模板）都给了这几样长相，
    并且真打一个 EPUB 出来，确认包里的正文和样式都带上了
  E 转义：\\# \\* \\| 这类「这是个字不是记号」两边都得认，代码块与行内码不受牵连

不联网、不起浏览器：全程在临时目录里造一份最小书库。
"""
import os
import shutil
import sys
import tempfile
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import book_export          # noqa: E402
import writer               # noqa: E402

FAIL = []


def chk(name, cond, extra=""):
    print(("PASS  " if cond else "FAIL  ") + name + ("" if cond else "   " + repr(extra)[:220]))
    if not cond:
        FAIL.append(name)


SAMPLE = """## 小样

正常一句，<span style="color:#c9252d">染了红</span>、<span style="background-color:#ffe58a">打了底</span>、<u>画了下划线</u>、~~划掉这句~~、**粗**、*斜*、`码`。

- [ ] 没办完
- [x] 办完了
- 普通一条

| 书名 | 状态 |
| --- | ---: |
| **活着** | 在读 |

上一段 | 带一根竖线但不是表格
"""

HTML = book_export.md_to_html(SAMPLE)

# ---------- A 白名单里的三种内联标记 ----------
chk("内联：颜色还原成真的 span", '<span style="color:#c9252d">染了红</span>' in HTML, HTML[:200])
chk("内联：底色还原成真的 span",
    '<span style="background-color:#ffe58a">打了底</span>' in HTML)
chk("内联：下划线还原", "<u>画了下划线</u>" in HTML)
chk("内联：删除线走 markdown 的 ~~", "<s>划掉这句</s>" in HTML)
chk("内联：粗斜体行内码照旧",
    "<strong>粗</strong>" in HTML and "<em>斜</em>" in HTML and "<code>码</code>" in HTML)

# ---------- B 白名单之外的一律留在转义态 ----------
chk("越权的标签不许放行：script", "&lt;script&gt;alert(1)&lt;/script&gt;"
    in book_export.md_to_html("<script>alert(1)</script>"))
chk("越权的标签不许放行：div 带属性",
    "&lt;div class=\"x\"&gt;" in book_export.md_to_html('<div class="x">'))
chk("颜色写法不合规矩就挡掉（英文色名）",
    "&lt;span style=\"color:red\"&gt;" in book_export.md_to_html('<span style="color:red">x</span>'))
chk("颜色写法不合规矩就挡掉（带空格与分号）",
    "&lt;span style = \"color: #fff ;\"&gt;"
    in book_export.md_to_html('<span style = "color: #fff ;">x</span>'))
chk("行内码里举例写的标记不该真的生效",
    "<code>&lt;span style=\"color:#000000\"&gt;</code>"
    in book_export.md_to_html('`<span style="color:#000000">` 是例子'))

# ---------- C 块级：待办勾与表格 ----------
chk("待办：没勾上的带 task", '<li class="task">没办完</li>' in HTML, HTML[:400])
chk("待办：勾上的带 task done", '<li class="task done">办完了</li>' in HTML)
chk("染色的字只按字面计数（尖括号里那些词不吃进来）",
    writer.words('<span style="color:#c9252d">三个字</span>')['total'] == 3,
    writer.words('<span style="color:#c9252d">三个字</span>'))
chk("表格：表头成 thead 且带 scope", "<thead><tr><th scope=\"col\">书名</th>" in HTML)
chk("表格：分隔行不吃进正文", "<tbody><tr><td><strong>活着</strong></td>" in HTML)
chk("表格：分隔行的冒号变成对齐", 'style="text-align:right"' in HTML)
chk("表格：只有一根竖线的句子仍然是段落",
    "<p>上一段 | 带一根竖线但不是表格</p>" in HTML, HTML[-200:])
chk("表格：紧跟段落时不会被段落吞掉",
    "<p>甲乙丙</p>\n<table>" in book_export.md_to_html("甲乙丙\n| a | b |\n| --- | --- |\n| 1 | 2 |\n"))
chk("表格：缺格补空、多格截掉",
    book_export.md_to_html("| a | b |\n| --- | --- |\n| 1 | 2 | 3 |\n| 4 |\n")
    .count("<td") == 4)

# ---------- D 三份样式表都给了这几样长相 ----------
for 名字, css in (("电子书", book_export._EPUB_CSS), ("打印", book_export._PDF_CSS),
                  ("排版模板", writer._TPL_BASE)):
    chk("样式：%s 画了待办方框" % 名字, "li.task::before" in css)
    chk("样式：%s 勾上的有填色" % 名字, "li.task.done::before" in css)
    chk("样式：%s 认表格" % 名字, "border-collapse" in css)
    chk("样式：%s 认下划线" % 名字, "underline" in css)
    chk("样式：%s 认删除线" % 名字, "line-through" in css)
chk("样式：排版模板的百分号没被格式化吃掉",
    "max-width: 100%;" in (writer._TPL_BASE % writer.TEMPLATES["plain"]))

# 四种排版都渲染一遍：模板之间拼的是同一份正文，任何一套崩在这上面都算事故。
for tid in [t[0] for t in writer.TPL_LABELS]:
    one = writer.export_html(SAMPLE, title="小样", tpl=tid)
    chk("排版 %s：导出页里颜色还在" % tid, 'style="color:#c9252d"' in one)
    chk("排版 %s：导出页里表格还在" % tid, "<table>" in one)

# ---------- 真打一个 EPUB，确认成品包里带着这些 ----------
TMP = tempfile.mkdtemp(prefix="guizang-rich-")
try:
    book = os.path.join(TMP, "bk")
    os.makedirs(os.path.join(book, "chapters"))
    with open(os.path.join(book, "chapters", "0001.md"), "w", encoding="utf-8") as f:
        f.write(SAMPLE)
    dest = os.path.join(TMP, "rich.epub")
    book_export.build_epub(book, dest, title="富文本样张")
    with zipfile.ZipFile(dest) as z:
        names = z.namelist()
        first_stored = (names[0] == "mimetype"
                        and z.getinfo("mimetype").compress_type == zipfile.ZIP_STORED)
        sec = [n for n in names if n.startswith("OEBPS/sec")][0]
        body = z.read(sec).decode("utf-8")
        style = z.read("OEBPS/style.css").decode("utf-8")
    chk("EPUB：正文里颜色没被转义成文字", '<span style="color:#c9252d">' in body)
    chk("EPUB：正文里有表格", "<table>" in body and "<th" in body)
    chk("EPUB：正文里有待办勾", '<li class="task done">' in body)
    chk("EPUB：样式表带了表格与待办的画法",
        "border-collapse" in style and "li.task::before" in style)
    # 这两条是「阅读器拒收」级别的老规矩，顺手在这里再钉一次
    chk("EPUB：mimetype 是第一个条目且不压缩", first_stored, names[:3])
    chk("EPUB：包里有 container.xml", "META-INF/container.xml" in names)
finally:
    shutil.rmtree(TMP, ignore_errors=True)

# ---------- E 转义：工具条写进去的「这是个字，不是记号」 ----------
# 正文不再由用户手写 Markdown，所以「用户打的井号不能被当成标题」这件事必须由
# 生成器写 \\#、渲染器认 \\# 两边一起成立。哪一边漏了都会变成：写的时候没事，
# 一导出句子突然变大标题 —— 这种错在成品上才发现，最难看。
chk("转义：行首的 \\# 不成标题",
    "<h1>" not in book_export.md_to_html("\\# 这不是标题")
    and "<p># 这不是标题</p>" in book_export.md_to_html("\\# 这不是标题"))
chk("转义：\\*\\* 不成粗体",
    "<strong>" not in book_export.md_to_html(r"这里 \*\*不是\*\* 粗体")
    and "这里 **不是** 粗体" in book_export.md_to_html(r"这里 \*\*不是\*\* 粗体"))
chk("转义：\\- [ ] 不成待办列表",
    "<li" not in book_export.md_to_html(r"\- [ ] 这只是一句带方括号的话"))
chk("转义：表格单元格里的 \\| 不切格子",
    '<td>一 | 二</td>' in book_export.md_to_html(
        "| 甲 | 乙 |\n| --- | --- |\n| 一 \\| 二 | x |"))
chk("转义：行内码里的反斜杠原样留着",
    "<code>\\d+\\.</code>" in book_export.md_to_html(r"正则 `\d+\.` 用来举例"))
chk("转义：代码块里的反斜杠原样留着",
    "<pre><code>\\d+\\.</code></pre>" in book_export.md_to_html(
        "\n".join(["```", r"\d+\.", "```"])))
chk("转义：\\~~ 不成删除线",
    "<s>" not in book_export.md_to_html(r"划掉的写法长这样：\~~文字\~~"))
# 写作页那份字数口径（writer.words）不能因为稿子里多了反斜杠就换个数：
# 反斜杠既不是汉字也不是英文词，剥掉转义之后字数应当一模一样。
chk("转义：字数不被反斜杠影响",
    writer.words(r"这\*是一句话")["total"] == writer.words("这是一句话")["total"],
    writer.words(r"这\*是一句话"))

print()
print("富文本导出口径取证：" + ("全部通过" if not FAIL else "%d 项未过：%s" % (len(FAIL), FAIL)))
sys.exit(1 if FAIL else 0)
