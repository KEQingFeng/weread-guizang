# -*- coding: utf-8 -*-
"""把取回的书导出成 EPUB / PDF。

两条路都刻意不引新依赖：

- EPUB 纯标准库手搓（zipfile）。规矩只有一条硬性的 —— `mimetype` 必须是
  压缩包里的第一个条目、且以 STORED（不压缩）方式存放，否则阅读器拒收。
- PDF 借已经装好的 Playwright 铬内核打印。前端本来就靠它取书，多这一个用法
  不用再让用户装东西；铬没装好时由调用方先拦一道（见 ui_server.chromium_ready）。

正文是我们自己抓下来的，语法面很窄（标题、图片、粗斜体、行内码、列表、引用、
代码块、分隔线、链接），所以这里手写一个够用的 Markdown → HTML，而不是为了
一个子集去拖进一个第三方库、再把它塞进安装包。
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


# ─────────────────────────── 极简 Markdown → HTML ───────────────────────────

_INLINE_CODE = re.compile(r"`([^`]+)`")
_IMG = re.compile(r"!\[([^\]]*)\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
_LINK = re.compile(r"\[([^\]]+)\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
_BOLD = re.compile(r"\*\*([^*]+)\*\*")
_ITAL = re.compile(r"(?<!\*)\*([^*]+)\*(?!\*)")
_HR = re.compile(r"(-{3,}|\*{3,}|_{3,})")
_UL = re.compile(r"[-*+]\s+")
_OL = re.compile(r"\d+[.)]\s+")


def _esc(s):
    return html.escape(str(s if s is not None else ""), quote=False)


def _inline(text):
    """行内语法。先转义再替换 —— 转义只动 &<>，不管是 * ` [] 还是括号，
    所以标记符号原样在，替换后插进去的 HTML 也不会被二次转义。"""
    text = _esc(text)
    stash = []

    def _keep(m):
        stash.append(m.group(1))
        return "\x00%d\x00" % (len(stash) - 1)

    text = _INLINE_CODE.sub(_keep, text)
    text = _IMG.sub(lambda m: '<img src="%s" alt="%s"/>' % (m.group(2), m.group(1)), text)
    text = _LINK.sub(lambda m: '<a href="%s">%s</a>' % (m.group(2), m.group(1)), text)
    text = _BOLD.sub(lambda m: "<strong>%s</strong>" % m.group(1), text)
    text = _ITAL.sub(lambda m: "<em>%s</em>" % m.group(1), text)
    text = re.sub(r"\x00(\d+)\x00",
                  lambda m: "<code>%s</code>" % stash[int(m.group(1))], text)
    return text


def md_to_html(md):
    """块级语法。返回一段一段的 HTML，句首句尾都不带换行，方便拼。"""
    lines = (md or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    out = []
    i, n = 0, len(lines)
    while i < n:
        s = lines[i].strip()
        if not s:
            i += 1
            continue
        # 围栏代码块
        if s.startswith("```"):
            lang = s[3:].strip()
            buf = []
            i += 1
            while i < n and not lines[i].strip().startswith("```"):
                buf.append(lines[i])
                i += 1
            i += 1
            cls = ' class="lang-%s"' % _esc(lang) if lang else ""
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
        # 无序 / 有序列表
        m = _UL.match(s)
        if m:
            buf = []
            while i < n and _UL.match(lines[i].strip()):
                buf.append(_UL.sub("", lines[i].strip(), count=1))
                i += 1
            out.append("<ul>%s</ul>" % "".join("<li>%s</li>" % _inline(x) for x in buf))
            continue
        if _OL.match(s):
            buf = []
            while i < n and _OL.match(lines[i].strip()):
                buf.append(_OL.sub("", lines[i].strip(), count=1))
                i += 1
            out.append("<ol>%s</ol>" % "".join("<li>%s</li>" % _inline(x) for x in buf))
            continue
        # 段落：一直吃到下一个块级起点
        buf = [s]
        i += 1
        while i < n:
            nxt = lines[i].strip()
            if (not nxt or nxt.startswith("```") or nxt.startswith(">")
                    or _HR.fullmatch(nxt) or re.match(r"#{1,6}\s+", nxt)
                    or _UL.match(nxt) or _OL.match(nxt)):
                break
            buf.append(nxt)
            i += 1
        out.append("<p>%s</p>" % "<br/>".join(_inline(x) for x in buf))
    return "\n".join(out)


# ─────────────────────────── 公共小工具 ───────────────────────────

def read_chapters(book_dir):
    """(文件名, 正文) 顺序读回逐章 md。"""
    ch_dir = os.path.join(book_dir, "chapters")
    if not os.path.isdir(ch_dir):
        return []
    out = []
    for fn in sorted(f for f in os.listdir(ch_dir) if f.endswith(".md")):
        try:
            with open(os.path.join(ch_dir, fn), encoding="utf-8") as f:
                out.append((fn, f.read()))
        except OSError:
            pass
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
    title = (title or os.path.basename(book_dir)).strip()
    author = (author or "").strip()
    modified = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    uid = "urn:uuid:" + str(uuid.uuid4())

    imgs = _book_images(book_dir)
    img_names = {f: "img/%s" % f for f in imgs}

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
                      % _esc(cover_file))
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
                        % (_esc(cover_file), _MIME_BY_EXT.get(
                            os.path.splitext(cover_file)[1].lower(), "image/jpeg")))
    for s in secs:
        manifest.append('<item id="%s" href="%s" media-type="application/xhtml+xml"/>'
                        % (s["id"], s["href"]))
    for f in imgs:
        manifest.append('<item id="img-%s" href="img/%s" media-type="%s"/>'
                        % (_esc(f), _esc(f),
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
"""


def _abs_images(html_str, img_dir):
    """把 <img src="images/x"> 换成绝对 file:// —— 铬内核在 file:// 页面上
    加载同协议资源是允许的，图片才不会全空。"""
    def rep(m):
        p = os.path.join(img_dir, os.path.basename(m.group(1)))
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
    title = (title or os.path.basename(book_dir)).strip()
    author = (author or "").strip()
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
