#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""书名 / 作者里「XML 收不下的字符」不许把导出毁在最后一步。

为什么单开一份
    一本书的身份（title / author）来自 meta.json —— 用户手改、云同步落半截、导入的
    书都写过它，而 `json` 连 `"\u000c"`（换页符）、`"\u000b"`（垂直制表符）这种
    转义都照收。它们落进 XML 就出事：XML 1.0 的 Char 产生式里根本没有这几个码点。
    正文那条路在 `book_export.md_to_html` 入口已经洗过一道（`_CTRL_DROP`），
    书名 / 作者却直接进 `_esc` —— 于是标题里一个换页符就让 `content.opf` 与
    `cover.xhtml`「不是合法 XML」，阅读器把整本书拒收，而导出这一步返回成功、
    界面上看是「导出好了」，用户点开是报错或空白。落单的半个代理码点更早：
    `zipfile.writestr` 在 `encode("utf-8")` 那一步就 UnicodeEncodeError，整趟打抛。

为什么不能只靠 `book_layout.read_meta`
    它摘代理码点，**不摘控制符**（那是另一件事）；而且导出这一侧还有别的标题来源
    （写作页的稿名、调用方直接传的 title）。所以收口落在「元数据进成品之前」那一步：
    `build_epub` / `build_pdf` 入口的 `_xml_clean`。`read_meta` 一个字不改。
    也不能把它塞进 `_esc` / `_attrq`：那两个函数坐在整份正文的转义管线里，正文中间
    夹着 `\x00N\x00` / `\x01N\x01` 占位记号，顺手把 C0 洗掉会连 `\#` `\*` 这些转义
    一起抹掉（E 组就是钉这一条的防回归）。

钉住四件事（都在临时目录里真打一个 EPUB，不联网、不起浏览器）：
  A 脏元数据：title / author 夹 C0 控制符或半个代理码点，成品 OPF 与三份 XHTML
    仍然是合法 XML（真用 ElementTree 解），且被摘掉的只有那几个非法字符
  B 正常元数据一个字不改：& < > 照文本转义，解回来的可见文字与原文一致
  C 对照：不洗的旧写法（html.escape 直上）在同一份输入上确实解不动 / 编不出，
    免得这条门禁空转
  D 正文那条路照旧：章节里的 C0 字节还是被丢掉，占位记号不串
  E 转义管线一个字没动：`\t \n \r` 留着、`_esc` 不碰占位记号、属性转义照旧
"""
import html
import json
import os
import shutil
import sys
import tempfile
import xml.etree.ElementTree as ET
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import book_export          # noqa: E402
import book_layout          # noqa: E402

FAIL = []
XHTML = "{http://www.w3.org/1999/xhtml}"
DC = "{http://purl.org/dc/elements/1.1/}"


def chk(name, cond, extra=""):
    print(("PASS  " if cond else "FAIL  ") + name + ("" if cond else "   " + repr(extra)[:220]))
    if not cond:
        FAIL.append(name)


def make_book(root, md="# 第一章\n\n正文一句。\n", meta=None, meta_ascii=False):
    """造一本最小的书：chapters/0001.md（+ 可选 meta.json），返回书目录。

    `meta_ascii=True` 时按 JSON 的转义写法落盘（落单的半个代理码点编不进 utf-8，
    只能以 `"\\ud800"` 这种转义住在文件里 —— 手改 / 云同步落下来的正是这个形状）。
    """
    d = os.path.join(root, "bk")
    os.makedirs(os.path.join(d, "chapters"), exist_ok=True)
    with open(os.path.join(d, "chapters", "0001.md"), "w", encoding="utf-8") as f:
        f.write(md)
    if meta is not None:
        with open(os.path.join(d, "meta.json"), "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=meta_ascii)
    return d


def build(d, root, title, author=""):
    dest = os.path.join(root, "out.epub")
    if os.path.exists(dest):
        os.remove(dest)
    book_export.build_epub(d, dest, title=title, author=author)
    with zipfile.ZipFile(dest) as z:
        return dest, {n: z.read(n).decode("utf-8")
                      for n in z.namelist() if n.endswith((".opf", ".xhtml"))}


def wellformed(files):
    """包里每份 XML 都真解一遍；解不动就把「哪一份、为什么」回给调用方。"""
    for name, text in files.items():
        try:
            ET.fromstring(text)
        except ET.ParseError as e:
            return "%s: %s" % (name, e)
    return None


def text_of(files, name, tag):
    """从成品里取一个元素的可见文字；解不动就回 None（让断言自己报 FAIL，不抛栈）。"""
    try:
        return ET.fromstring(files[name]).find(".//" + tag).text
    except Exception:
        return None


TMP = tempfile.mkdtemp(prefix="guizang-xmlchars-")
try:
    # ---------- A1：控制符（换页符 / 垂直制表符 / NUL）不许进成品 ----------
    d = make_book(os.path.join(TMP, "a1"))
    _dest, files = build(d, os.path.join(TMP, "a1"), "样张\u000c尾注", "作\u000b者\x00")
    bad = wellformed(files)
    chk("A1 脏元数据：成品里每份 XML 都解得动（换页符 / 垂直制表符 / NUL）", bad is None, bad)
    opf = files["OEBPS/content.opf"]
    cover = files["OEBPS/cover.xhtml"]
    chk("A1 书名 / 作者里的非法字符被摘掉，剩下的字一个不动",
        "<dc:title>样张尾注</dc:title>" in opf and "<dc:creator>作者</dc:creator>" in opf,
        opf[opf.find("<dc:title>"):opf.find("</dc:title>") + 11])
    chk("A1 封面页里的书名同样干净", "<h1>样张尾注</h1>" in cover
        and "\x0c" not in cover and "\x0b" not in cover)
    chk("A1 成品字节里再也不出现那几个控制符",
        not any(c in opf for c in ("\x0c", "\x0b", "\x00"))
        and not any("\x0c" in t or "\x00" in t for t in files.values()))

    # ---------- A2：meta.json 里落单的半个代理码点，端到端两处都兜住 ----------
    d = make_book(os.path.join(TMP, "a2"),
                  meta={"title": "样\ud800张", "author": "作\udfff者"}, meta_ascii=True)
    meta = book_layout.read_meta(d)
    chk("A2 读 meta 这一层先把半个代理码点摘掉（已有的收口没被绕过）",
        meta["title"] == "样张" and meta["author"] == "作者", meta)
    _dest, files = build(d, os.path.join(TMP, "a2"), meta["title"], meta["author"])
    bad = wellformed(files)
    chk("A2 落单代理码点：导出不再 UnicodeEncodeError，成品也解得动", bad is None, bad)
    chk("A2 解回来的书名与清洗后的一致",
        text_of(files, "OEBPS/content.opf", DC + "title") == "样张")

    # ---------- A3：从 meta.json 手改的换页符能一路走到导出（证明这条线真通） ----------
    d = make_book(os.path.join(TMP, "a3"),
                  meta={"title": "从 PDF 粘来的\u000c书名", "author": "某\u000b作者"})
    meta = book_layout.read_meta(d)
    chk("A3 read_meta 保留控制符（它是另一件事，这次收口不越界去改它）",
        "\x0c" in meta["title"], repr(meta["title"]))
    _dest, files = build(d, os.path.join(TMP, "a3"), meta["title"], meta["author"])
    bad = wellformed(files)
    chk("A3 手改 meta.json 进来的换页符不再把 content.opf / cover.xhtml 打坏", bad is None, bad)
    chk("A3 解回来的书名是摘掉换页符之后的那一个",
        text_of(files, "OEBPS/content.opf", DC + "title") == "从 PDF 粘来的书名")

    # ---------- B：正常元数据一个字不改 ----------
    d = make_book(os.path.join(TMP, "b"))
    raw_title, raw_author = 'A & B <C> "D"', "E & F"
    _dest, files = build(d, os.path.join(TMP, "b"), raw_title, raw_author)
    opf = files["OEBPS/content.opf"]
    chk("B 正常书名：& < > 照旧转义", 'A &amp; B &lt;C&gt; "D"' in opf, opf[opf.find("<dc:title>"):][:80])
    chk("B 解回来的可见文字与原文一模一样",
        text_of(files, "OEBPS/content.opf", DC + "title") == raw_title
        and text_of(files, "OEBPS/content.opf", DC + "creator") == raw_author)
    chk("B 封面页 h1 里也是同一份转义后的文字", "<h1>A &amp; B &lt;C&gt; \"D\"</h1>" in files["OEBPS/cover.xhtml"])

    # ---------- C：对照 —— 不洗的旧写法确实会坏，门禁不空转 ----------
    old = "<t>%s</t>" % html.escape("样\u000c张", quote=False)
    try:
        ET.fromstring(old)
        old_died = False
    except ET.ParseError:
        old_died = True
    chk("C 对照：html.escape 直上（没有 _xml_clean）的那份确实解不动", old_died, old)
    try:
        "样\ud800张".encode("utf-8")
        surro_died = False
    except UnicodeEncodeError:
        surro_died = True
    chk("C 对照：落单的半个代理码点确实编不进 utf-8", surro_died)

    # ---------- D：正文那条路照旧 ----------
    d = make_book(os.path.join(TMP, "d"), md="# 第一章\x0c\n\n正文\x01 0 \x01掉一下结尾。\n")
    _dest, files = build(d, os.path.join(TMP, "d"), "正文样张")
    bad = wellformed(files)
    chk("D 正文里的 C0 字节照旧在入口被丢掉，成品每份 XML 都解得动", bad is None, bad)
    sec = [t for n, t in files.items() if "/sec" in n][0]
    chk("D 正文里不再有控制字节，也不剩占位记号",
        "\x01" not in sec and "\x0c" not in sec and "掉一下结尾" in sec, sec[:200])

    # ---------- E：只摘非法的，转义管线一个字没动 ----------
    chk("E 书的身份那道尺子：只摘 XML / utf-8 收不下的，正常字一个不改",
        book_export._xml_clean("样\u000c张\ud800尾") == "样张尾"
        and book_export._xml_clean("a\tb\nc\rd") == "a\tb\nc\rd"
        and book_export._xml_clean(None) == "" and book_export._xml_clean(12) == "12")
    chk("E 正文的转义管线没被碰：_esc 不洗占位记号（洗了就轮到 \\# \\* 消失）",
        book_export._esc("\x010\x01#") == "\x010\x01#"
        and book_export._esc("a\tb\nc\rd") == "a\tb\nc\rd"
        and book_export._esc(None) == "" and book_export._esc(12) == "12")
    chk("E 属性那条路照旧只补引号 + 转 &<>",
        book_export._attr('a"b&c.jpg') == 'a&quot;b&amp;c.jpg')
finally:
    shutil.rmtree(TMP, ignore_errors=True)

print()
print("导出元数据 XML 字符取证：" + ("全部通过" if not FAIL else "%d 项未过：%s" % (len(FAIL), FAIL)))
sys.exit(1 if FAIL else 0)
