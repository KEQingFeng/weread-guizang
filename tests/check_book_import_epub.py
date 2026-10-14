# -*- coding: utf-8 -*-
"""EPUB 导入的配图归属：同名图不许串门，相对路径要按本文档解析。

`read_epub` 一直没门禁。它最容易出的事故不是「导不进来」，而是**导进来但配错图**：
落盘名由 `_unique_names` 对「包内路径」去重（`a/pic.png` → `pic.png`、
`b/pic.png` → `pic_2.png`），而正文里的引用原先只拿 `_safe_name(basename)` 算。
两篇文档各引一张 `pic.png` 时，正文两边都写 `images/pic.png` —— 第二章显示的是
第一章的图，不报任何错。修复是按「本文档所在目录」把相对路径解析成包内路径，
再查同一张落盘表。

这里只喂内存里现造的 EPUB（zipfile 手搓），不联网、不开浏览器、不碰真实书库。

跑法：.venv/bin/python tests/check_book_import_epub.py
"""
import atexit
import io
import os
import posixpath
import re
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

SANDBOX = tempfile.mkdtemp(prefix="gz-epub-check-")
atexit.register(lambda: shutil.rmtree(SANDBOX, ignore_errors=True))
os.environ["GUIZANG_DATA"] = os.path.join(SANDBOX, "data")

import book_import as B  # noqa: E402

PASSED = 0
FAIL = []


def chk(name, cond, extra=""):
    global PASSED
    if cond:
        PASSED += 1
        print("  ok  " + name)
    else:
        FAIL.append(name)
        print("  ✗  " + name + "   " + repr(extra))


def build_epub(docs, images, title="同名图"):
    """docs = [(href, xhtml)], images = [(包内路径, bytes)]"""
    buf = io.BytesIO()
    manifest, spine = [], []
    for i, (href, _html) in enumerate(docs):
        manifest.append('<item id="c%d" href="%s" media-type="application/xhtml+xml"/>'
                        % (i, href))
        spine.append('<itemref idref="c%d"/>' % i)
    for i, (path, _blob) in enumerate(images):
        manifest.append('<item id="i%d" href="%s" media-type="image/png"/>' % (i, path))
    opf = ('<?xml version="1.0"?><package xmlns="http://www.idpf.org/2007/opf">'
           '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">'
           '<dc:title>%s</dc:title><dc:creator>某人</dc:creator></metadata>'
           '<manifest>%s</manifest><spine>%s</spine></package>'
           % (title, "".join(manifest), "".join(spine)))
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("mimetype", "application/epub+zip")
        z.writestr("META-INF/container.xml",
                   '<?xml version="1.0"?><container><rootfiles>'
                   '<rootfile full-path="OEBPS/content.opf"/></rootfiles></container>')
        z.writestr("OEBPS/content.opf", opf)
        for href, html in docs:
            z.writestr("OEBPS/" + href, html)
        for path, blob in images:
            z.writestr("OEBPS/" + path, blob)
    return buf.getvalue()


def import_epub(data, name="书.epub"):
    dest = os.path.join(SANDBOX, "books")
    info = B.import_book(dest, name, data)
    return info["dir"], info


def chapter_text(book_dir):
    ch = os.path.join(book_dir, "chapters")
    return [open(os.path.join(ch, f), encoding="utf-8").read()
            for f in sorted(os.listdir(ch))]


def img_ref(md):
    m = re.search(r"!\[[^\]]*\]\(images/([^)]+)\)", md)
    return m.group(1) if m else ""


def page(title, body, src, alt="图"):
    return ('<html><body><h1>%s</h1><p>%s</p>'
            '<img src="%s" alt="%s"/></body></html>' % (title, body, src, alt))


# ── A. 纯函数：包内路径解析 ──────────────────────────────────────
NM = {"OEBPS/a/pic.png": "pic.png", "OEBPS/b/pic.png": "pic_2.png"}

chk("A1 同一目录下的相对引用按包内路径解析",
    B._img_flat("pic.png", NM, "OEBPS/a") == "pic.png",
    B._img_flat("pic.png", NM, "OEBPS/a"))
chk("A2 ../ 退回上一级后不会串到同名图",
    B._img_flat("../b/pic.png", NM, "OEBPS/a") == "pic_2.png",
    B._img_flat("../b/pic.png", NM, "OEBPS/a"))
chk("A3 根相对路径（/OEBPS/...）也认",
    B._img_flat("/OEBPS/b/pic.png", NM, "OEBPS/text") == "pic_2.png",
    B._img_flat("/OEBPS/b/pic.png", NM, "OEBPS/text"))
chk("A4 查询串 / 锚点不参与文件名",
    B._img_flat("pic.png?v=2#frag", NM, "OEBPS/a") == "pic.png",
    B._img_flat("pic.png?v=2#frag", NM, "OEBPS/a"))
chk("A5 只在文件名里找得到时退回文件名",
    B._img_flat("pic.png", NM, "OEBPS/elsewhere") in ("pic.png", "pic_2.png"),
    B._img_flat("pic.png", NM, "OEBPS/elsewhere"))
chk("A6 没给表时仍走 _safe_name 老口径",
    B._img_flat("a/b/my pic.png") == B._safe_name("my pic.png"),
    B._img_flat("a/b/my pic.png"))

# 中文名（HTML 里是百分号编码）要能对上包内的中文路径。
# 落盘名本身是 ASCII（`_safe_name` 把非 ASCII 换成下划线），这里用的是同一把尺子。
CN = {"OEBPS/img/封面.png": B._safe_name("封面.png")}
chk("A7 百分号编码的中文图名对得上（先 unquote 再查表）",
    B._img_flat("%E5%B0%81%E9%9D%A2.png", CN, "OEBPS/img") == "__.png",
    B._img_flat("%E5%B0%81%E9%9D%A2.png", CN, "OEBPS/img"))

# ── B. 归到 images/ 与网络图原样保留 ────────────────────────────
md = "![x](images/already.png) ![n](https://e.com/a.png) ![d](data:image/png;base64,AA)"
chk("B1 已经是 images/ 的、http(s):、data: 一律不动",
    B._rewrite_imgs(md, NM, "OEBPS/a") == md, B._rewrite_imgs(md, NM, "OEBPS/a"))

# ── C. 端到端：两篇文档同名图，各指各的 ─────────────────────────
data = build_epub(
    docs=[("text/ch1.xhtml", page("第一章", "AAA 正文", "../a/pic.png", "图A")),
          ("text/ch2.xhtml", page("第二章", "BBB 正文", "../b/pic.png", "图B"))],
    images=[("a/pic.png", b"\x89PNG-AAA"), ("b/pic.png", b"\x89PNG-BBB")])
book_dir, info = import_epub(data)
texts = chapter_text(book_dir)
r1, r2 = img_ref(texts[0]), img_ref(texts[1])
chk("C1 两章引用的是两个不同的落盘文件", r1 and r2 and r1 != r2, (r1, r2))
if r1 and r2:
    img_dir = os.path.join(book_dir, "images")
    b1 = open(os.path.join(img_dir, r1), "rb").read()
    b2 = open(os.path.join(img_dir, r2), "rb").read()
    chk("C2 第一章引用的确实是它自己的图（内容对得上）", b1 == b"\x89PNG-AAA", b1)
    chk("C3 第二章引用的确实是它自己的图（内容对得上）", b2 == b"\x89PNG-BBB", b2)
    chk("C4 两处 vendor 前缀仍是 images/，没写成裸文件名",
        texts[0].count("](images/") == 1 and texts[1].count("](images/") == 1,
        (texts[0], texts[1]))
else:
    chk("C2 第一章引用的确实是它自己的图（内容对得上）", False, texts)
    chk("C3 第二章引用的确实是它自己的图（内容对得上）", False, texts)
    chk("C4 两处前缀仍是 images/", False, texts)

# ── D. 端到端：普通同目录引用 + 根相对引用 + 缺图 ───────────────
data2 = build_epub(
    docs=[("text/ch1.xhtml", page("第一章", "一", "img/only.png")),
          ("text/ch2.xhtml", page("第二章", "二", "/OEBPS/img/only.png")),
          ("text/ch3.xhtml", page("第三章", "三", "img/missing.png"))],
    images=[("img/only.png", b"\x89ONLY")])
book_dir2, _ = import_epub(data2)
t2 = chapter_text(book_dir2)
r_only = img_ref(t2[0])
chk("D1 普通相对引用仍能归位且文件真在",
    r_only and os.path.isfile(os.path.join(book_dir2, "images", r_only)),
    (r_only, t2[0]))
chk("D2 根相对路径引到同一张图（同名同内容只存一份）",
    img_ref(t2[1]) == r_only and len(os.listdir(os.path.join(book_dir2, "images"))) == 1,
    (img_ref(t2[1]), os.listdir(os.path.join(book_dir2, "images"))))
chk("D3 引了包里没有的图：给出确定名字、不抛异常",
    img_ref(t2[2]) != "" and not os.path.isfile(
        os.path.join(book_dir2, "images", img_ref(t2[2]))),
    t2[2])

# ── E. 老行为一个都不许丢 ───────────────────────────────────────
chk("E1 EPUB 元信息仍认得出（dc:title / dc:creator）",
    info.get("title") == "同名图" and info.get("author") == "某人", info)
chk("E2 章节数与目录仍对得上",
    info.get("chapters") == 2 and info.get("format") == "epub", info)
chk("E3 图片计数没变（仍按实际落盘数）", info.get("images") == 2, info)

print("\nEPUB 导入配图体检：%d 项通过，失败 %d 项" % (PASSED, len(FAIL)))
for n in FAIL:
    print("  ✗ " + n)
sys.exit(1 if FAIL else 0)
