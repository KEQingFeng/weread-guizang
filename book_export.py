# -*- coding: utf-8 -*-
"""把取回的书导出成 EPUB / PDF。

两条路都刻意不引新依赖：

- EPUB 纯标准库手搓（zipfile）。规矩只有一条硬性的 —— `mimetype` 必须是
  压缩包里的第一个条目、且以 STORED（不压缩）方式存放，否则阅读器拒收。
- PDF 借已经装好的 Playwright 铬内核打印。前端本来就靠它取书，多这一个用法
  不用再让用户装东西；铬没装好时由调用方先拦一道（见 ui_server.chromium_ready）。

正文的来源有两处：取书时抓下来的 HTML，和写作页那排工具条产出的 Markdown。
两边语法面都不宽（标题、图片、粗斜体、行内码、列表、待办、引用、代码块、
分隔线、链接、表格，加颜色和底色和下划线三个内联标记），所以这里手写一个
够用的 Markdown → HTML，而不是为了一个子集去拖进一个第三方库、再把它塞进
安装包。

白名单只放工具条真正会写的那几种标记：稿子里手打的其它尖括号留在转义态，
导出后仍然是一行看得见的文字，不会变成能执行的东西。
"""

import html
import os
import re
import shutil
import tempfile
import time
import uuid
import zipfile
from pathlib import Path

import md_fence


# ─────────────────────────── 极简 Markdown → HTML ───────────────────────────

_INLINE_CODE = re.compile(r"`([^`]+)`")
_IMG = re.compile(r"!\[([^\]]*)\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
_LINK = re.compile(r"\[([^\]]+)\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
_BOLD = re.compile(r"\*\*([^*]+)\*\*")
_STRIKE = re.compile(r"~~([^~]+)~~")
_ITAL = re.compile(r"(?<!\*)\*([^*]+)\*(?!\*)")
_HR = re.compile(r"(-{3,}|\*{3,}|_{3,})")
_UL = re.compile(r"[-*+]\s+")
_OL = re.compile(r"\d+[.)]\s+")
_TASK = re.compile(r"^\[( |x|X)?\]\s*")
_TROW = re.compile(r"^\|.*\|$")
_TSEP = re.compile(r"^\|(?:\s*:?-+:?\s*\|)+$")

# 工具条往稿子里写的三种内联标记。写作页存的是真 HTML，而本模块第一步就把整行
# 转义了（_esc 用 quote=False，双引号原样留着），所以这几条模式必须写成「转义后」
# 的样子 —— 拿原始尖括号去匹配，永远命不中，导出就是一行看得见的 <span style=…>。
# 颜色只认 #hex 这一段，别的值一律不放行：宁可少一层颜色，也不要让稿子里凑巧
# 写的东西变成能生效的属性。
_SPAN_OPEN = re.compile(
    r'&lt;span style="(background-color|color):(#[0-9A-Fa-f]{3,8})"&gt;')
_SPAN_CLOSE = "&lt;/span&gt;"
_U_OPEN = "&lt;u&gt;"
_U_CLOSE = "&lt;/u&gt;"


def _rich(text):
    """把白名单里的内联标记放回成真的 HTML；其余尖括号保持转义态。"""
    text = _SPAN_OPEN.sub(lambda m: '<span style="%s:%s">' % (m.group(1), m.group(2)), text)
    text = text.replace(_SPAN_CLOSE, "</span>")
    text = text.replace(_U_OPEN, "<u>").replace(_U_CLOSE, "</u>")
    return text


# 「XML 1.0 里没有合法写法」的两类字符，收成一把尺子（_xml_clean）：
#   · C0 控制符（\t \n \r 除外）：XHTML / EPUB 里非法；正文那一路还得靠它躲开
#     占位记号（\x00N\x00 / \x01N\x01）撞车，理由写在 _stash_line 那段。
#   · 落单的半个代理码点（U+D800–U+DFFF 不成对）：JSON 的 `"\ud800"` 转义读得回来，
#     可 utf-8 里编不出来 —— zipfile.writestr 在 encode 那一步就抛。
# 量哪儿是清楚的一条线：**按整份文本走转义管线的正文不动**（它中间夹着占位记号，
# 顺手洗掉 C0 反而会把 \\# \\* 这些转义连同记号一起抹掉 —— 实测过，正文里的转义
# 会当场消失），洗的是**书的身份**：书名 / 作者。它们从手改 / 云同步 / 导入的
# meta.json 直接过来，而 `book_layout.read_meta` 只摘代理码点、**不摘控制符**。
# 于是标题里一个换页符（`"\u000c"`，从 PDF 里粘书名就会带进来）就能让 content.opf
# 与 cover.xhtml「不是合法 XML」，阅读器把整本书拒收 —— 而导出这一步返回成功，
# 界面上看是「导出好了」，用户点开是报错或空白。收口落在 build_epub / build_pdf
# 两个入口（元数据进成品之前那一步），_esc / _attrq 一个字不改。
_SURROGATE = re.compile("[\ud800-\udfff]")
_CTRL_DROP = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def _xml_clean(s):
    """书的身份（书名 / 作者）落进 XML 之前摘掉 XML / utf-8 都不接受的字符。

    正常文字一个字不改；`\\t \\n \\r` 是合法 XML，留着。
    """
    s = _SURROGATE.sub("", str(s if s is not None else ""))
    return _CTRL_DROP.sub("", s)


def _esc(s):
    return html.escape(str(s if s is not None else ""), quote=False)


def _attrq(s):
    """属性值里的双引号。

    `_esc` 只转 & < >（quote=False），双引号原样留着 —— 正文里这样正好，可图片
    地址、链接地址与图片 alt 都是**外部内容**（别人网页里的图片 URL、导入书里
    写的链接），直接塞进 `src="…"` / `alt="…"` 就是一个逃逸口：一个引号就能关掉
    属性、再往标签上挂一个 onerror=… ，导出的 HTML 与交给铬内核打印的 PDF 里
    就是能执行的脚本。凡是往属性里塞的，都必须再过这一道。
    """
    return str(s if s is not None else "").replace('"', "&quot;")


def _attr(s):
    """双引号属性值里的一整段字符串：先按文本转义 &<>，再把双引号收成 &quot;。

    `_attrq` 只做后一半 —— 正文那条路的调用点（`_inline`）自己先 `_esc` 过一遍，
    所以只补引号就够。可 images/ 目录里的文件名是直接读出来的（下载器只挡住了
    路径分隔符与控制符，`& < > "` 都留着），没有「先 `_esc`」那一道，
    这里一次到位才不会漏：漏一个引号，`href="img/…"` 就会提前收尾，
    整份 content.opf / cover.xhtml 不再是合法 XML。
    """
    return _attrq(_esc(s))


def _inline(text):
    """行内语法。先转义再替换 —— 转义只动 &<>，不管是 * ` [] 还是括号，
    所以标记符号原样在，替换后插进去的 HTML 也不会被二次转义。"""
    text = _esc(text)
    stash = []

    def _keep(m):
        stash.append(m.group(1))
        return "\x00%d\x00" % (len(stash) - 1)

    text = _INLINE_CODE.sub(_keep, text)
    # 白名单放在抽出行内码之后：`<span style="color:#000">` 这种「把标记当例子写」
    # 的句子该照原样出现在 <code> 里，不该真的染上颜色。
    text = _rich(text)
    text = _IMG.sub(lambda m: '<img src="%s" alt="%s"/>'
                    % (_attrq(m.group(2)), _attrq(m.group(1))), text)
    text = _LINK.sub(lambda m: '<a href="%s">%s</a>'
                     % (_attrq(m.group(2)), m.group(1)), text)
    text = _BOLD.sub(lambda m: "<strong>%s</strong>" % m.group(1), text)
    text = _STRIKE.sub(lambda m: "<s>%s</s>" % m.group(1), text)
    text = _ITAL.sub(lambda m: "<em>%s</em>" % m.group(1), text)
    text = re.sub(r"\x00(\d+)\x00",
                  lambda m: "<code>%s</code>" % stash[int(m.group(1))], text)
    return text


def _li(text):
    """一条列表项。写作页的待办勾存成 - [ ] / - [x]，这里换成带状态 class 的 li。
    方框由 CSS 画，不产出 <input>：EPUB 阅读器对表单元素的支持参差，打印时又容易
    被画成「能点」的样子，而导出的成品里没有一处该让人点。"""
    m = _TASK.match(text)
    if not m:
        return "<li>%s</li>" % _inline(text)
    done = (m.group(1) or "").lower() == "x"
    return '<li class="task%s">%s</li>' % (" done" if done else "",
                                          _inline(text[m.end():]))


def _tcells(line):
    """拆一行表格：去掉首尾竖线再按竖线分格。单元格里要写真竖线，写 \\| ——
    那一处转义在进到这里之前已经被换成占位，所以不会把格子切歪。"""
    return [c.strip() for c in line.strip()[1:-1].split("|")]


def _talign(cell):
    """分隔行那一格里的冒号决定对齐。左对齐是文字排版的默认，不额外写属性。"""
    left = cell.startswith(":")
    if cell.endswith(":"):
        return "center" if left else "right"
    return ""


def _table_at(lines, i):
    """i 这一行是不是表头。判据是「这一行像表格行，下一行就是分隔行」——
    只有这一条能把「段落里一句带竖线的话」和「真表格」分开。"""
    return (i < len(lines) and _TROW.match(lines[i].strip()) is not None
            and i + 1 < len(lines) and _TSEP.match(lines[i + 1].strip()) is not None)


def _render_table(head, aligns, rows):
    """列数以表头为准：数据行多了截掉、少了补空，跟 GFM 的处理一致。"""
    cols = len(head)

    def cells(tag, row):
        out = []
        for c in range(cols):
            txt = row[c] if c < len(row) else ""
            align = aligns[c] if c < len(aligns) else ""
            style = ' style="text-align:%s"' % align if align else ""
            scope = ' scope="col"' if tag == "th" else ""
            out.append("<%s%s%s>%s</%s>" % (tag, scope, style, _inline(txt), tag))
        return "".join(out)

    return ("<table><thead><tr>%s</tr></thead><tbody>%s</tbody></table>"
            % (cells("th", head),
               "".join("<tr>%s</tr>" % cells("td", r) for r in rows)))


# 写作页工具条往稿子里写的「这是个字，不是记号」：反斜杠 + 一个对 markdown 有意义
# 的字符。用户手打 Markdown 那套已经不成立了（正文由工具条生成），所以这一层必须由
# 生成器写、渲染器认 —— 否则人在正文里打的一个井号，导出后变成一个标题。
_ESC_CHAR = re.compile(r"\\([\\`*_{}\[\]()#+\-.!>~|])")
_STASH_MARK = re.compile(r"\x01(\d+)\x01")

# 占位机制（_inline 的 \x00N\x00 与 _stash_line 的 \x01N\x01）拿 C0 控制字节当记号，
# 而正文是外部内容：导入的书、剪藏回来的网页、用户手改或云同步写回的章节，里面真
# 可能出现这两个字节（0x00 / 0x01 都是合法 UTF-8，read_chapters 一个字都不换）。那
# 时正文里凑巧一段「\x01 + 几位数字 + \x01」就会被当成自己的占位：序号越界是
# IndexError 把整趟导出打抛（界面上「导不出来」），序号落回范围内则更阴 —— 一段
# 正文被悄悄替换成另一个转义字符。C0 控制符（\t \n \r 除外）本来也不合法于
# XHTML / EPUB，落进成品还可能被阅读器拒收，所以入口一次洗掉，占位与正文再无交集。
# （那把尺子 `_CTRL_DROP` 现在定义在上面，_xml_clean 与正文共用同一份。）


def _stash_line(line, keep):
    """把这一行里的 \\X 收进 keep，原地留一个 \\x01 序号 \\x01 的占位。

    占位里不含换行，所以「一行一块」那套判据一律不受影响；行首的井号被换掉之后，
    `#{1,6}` 那条自然命不中 —— 这一步必须在分块之前做，晚一步标题已经切好了。
    反引号那一段是代码，里面的字符原样留着：举例写的 `\\d+\\.` 被吃掉一个反斜杠，
    导出的代码块就成了错的正则。
    """
    out = []
    pos = 0

    def _sub(chunk):
        def _keep(m):
            keep.append(m.group(1))
            return "\x01%d\x01" % (len(keep) - 1)
        return _ESC_CHAR.sub(_keep, chunk)

    for m in _INLINE_CODE.finditer(line):
        out.append(_sub(line[pos:m.start()]))
        out.append(m.group(0))
        pos = m.end()
    out.append(_sub(line[pos:]))
    return "".join(out)


def _restore(text, keep):
    """占位换回真字。放在整篇拼完之后：那时转义早过了，得自己再补一遍。"""
    if not keep:
        return text
    return _STASH_MARK.sub(lambda m: _esc(keep[int(m.group(1))]), text)


def md_to_html(md):
    """块级语法。返回一段一段的 HTML，句首句尾都不带换行，方便拼。

    围栏代码块只认 `md_fence` 那一把尺子。导出此前自带一份「行首是不是 ```」的
    开关，于是同一段稿子在这里和大纲 / 切章那一侧有了两副面孔：

      · `` ``` a`b ``（反引号信息串里还有反引号）按 CommonMark 只是普通正文，
        旧写法却当它开栏，把后面真正的标题与正文一路吞进 `<pre>` —— 导出丢内容，
        还丢得看不出是丢在哪；
      · `~~~` 起的围栏不认，代码块被拆成一堆 `<h1>` 与 `<p>`；
      · 收围栏不比长度、不比字符：短一行或另一种字符也能把块收掉。

    这些正是 `md_fence` 已经收口过、`check_fence_rulers` 钉过的那几条，只是导出
    这条路没接上。现在抽取反斜杠转义与块级判定都过同一份状态机，两边不会再走散。
    """
    raw = (_CTRL_DROP.sub("", md or "")
           .replace("\r\n", "\n").replace("\r", "\n").split("\n"))
    keep = []
    # 哪些行在代码块里由 md_fence 说了算；代码块内的反斜杠是字面量，不进抽取。
    in_code = md_fence.fence_mask(raw)
    lines = [s if coded else _stash_line(s, keep)
             for s, coded in zip(raw, in_code)]
    out = []
    i, n = 0, len(lines)
    while i < n:
        s = lines[i].strip()
        if not s:
            i += 1
            continue
        # 围栏代码块
        mark = md_fence.fence_mark(lines[i])
        if mark:
            ch, run, info = mark
            lang = info.strip()
            buf = []
            i += 1
            while i < n:
                close = md_fence.fence_mark(lines[i])
                # 收围栏必须同种字符、不短于起围栏、后面没有别的东西。
                if close and close[0] == ch and close[1] >= run and not close[2].strip():
                    i += 1
                    break
                buf.append(lines[i])
                i += 1
            # 围栏的语言串也来自外部内容（导入书 / 剪藏原文），落点又是 class="…"
            # 这个属性，所以和图片地址一样得先过 _attrq：`_esc` 只转 &<>，双引号
            # 留着就能关掉属性再挂一个 onmouseover=…。
            cls = ' class="lang-%s"' % _attrq(_esc(lang)) if lang else ""
            out.append("<pre><code%s>%s</code></pre>" % (cls, _esc("\n".join(buf))))
            continue
        # 分隔线（合并稿里章与章之间就是靠它分段的）
        if _HR.fullmatch(s):
            out.append("<hr/>")
            i += 1
            continue
        # 标题
        m = re.match(r"(#{1,6})\s+(.*)$", s)
        if m:
            lvl = len(m.group(1))
            out.append("<h%d>%s</h%d>" % (lvl, _inline(m.group(2).strip()), lvl))
            i += 1
            continue
        # 引用
        if s.startswith(">"):
            buf = []
            while i < n and lines[i].strip().startswith(">"):
                buf.append(lines[i].strip()[1:].strip())
                i += 1
            out.append("<blockquote><p>%s</p></blockquote>"
                       % "<br/>".join(_inline(x) for x in buf))
            continue
        # 表格（工具条插的那张表就是这副写法）
        if _table_at(lines, i):
            head = _tcells(s)
            aligns = [_talign(c) for c in _tcells(lines[i + 1].strip())]
            i += 2
            rows = []
            while i < n and _TROW.match(lines[i].strip()):
                rows.append(_tcells(lines[i].strip()))
                i += 1
            out.append(_render_table(head, aligns, rows))
            continue
        # 无序 / 有序列表
        m = _UL.match(s)
        if m:
            buf = []
            while i < n and _UL.match(lines[i].strip()):
                buf.append(_UL.sub("", lines[i].strip(), count=1))
                i += 1
            out.append("<ul>%s</ul>" % "".join(_li(x) for x in buf))
            continue
        if _OL.match(s):
            buf = []
            while i < n and _OL.match(lines[i].strip()):
                buf.append(_OL.sub("", lines[i].strip(), count=1))
                i += 1
            out.append("<ol>%s</ol>" % "".join(_li(x) for x in buf))
            continue
        # 段落：一直吃到下一个块级起点
        buf = [s]
        i += 1
        while i < n:
            nxt = lines[i].strip()
            if (not nxt or md_fence.fence_mark(lines[i]) or nxt.startswith(">")
                    or _HR.fullmatch(nxt) or re.match(r"#{1,6}\s+", nxt)
                    or _UL.match(nxt) or _OL.match(nxt) or _table_at(lines, i)):
                break
            buf.append(nxt)
            i += 1
        out.append("<p>%s</p>" % "<br/>".join(_inline(x) for x in buf))
    return _restore("\n".join(out), keep)


# ─────────────────────────── 公共小工具 ───────────────────────────

def read_chapter_file(path):
    """单章正文：按字节读、再按 UTF-8 解，认不出的字节用 U+FFFD 顶上。

    章节文件是用户能直接编辑的文本，也会被云同步 / 旧版本 / 别的编辑器写回来：
    一个手滑另存成 GBK 的章节、或一次写到一半的断电，严格 UTF-8 读法当场
    UnicodeDecodeError。导出那两条路（EPUB / PDF）早就按这个口径收口了，但
    **读同一批文件的还有界面这一侧** —— 阅读页取某一章正文、合并稿、打包、
    整篇小结，各自再读一遍。同一个坏字节在那边是「这一章打不开 / 下载点了没反应」，
    在这边是「整本书导不出来」，病根一模一样，尺子也只该有一把，所以抽到这里：
    导出与读写章节的地方都走这个函数。

    编码那一半用 `utf-8-sig`：章节 / 写作稿也是「用户能直接用记事本改的文本」，
    Windows 记事本「另存为 UTF-8」会在最前面多塞三字节 BOM。裸 `utf-8` 解出来的
    开头是一个 U+FEFF，它跟着正文一路往下走 —— `writer.outline` / `title_from` 按
    `^#` 认标题，行首顶着 BOM 的第一行 `# 标题` 当场认不出来（大纲是空的、标题退回
    「未题名」），字数也把这三字节算进去；导出 EPUB / PDF 时那个不可见字符同样留在
    正文里。`utf-8-sig` 只在开头出现 BOM 时摘掉它，正文中间的同名码点一个字不动，
    与 `book_layout.read_meta` / 各本账读口是同一条口径（写口照旧 `utf-8`，不种 BOM）。

    认不出的字节只换成替代符，正文其余部分照旧（正常 UTF-8 一个字都不动）；
    文件读不到（不存在 / 权限 / 同名目录）才回 None，由调用方决定怎么落。
    """
    try:
        with open(path, "rb") as f:
            return f.read().decode("utf-8-sig", errors="replace")
    except OSError:
        return None


def read_chapters(book_dir):
    """(文件名, 正文) 顺序读回逐章 md。

    逐章走 read_chapter_file 那唯一一把尺子：坏字节换成替代符、正文其余部分
    照旧，不丢章、也不把一个坏文件当成整本书的错。读不到的文件（权限 / 同名
    目录）只跳过，不带崩整趟。
    """
    ch_dir = os.path.join(book_dir, "chapters")
    if not os.path.isdir(ch_dir):
        return []
    out = []
    for fn in sorted(f for f in os.listdir(ch_dir) if f.endswith(".md")):
        text = read_chapter_file(os.path.join(ch_dir, fn))
        if text is not None:
            out.append((fn, text))
    return out


def _first_heading(body, fallback):
    m = re.search(r"<h1>(.*?)</h1>", body)
    if m:
        t = re.sub(r"<[^>]+>", "", m.group(1)).strip()
        if t:
            return t
    return fallback


_MIME_BY_EXT = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
                ".gif": "image/gif", ".webp": "image/webp", ".svg": "image/svg+xml",
                ".bmp": "image/bmp"}


def _book_images(book_dir):
    d = os.path.join(book_dir, "images")
    if not os.path.isdir(d):
        return []
    return [f for f in sorted(os.listdir(d))
            if not f.startswith(".") and os.path.isfile(os.path.join(d, f))]


# ─────────────────────────── EPUB 3 ───────────────────────────

_EPUB_CSS = """\
html, body { margin: 0; padding: 0; }
body { font-family: "PingFang SC", "Songti SC", "Noto Serif CJK SC", serif;
       line-height: 1.85; color: #1c1c1e; }
h1 { font-size: 1.5em; margin: 1.4em 0 .8em; line-height: 1.4; }
h2 { font-size: 1.28em; margin: 1.3em 0 .6em; }
h3, h4, h5, h6 { font-size: 1.1em; margin: 1.1em 0 .5em; }
p { margin: 0 0 .9em; text-indent: 2em; }
blockquote { margin: 1em 0; padding: .3em 0 .3em 1em; border-left: 3px solid #d8d8dc;
             color: #55555a; }
blockquote p { text-indent: 0; }
img { max-width: 100%; height: auto; display: block; margin: 1em auto; }
pre { background: #f5f5f7; padding: .8em 1em; overflow-x: auto; border-radius: 6px; }
code { font-family: "SF Mono", Menlo, monospace; font-size: .92em;
       background: #f5f5f7; padding: .1em .3em; border-radius: 4px; }
pre code { background: none; padding: 0; }
hr { border: none; border-top: 1px solid #e2e2e6; margin: 1.6em 0; }
a { color: #0a6cff; text-decoration: none; }
u { text-decoration: underline; }
s { text-decoration: line-through; }
/* 表格显式写 display：写作页能插表格，而 EPUB 阅读器的默认样式表参差 ——
   有的把 table 当 block 排，格子就全糊成一行。*/
table { display: table; width: 100%; border-collapse: collapse; margin: 1em 0; }
thead { display: table-header-group; }
tbody { display: table-row-group; }
tr { display: table-row; }
th, td { display: table-cell; border: 1px solid #d8d8dc; padding: .35em .6em;
         vertical-align: top; text-align: left; }
th { background: #f5f5f7; font-weight: 600; }
ul { padding-left: 1.6em; }
li.task { list-style: none; position: relative; }
li.task::before { content: ""; position: absolute; left: -1.5em; top: .42em;
                  width: .85em; height: .85em; border: 1.5px solid #9a9aa0;
                  border-radius: 3px; }
li.task.done::before { border-color: #0a6cff; background: #0a6cff; }
.cover { height: 100%; display: flex; flex-direction: column; justify-content: center;
         align-items: center; text-align: center; padding: 8%; }
.cover h1 { font-size: 2em; margin: 0 0 .6em; text-indent: 0; }
.cover .author { color: #6c6c70; font-size: 1.05em; }
.cover img { max-width: 92%; max-height: 80%; }
"""


def _xhtml(title, body, extra_class=""):
    cls = (' class="%s"' % extra_class) if extra_class else ""
    return ("<?xml version=\"1.0\" encoding=\"utf-8\"?>\n"
            "<!DOCTYPE html>\n"
            "<html xmlns=\"http://www.w3.org/1999/xhtml\" "
            "xmlns:epub=\"http://www.idpf.org/2007/ops\" xml:lang=\"zh\" lang=\"zh\">\n"
            "<head><meta charset=\"utf-8\"/><title>%s</title>"
            "<link rel=\"stylesheet\" type=\"text/css\" href=\"style.css\"/></head>\n"
            "<body%s>\n%s\n</body>\n</html>\n" % (_esc(title), cls, body))


def build_epub(book_dir, dest, title="", author="", cover=None):
    """把 book_dir 打成一个 EPUB 3 文件，写到 dest。

    cover 是可选封面图路径（存在就嵌进去）。
    """
    chapters = read_chapters(book_dir)
    if not chapters:
        raise ValueError("这本书还没有可导出的章节")
    # 书名 / 作者进任何一份 XML 之前先过 _xml_clean：换页符之类的码点 XML 1.0 里
    # 没有合法写法，拼进 content.opf / cover.xhtml 就会让阅读器把整本书拒收。
    title = _xml_clean((title or os.path.basename(book_dir)).strip())
    author = _xml_clean((author or "").strip())
    modified = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    uid = "urn:uuid:" + str(uuid.uuid4())

    imgs = _book_images(book_dir)

    secs = []
    for i, (_fn, md) in enumerate(chapters):
        body = md_to_html(md).replace('src="images/', 'src="img/')
        secs.append({"href": "sec%04d.xhtml" % i,
                     "id": "sec%04d" % i,
                     "title": _first_heading(body, "第 %d 章" % (i + 1)),
                     "body": body})

    cover_file = os.path.basename(cover) if (cover and os.path.isfile(cover)) else None

    # 封面页：有封面图就显示图，没有就排一个文字封面（EPUB 允许）。
    if cover_file:
        cover_body = ('<div class="cover"><img src="img/%s" alt="封面"/></div>'
                      % _attr(cover_file))
    else:
        cover_body = ('<div class="cover"><h1>%s</h1>%s</div>'
                      % (_esc(title), ('<div class="author">%s</div>' % _esc(author)) if author else ""))

    nav_items = "".join('<li><a href="%s">%s</a></li>' % (s["href"], _esc(s["title"]))
                        for s in secs)
    nav = _xhtml("目录",
                 '<nav epub:type="toc" id="toc"><h1>目录</h1><ol>%s</ol></nav>' % nav_items)

    manifest = ['<item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>',
                '<item id="css" href="style.css" media-type="text/css"/>',
                '<item id="cover" href="cover.xhtml" media-type="application/xhtml+xml"/>']
    if cover_file:
        manifest.append('<item id="cover-img" href="img/%s" media-type="%s" properties="cover-image"/>'
                        % (_attr(cover_file), _MIME_BY_EXT.get(
                            os.path.splitext(cover_file)[1].lower(), "image/jpeg")))
    for s in secs:
        manifest.append('<item id="%s" href="%s" media-type="application/xhtml+xml"/>'
                        % (s["id"], s["href"]))
    for i, f in enumerate(imgs):
        # id 用序号而不是文件名：文件名来自 images/ 目录，空格 / 引号 / 点号
        # 都不是合法 XML ID 字符，拼进去就算转义过了也只是「没报错的不合法」。
        # 这个 id 谁都不引用（spine 只引 chapter），序号既稳定又合规。
        manifest.append('<item id="img%04d" href="img/%s" media-type="%s"/>'
                        % (i, _attr(f),
                           _MIME_BY_EXT.get(os.path.splitext(f)[1].lower(),
                                            "application/octet-stream")))

    spine = ['<itemref idref="cover"/>', '<itemref idref="nav"/>']
    spine += ['<itemref idref="%s"/>' % s["id"] for s in secs]

    opf = ("<?xml version=\"1.0\" encoding=\"utf-8\"?>\n"
           "<package xmlns=\"http://www.idpf.org/2007/opf\" version=\"3.0\" "
           "unique-identifier=\"bookid\" xml:lang=\"zh\">\n"
           "  <metadata xmlns:dc=\"http://purl.org/dc/elements/1.1/\">\n"
           "    <dc:identifier id=\"bookid\">%s</dc:identifier>\n"
           "    <dc:title>%s</dc:title>\n"
           "    <dc:language>zh</dc:language>\n"
           "%s"
           "    <meta property=\"dcterms:modified\">%s</meta>\n"
           "  </metadata>\n"
           "  <manifest>\n    %s\n  </manifest>\n"
           "  <spine>\n    %s\n  </spine>\n"
           "</package>\n"
           % (uid, _esc(title),
              ("    <dc:creator>%s</dc:creator>\n" % _esc(author)) if author else "",
              modified, "\n    ".join(manifest), "\n    ".join(spine)))

    container = ("<?xml version=\"1.0\" encoding=\"utf-8\"?>\n"
                 "<container version=\"1.0\" "
                 "xmlns=\"urn:oasis:names:tc:opendocument:xmlns:container\">\n"
                 "  <rootfiles>\n"
                 "    <rootfile full-path=\"OEBPS/content.opf\" "
                 "media-type=\"application/oebps-package+xml\"/>\n"
                 "  </rootfiles>\n</container>\n")

    tmp = dest + ".part"
    try:
        with zipfile.ZipFile(tmp, "w") as z:
            # mimetype 不压缩、放第一个：这是 EPUB 唯一的硬性要求
            zi = zipfile.ZipInfo("mimetype", time.localtime()[:6])
            zi.compress_type = zipfile.ZIP_STORED
            z.writestr(zi, "application/epub+zip")
            z.writestr("META-INF/container.xml", container, zipfile.ZIP_DEFLATED)
            z.writestr("OEBPS/content.opf", opf, zipfile.ZIP_DEFLATED)
            z.writestr("OEBPS/style.css", _EPUB_CSS, zipfile.ZIP_DEFLATED)
            z.writestr("OEBPS/nav.xhtml", nav, zipfile.ZIP_DEFLATED)
            z.writestr("OEBPS/cover.xhtml", _xhtml(title, cover_body, "cover"), zipfile.ZIP_DEFLATED)
            for s in secs:
                z.writestr("OEBPS/%s" % s["href"], _xhtml(s["title"], s["body"]),
                           zipfile.ZIP_DEFLATED)
            for f in imgs:
                z.write(os.path.join(book_dir, "images", f),
                        "OEBPS/img/%s" % f, zipfile.ZIP_DEFLATED)
            if cover_file:
                z.write(cover, "OEBPS/img/%s" % cover_file, zipfile.ZIP_DEFLATED)
        os.replace(tmp, dest)
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass
    return dest


# ─────────────────────────── PDF ───────────────────────────

_PDF_CSS = """\
@page { size: A4; }
* { box-sizing: border-box; }
body { font-family: "PingFang SC", "Songti SC", "Noto Serif CJK SC", serif;
       line-height: 1.85; color: #1c1c1e; }
.booktitle { text-align: center; margin: 26% 0 0; text-indent: 0; font-size: 2.1em;
             font-weight: 700; }
.bookauthor { text-align: center; color: #6c6c70; margin-top: 1.2em; text-indent: 0; }
.chapter { page-break-before: always; }
.chapter:first-of-type { page-break-before: avoid; }
h1 { font-size: 1.5em; margin: 1.2em 0 .8em; line-height: 1.4; }
h2 { font-size: 1.28em; margin: 1.2em 0 .6em; }
h3, h4, h5, h6 { font-size: 1.1em; margin: 1em 0 .5em; }
p { margin: 0 0 .9em; text-indent: 2em; }
blockquote { margin: 1em 0; padding: .3em 0 .3em 1em; border-left: 3px solid #d8d8dc;
             color: #55555a; }
blockquote p { text-indent: 0; }
img { max-width: 100%; height: auto; display: block; margin: 1em auto;
      page-break-inside: avoid; }
pre { background: #f5f5f7; padding: .8em 1em; border-radius: 6px; white-space: pre-wrap;
      word-break: break-word; }
code { font-family: "SF Mono", Menlo, monospace; font-size: .92em; }
hr { border: none; border-top: 1px solid #e2e2e6; margin: 1.6em 0; }
a { color: #0a6cff; text-decoration: none; }
u { text-decoration: underline; }
s { text-decoration: line-through; }
table { border-collapse: collapse; margin: 1em 0; }
th, td { border: 1px solid #d8d8dc; padding: .35em .6em; vertical-align: top;
         text-align: left; }
th { background: #f5f5f7; font-weight: 600; }
li.task { list-style: none; position: relative; }
li.task::before { content: ""; position: absolute; left: -1.5em; top: .42em;
                  width: .85em; height: .85em; border: 1.5px solid #9a9aa0;
                  border-radius: 3px; }
li.task.done::before { border-color: #0a6cff; background: #0a6cff; }
"""


def _abs_images(html_str, img_dir):
    """把 <img src="images/x"> 换成绝对 file:// —— 铬内核在 file:// 页面上
    加载同协议资源是允许的，图片才不会全空。

    `md_to_html` 交出来的属性值是**实体编码过**的（`a&b.png` 写成 `a&amp;b.png`，
    `a"b.png` 写成 `a&quot;b.png`），所以先 `html.unescape` 还原成磁盘上真正的
    文件名再去找 —— 少了这一步，带 `& < > "` 的图名一律「文件不存在」，PDF 里
    那张图就静默不见了（EPUB 那条路不走这里，用的是原始文件名）。
    换出来的 `file://` URI 由 `as_uri()` 自己按百分号编码，不会再带裸引号。
    """
    def rep(m):
        name = html.unescape(m.group(1))
        p = os.path.join(img_dir, os.path.basename(name))
        if os.path.isfile(p):
            return 'src="%s"' % Path(p).resolve().as_uri()
        return m.group(0)
    return re.sub(r'src="images/([^"]+)"', rep, html_str)


def build_pdf(book_dir, dest, title="", author=""):
    """铬内核把整本书打印成一个 PDF。铬没装好时抛异常，调用方先拦。

    注意 page.pdf() 不吃 timeout 参数（超时由 goto 那一跳控制），别往上加。"""
    chapters = read_chapters(book_dir)
    if not chapters:
        raise ValueError("这本书还没有可导出的章节")
    # 同 build_epub：打印页上的书名 / 作者也不许夹 XML 收不下的字符。
    title = _xml_clean((title or os.path.basename(book_dir)).strip())
    author = _xml_clean((author or "").strip())
    img_dir = os.path.join(book_dir, "images")

    parts = []
    for i, (_fn, md) in enumerate(chapters):
        body = md_to_html(md)
        body = re.sub(r"<hr\s*/?>", '<hr/>', body)
        body = _abs_images(body, img_dir)
        parts.append('<section class="chapter" id="ch%d">\n%s\n</section>' % (i, body))

    doc = ("<!DOCTYPE html>\n<html lang=\"zh\"><head><meta charset=\"utf-8\"/>"
           "<title>%s</title><style>%s</style></head><body>\n"
           "<h1 class=\"booktitle\">%s</h1>\n%s\n%s\n</body></html>"
           % (_esc(title), _PDF_CSS, _esc(title),
              ('<div class="bookauthor">%s</div>' % _esc(author)) if author else "",
              "\n".join(parts)))

    tmpdir = tempfile.mkdtemp(prefix="guizang-pdf-")
    try:
        html_path = os.path.join(tmpdir, "book.html")
        with open(html_path, "w", encoding="utf-8") as f:
            f.write(doc)
        from playwright.sync_api import sync_playwright
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            try:
                page = browser.new_page()
                page.goto(Path(html_path).resolve().as_uri(), timeout=120000)
                page.pdf(path=dest, format="A4", print_background=True,
                         margin={"top": "18mm", "bottom": "18mm",
                                 "left": "16mm", "right": "16mm"})
            finally:
                browser.close()
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
    return dest
