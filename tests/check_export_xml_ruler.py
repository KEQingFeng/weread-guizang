#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""书名 / 作者 / 正文里「XML 1.0 的 Char 产生式收不下」的字符，一个都不许溜进成品。

为什么单开一份（`check_export_xml_chars.py` 已经钉过控制符与半个代理码点）
    那一份钉的是「手写清单里点到的三种」：C0、DEL、落单代理码点。可 XML 1.0 的
    Char 产生式排除的是**一整个集合**：

        Char ::= #x9 | #xA | #xD | [#x20-#xD7FF] | [#xE000-#xFFFD]
                 | [#x10000-#x10FFFF]

    在 BMP 里，除代理码点之外它只额外排掉 **U+FFFE / U+FFFF** 这两个非字符 ——
    而它们恰恰是手写清单最容易漏的两种：`json` 的 `"\uffff"` 是合法转义，云同步
    写回的 meta.json、从 PDF / 别的编码里粘过来的书名都可能带着它。旧写法
    `_CTRL_DROP = [\x00-\x08\x0b\x0c\x0e-\x1f\x7f]` 加上代理码点那把，一个都不管
    U+FFFE / U+FFFF：`zipfile.writestr` 把它们按 UTF-8 正常编出去（EF BF BF），
    于是导出的 content.opf / nav.xhtml / secNNNN.xhtml **不是合法 XML**，阅读器把
    整本书拒收，而这一趟导出返回成功、界面上看是「导出好了」。

为什么判据要按产生式的补集写、不逐个点名
    点名就得跟着规格补，漏一个就是同一场事故。现在两处（书的身份 `_xml_clean`、
    正文入口 `md_to_html`）共用同一把 `_XML_ILLEGAL`，本份门禁同时钉住它的两侧：
    非法的洗掉、合法的一个字不改（C1、U+FFFD、emoji 都是合法 XML，别被顺手洗了）。

钉住四件事（都在临时目录里真打一个 EPUB，不联网、不起浏览器）：
  A 脏元数据 / 脏章节名：title / author / 章节首行夹 U+FFFE / U+FFFF，成品每份 XML
    仍然解得动（真用 ElementTree 解），且摘掉的只有那几个非字符
  B 正文那条路：章节正文里的 U+FFFF 落不进 secNNNN.xhtml，合法的 U+FFFD / C1 /
    emoji 照旧在
  C 对照：旧的手写清单（C0 + 代理码点）在同一份输入上确实留下 U+FFFF、XML 解不动，
    免得这条门禁空转
  D 口径没被顺手改宽：`\t \n \r` 留着、DEL 在正文入口照旧洗掉（
    `check_export_placeholder` 从 0.9.x 起就钉着这一条）、None / 非字符串照旧
"""
import json
import os
import re
import shutil
import sys
import tempfile
import xml.etree.ElementTree as ET
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import book_export          # noqa: E402

FAIL = []
DC = "{http://purl.org/dc/elements/1.1/}"

# 旧的手写清单（改动前的 `_SURROGATE` + `_CTRL_DROP`），只用于 C 组对照。
OLD_REGEXES = (re.compile("[\ud800-\udfff]"),
               re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]"))


def chk(name, cond, extra=""):
    print(("PASS  " if cond else "FAIL  ") + name + ("" if cond else "   " + repr(extra)[:220]))
    if not cond:
        FAIL.append(name)


def make_book(root, md="# 第一章\n\n正文一句。\n", meta=None):
    """造一本最小的书：chapters/0001.md（+ 可选 meta.json），返回书目录。"""
    d = os.path.join(root, "bk")
    os.makedirs(os.path.join(d, "chapters"), exist_ok=True)
    with open(os.path.join(d, "chapters", "0001.md"), "w", encoding="utf-8") as f:
        f.write(md)
    if meta is not None:
        with open(os.path.join(d, "meta.json"), "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False)
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
    for name, text in files.items():
        try:
            ET.fromstring(text)
        except ET.ParseError as e:
            return "%s: %s" % (name, e)
    return None


def text_of(files, name, tag):
    try:
        return ET.fromstring(files[name]).find(".//" + tag).text
    except Exception:
        return None


TMP = tempfile.mkdtemp(prefix="guizang-xmlruler-")
try:
    # ---------- A：U+FFFE / U+FFFF 不许把成品打成非法 XML ----------
    d = make_book(os.path.join(TMP, "a"), md="# 第一章\uffff标题\n\n正文一句。\n",
                  meta={"title": "样\uffff张", "author": "作\ufffe者"})
    _dest, files = build(d, os.path.join(TMP, "a"), "样\uffff张", "作\ufffe者")
    bad = wellformed(files)
    chk("A1 书名 / 作者 / 章节名带 U+FFFE、U+FFFF：成品每份 XML 都解得动", bad is None, bad)
    opf = files["OEBPS/content.opf"]
    chk("A1 两个非字符被摘掉，剩下的字一个不动",
        "<dc:title>样张</dc:title>" in opf and "<dc:creator>作者</dc:creator>" in opf,
        opf[opf.find("<dc:title>"):opf.find("</dc:title>") + 11])
    chk("A1 解回来的可见文字就是摘干净的那一个",
        text_of(files, "OEBPS/content.opf", DC + "title") == "样张"
        and text_of(files, "OEBPS/content.opf", DC + "creator") == "作者")
    sec = [t for n, t in files.items() if "/sec" in n][0]
    chk("A1 章节首行那个 U+FFFF 也没进 secNNNN.xhtml",
        "\uffff" not in sec and "\ufffe" not in sec and "第一章标题" in sec, sec[:200])

    # ---------- B：正文里合法的那些别被顺手洗掉 ----------
    keep = "保留\ufffd替换符\x80\x9fC1与\U0001F600表情"
    d = make_book(os.path.join(TMP, "b"), md="# 第一章\n\n正文%s尾。\uffff\n" % keep)
    _dest, files = build(d, os.path.join(TMP, "b"), "正文样张")
    bad = wellformed(files)
    chk("B1 正文里的 U+FFFF 洗掉，成品解得动", bad is None, bad)
    sec = [t for n, t in files.items() if "/sec" in n][0]
    chk("B1 非字符落不进正文，合法的 U+FFFD / C1 / emoji 一个字不动",
        "\uffff" not in sec and keep in sec, sec[:260])

    # ---------- C：对照 —— 旧的手写清单确实漏掉这两个非字符 ----------
    old = "样\uffff张"
    for rx in OLD_REGEXES:
        old = rx.sub("", old)
    chk("C1 对照：旧尺子没摘掉 U+FFFF（输入原样留着）", old == "样\uffff张", repr(old))
    try:
        ET.fromstring("<t>%s</t>" % old)
        old_died = False
    except ET.ParseError:
        old_died = True
    chk("C1 对照：旧尺子留下的那份 XML 确实解不动（这条门禁不空转）", old_died, old)

    # ---------- D：口径没被顺手改宽 ----------
    chk("D1 尺子：非法的洗掉、`\\t \\n \\r` 与正常字一个不改",
        book_export._xml_clean("a\tb\nc\rd") == "a\tb\nc\rd"
        and book_export._xml_clean("样\u000c张\ud800尾\uffff完") == "样张尾完"
        and book_export._xml_clean("中\U0001F600\ufffd") == "中\U0001F600\ufffd"
        and book_export._xml_clean(None) == "" and book_export._xml_clean(12) == "12")
    chk("D2 正文入口照旧洗 DEL 与非字符（占位记号那一族的既有口径）",
        book_export.md_to_html("a\x00b\x01c\x08d\x0be\x0cf\x1fg\x7fh\uffffi")
        == "<p>abcdefghi</p>",
        book_export.md_to_html("a\x00b\x01c\x08d\x0be\x0cf\x1fg\x7fh\uffffi"))
finally:
    shutil.rmtree(TMP, ignore_errors=True)

print()
print("导出 XML Char 产生式取证：" + ("全部通过" if not FAIL else "%d 项未过：%s" % (len(FAIL), FAIL)))
sys.exit(1 if FAIL else 0)
