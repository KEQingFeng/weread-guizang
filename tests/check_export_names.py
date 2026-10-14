#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""成品里的图片文件名：带引号 / & / < 的图名不再把 EPUB 打坏、也不再从 PDF 里消失。

上一条（tests/check_export_safety.py）管的是「正文里的地址带引号会不会从属性里
逃出去」——那条线已经把 `md_to_html` 的 src / href / alt 收进 `_attrq` 了。可
成品里还有**另一处**属性值是直接来自磁盘的：`images/` 目录下的文件名。

下载器（download_images.safe_target）只挡路径分隔符、控制字符与落单的半个代理
码点，`& < > "` 都原样留着 —— 因为图片名本来就是从 `raw/*.json` 里读出来的网页
数据，不该假定它乖。于是：

  · EPUB（build_epub）把文件名拼进 `href="img/…"` / `id="img-…"`，用的却是
    `_esc`（`quote=False`，不动双引号）。一个 `a"b.jpg` 就让这一行提前收尾，
    `content.opf` 与 `cover.xhtml` 不再是合法 XML —— 阅读器拒收或封面空白。
  · PDF（build_pdf 的 `_abs_images`）拿 `md_to_html` 交出来的属性值去磁盘上找
    文件，而那条值是实体编码过的（`c&d.jpg` 写成 `c&amp;d.jpg`）：不解码就永远
    「文件不存在」，那张图在 PDF 里静默消失，正文里只剩一个空位。

钉住四件事：
  A 对照：证明老写法确实产出非法 XML（这组断言不许空转）。
  B EPUB 成品：真打一个带脏图名与脏封面的包，解析 content.opf / cover.xhtml /
    正文页，三份都必须是良构 XML，且 href 解码后能对上包里的原始条目名。
  C 回归：正常 ASCII / 中文图名一个字不改，id 唯一。
  D PDF 替换：`_abs_images` 能把实体编码过的脏图名换成 file://（解码后正对得上
    那个文件），缺图仍原样留在正文里。

不联网、不起浏览器、不需要 chromium：只走标准库的 zipfile 与 xml 解析。
"""
import os
import shutil
import sys
import tempfile
import urllib.parse
import xml.etree.ElementTree as ET
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import book_export as be          # noqa: E402

FAIL = []


def chk(name, cond, extra=""):
    print(("PASS  " if cond else "FAIL  ") + name + ("" if cond else "   " + repr(extra)[:220]))
    if not cond:
        FAIL.append(name)


# 每一份都能落成一个真文件：JPEG 魔数开头，扩展名也认得出来。
_JPEG = b"\xff\xd8\xff\xe0" + b"guizang"

# 图名 → 这个字会在哪个成品里咬人：
#   `"`  EPUB 属性提前收尾      `& < >`  PDF 属性实体编码后找不到文件
DIRTY = ['a"b.jpg', 'c&d.jpg', 'e<f>.jpg']
CLEAN = ["plain.jpg", "\u56fe1.png"]      # 正常英文名 / 中文名

TMP = tempfile.mkdtemp(prefix="guizang-export-names-")


def _write(path, data=_JPEG):
    with open(path, "wb") as f:
        f.write(data)


def _book():
    """一本书：chapters/ 里引用全部图名，images/ 里放着对应的真文件。"""
    book = os.path.join(TMP, "bk")
    os.makedirs(os.path.join(book, "chapters"))
    os.makedirs(os.path.join(book, "images"))
    names = DIRTY + CLEAN
    for n in names:
        _write(os.path.join(book, "images", n))
    md = "# \u7b2c\u4e00\u7ae0\n\n" + "\n\n".join(
        "![\u56fe](images/%s)" % n for n in names)
    with open(os.path.join(book, "chapters", "0001.md"), "w", encoding="utf-8") as f:
        f.write(md)
    return book, names


try:
    book, names = _book()
    cover = os.path.join(book, 'c"v.jpg')       # 封面文件名也带引号
    _write(cover)
    dest = os.path.join(TMP, "names.epub")
    be.build_epub(book, dest, title="脏图名样张", cover=cover)

    with zipfile.ZipFile(dest) as z:
        opf = z.read("OEBPS/content.opf").decode("utf-8")
        cover_x = z.read("OEBPS/cover.xhtml").decode("utf-8")
        sec = z.read("OEBPS/sec0000.xhtml").decode("utf-8")
        entries = z.namelist()

    # ── A. 对照：老写法（只 _esc，不挡双引号）确实产不出良构 XML ─────────
    old_line = ('<package><manifest>'
                '<item id="img-%s" href="img/%s" media-type="image/jpeg"/>'
                '</manifest></package>' % (be._esc(DIRTY[0]), be._esc(DIRTY[0])))
    old_raises = False
    try:
        ET.fromstring(old_line)
    except ET.ParseError:
        old_raises = True
    chk("对照：只 _esc 的老写法把双引号留在属性里，解析当场报错（这组不是空转）",
        old_raises, old_line)

    # ── B. EPUB：三份 XML 都必须良构，href 解码后对得上包里的原始条目名 ──
    parsed = {}
    for label, text in (("content.opf", opf), ("cover.xhtml", cover_x), ("正文页", sec)):
        try:
            ET.fromstring(text)
            parsed[label] = True
        except ET.ParseError as e:
            parsed[label] = False
            print("      %s 解析失败：%s" % (label, e))
    chk("EPUB：content.opf / cover.xhtml / 正文页三份都是良构 XML",
        all(parsed.values()), parsed)

    root = ET.fromstring(opf)
    items = [e for e in root.iter() if e.tag.endswith("item")]
    hrefs = [e.get("href") or "" for e in items]
    img_hrefs = sorted(h for h in hrefs if h.startswith("img/"))
    want = sorted("img/%s" % n for n in names + ['c"v.jpg'])
    chk("EPUB：每个图 / 封面都有 manifest 条目，且 href 解码后就是原始文件名（一个字不改）",
        img_hrefs == want, (img_hrefs, want))

    zip_imgs = sorted(os.path.basename(n) for n in entries if n.startswith("OEBPS/img/"))
    chk("EPUB：包里条目名仍是原始文件名（没有为了过 XML 而改名）",
        zip_imgs == sorted(names + ['c"v.jpg']), zip_imgs)

    ids = [e.get("id") for e in items]
    chk("EPUB：manifest 的 id 唯一（图名不再被拼进 id）",
        len(ids) == len(set(ids)) and len(ids) == len(items), ids)

    cover_root = ET.fromstring(cover_x)
    cover_srcs = [e.get("src") for e in cover_root.iter() if e.tag.endswith("img")]
    chk("EPUB：封面页的 src 解码后就是 img/c\"v.jpg",
        cover_srcs == ['img/c"v.jpg'], cover_srcs)

    # 正文页里的图引用也回到原始名（EPUB 用 images/ → img/ 的前缀替换）
    sec_root = ET.fromstring(sec)
    sec_srcs = sorted(e.get("src") for e in sec_root.iter()
                      if e.tag.endswith("img") and e.get("src"))
    chk("EPUB：正文页里带 & < 的图名仍是良构属性，解码后对得上",
        set(sec_srcs) <= set(want), sec_srcs)

    # ── C. 回归：正常文件名不被动过 ────────────────────────────────────────
    chk("回归：plain.jpg / 中文名照旧出现在 manifest 里",
        "img/plain.jpg" in img_hrefs and "img/\u56fe1.png" in img_hrefs, img_hrefs)

    # ── D. PDF：_abs_images 先解码实体，再找磁盘上的真文件 ────────────────
    img_dir = os.path.join(book, "images")
    html = be.md_to_html(open(os.path.join(book, "chapters", "0001.md"),
                              encoding="utf-8").read())
    out = be._abs_images(html, img_dir)
    chk("PDF：所有存在的图都换成了 file://（不再有落单的 images/ 相对路径）",
        'src="images/' not in out and out.count("file://") == len(DIRTY + CLEAN),
        (out.count("file://"), len(DIRTY + CLEAN)))

    uris = []
    for frag in out.split('src="')[1:]:
        u = frag.split('"', 1)[0]
        if u.startswith("file://"):
            uris.append(urllib.parse.unquote(urllib.parse.urlparse(u).path))
    chk("PDF：换出来的 file:// 解码后正指向 images/ 下那个文件（含脏名字）",
        sorted(os.path.basename(u) for u in uris) == sorted(DIRTY + CLEAN)
        and all(os.path.isfile(u) for u in uris), uris)

    chk("PDF：file:// URI 自己做过百分号编码（属性里不会带裸引号）",
        any("%22" in u for u in out.split('src="')[1:]) and '%22' in out, out[:300])

    # 缺图不许被顺手抹掉：老行为是原样留一行相对路径，交给铬内核显示成裂图。
    miss = be._abs_images('<img src="images/nope&quot;x.jpg" alt="x"/>', img_dir)
    chk("PDF：磁盘上没有这张图时原样留着，不抛也不悄悄删",
        miss == '<img src="images/nope&quot;x.jpg" alt="x"/>', miss)

    # 对照：不解码的老写法确实找不到这个文件（说明上面那条断言不是空转）
    naive = os.path.join(img_dir, os.path.basename("a&quot;b.jpg"))
    chk("对照：不解码的老写法找不到 a\"b.jpg（这组不是空转）",
        not os.path.isfile(naive) and os.path.isfile(os.path.join(img_dir, 'a"b.jpg')),
        naive)
finally:
    shutil.rmtree(TMP, ignore_errors=True)

print()
print("成品图片文件名口径：" + ("全部通过" if not FAIL else "%d 项未过：%s" % (len(FAIL), FAIL)))
sys.exit(1 if FAIL else 0)
