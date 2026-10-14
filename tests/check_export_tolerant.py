#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""导出的章节读取不许「一个坏字节带走整本书」。

为什么单独一份：书 → PDF / EPUB 这两条出口的第一步都是
`book_export.read_chapters`，它把 chapters/*.md 逐章读回内存。这些文件是用户能
直接编辑的文本（README 也这么写），也会被云同步、旧版本、别的编辑器写回来 ——
一个另存成 GBK 的章节、或一次断电留下的半截 UTF-8，旧写法（`open(..., encoding=
"utf-8")` 直接读）当场 UnicodeDecodeError，另外几章正文一起打不开：界面上的表现
是「这本书导不出来」，还指不出是哪一章的错。

钉四件事：
  A 读取：坏字节的章节照样读得出，正文其余部分原样，坏字节落成 U+FFFD；另有一
    条对照证明那份夹具**确实**按严格 UTF-8 解不开（防门禁空转）。
  B 不丢章：章节数、顺序、文件名一个不少 —— 修法不是「悄悄跳过坏文件」。
  C 成品：真打一个 EPUB，包里两章都在，好章一字不差；坏章进 md_to_html 不抛。
  D 回归：正常 UTF-8 的书读回来与严格解码逐字符一致，一个 U+FFFD 都不许多出。

不联网、不起浏览器、不 import 服务：解析用标准库 zipfile，全在系统临时目录里跑。
PDF 那一支要真铬内核，不在此列；但它与 EPUB 共用同一个 read_chapters，A–D 已经
守住这个入口。

跑法：python3 tests/check_export_tolerant.py
"""
import os
import pathlib
import shutil
import sys
import tempfile
import zipfile

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

import book_export          # noqa: E402

FAIL = []
REPL = "\ufffd"

GOOD1 = "# 好的一章\n\n这是干净的中文正文 🧭。\n"
GOOD2 = "# 好的二章\n\n第二章也没有坏字节。\n"
# 一节「另存为 GBK」的标题 + 一节正常 UTF-8 正文 + 一段半截字节：'坏' 的 GBK 编码
# 是 0xBB 0xB5，0xBB 在 UTF-8 里是续接字节、单独出现就是非法序列。正文故意留在
# UTF-8：这样才分得清「坏字节只坏那几个字」和「整章被丢弃」。
BAD = ("# 坏章\n\n".encode("gbk")
       + "这段文字是 UTF-8 写的。\n".encode("utf-8")
       + b"\xff\xfe not utf8 at all")


def chk(name, cond, extra=""):
    print(("PASS  " if cond else "FAIL  ") + name + ("" if cond else "   " + repr(extra)[:220]))
    if not cond:
        FAIL.append(name)


def write(path, data):
    with open(path, "wb") as f:
        f.write(data if isinstance(data, bytes) else data.encode("utf-8"))


tmp = tempfile.mkdtemp(prefix="guizang-export-tol-")
try:
    # ── 夹具：一本两好一坏的书 + 一本全好的书 ─────────────────────
    book = os.path.join(tmp, "book")
    ch = os.path.join(book, "chapters")
    os.makedirs(ch)
    write(os.path.join(ch, "0001.md"), GOOD1)
    write(os.path.join(ch, "0002.md"), BAD)
    write(os.path.join(ch, "0003.md"), GOOD2)
    write(os.path.join(ch, "notes.txt"), "不是章节，不许被读进来")
    os.makedirs(os.path.join(ch, "0004.md"))          # 与 .md 同名的目录：只跳过不炸

    clean_book = os.path.join(tmp, "clean")
    cch = os.path.join(clean_book, "chapters")
    os.makedirs(cch)
    write(os.path.join(cch, "0001.md"), GOOD1)
    write(os.path.join(cch, "0002.md"), GOOD2)

    # ── A 读取：坏字节不抛，好章原样 ─────────────────────────────
    strict_failed = False
    try:
        with open(os.path.join(ch, "0002.md"), encoding="utf-8") as f:
            f.read()
    except UnicodeDecodeError:
        strict_failed = True
    chk("对照：坏章节按严格 UTF-8 确实解不开（夹具不是空的）", strict_failed)

    got = book_export.read_chapters(book)
    names = [n for n, _ in got]
    chk("读取：坏字节不抛，五个 .md 里读回三个真章节",
        names == ["0001.md", "0002.md", "0003.md"], names)
    chk("不丢章：同名的 .md 目录只跳过、不许把整趟读取带走", len(got) == 3, names)
    chk("不误收：.txt 不是章节", all(n.endswith(".md") for n in names), names)

    body = dict(got)
    chk("读取：好的一章一字不差", body.get("0001.md") == GOOD1, body.get("0001.md"))
    chk("读取：好的二章一字不差", body.get("0003.md") == GOOD2, body.get("0003.md"))
    bad = body.get("0002.md", "")
    chk("读取：坏章节读得回来", isinstance(bad, str) and bad.startswith("#"), repr(bad)[:80])
    chk("读取：坏字节落成 U+FFFD 而不是被吞掉", REPL in bad, repr(bad)[:80])
    chk("读取：坏章节里能认出的中文照认", "这段文字是" in bad and "写的" in bad, repr(bad)[:80])

    # ── C 成品：真打一个 EPUB ────────────────────────────────────
    dest = os.path.join(tmp, "out.epub")
    book_export.build_epub(book, dest, title="容错样张", author="归藏")
    chk("成品：EPUB 落盘", os.path.isfile(dest))
    if os.path.isfile(dest):
        with zipfile.ZipFile(dest) as z:
            zip_names = z.namelist()
            nav = z.read("OEBPS/nav.xhtml").decode("utf-8")
            sec0 = z.read("OEBPS/sec0000.xhtml").decode("utf-8")
            sec1 = z.read("OEBPS/sec0001.xhtml").decode("utf-8")
            sec2 = z.read("OEBPS/sec0002.xhtml").decode("utf-8")
        chk("成品：三章都在包里（没有因为坏章少一章）",
            all("OEBPS/sec%04d.xhtml" % i in zip_names for i in range(3)), zip_names)
        chk("成品：目录里有三个条目",
            nav.count("<li><a href=") == 3, nav.count("<li><a href="))
        chk("成品：好的一章正文进了成品", "这是干净的中文正文" in sec0, sec0[:120])
        chk("成品：好的二章正文进了成品", "第二章也没有坏字节" in sec2, sec2[:120])
        chk("成品：坏章也进了成品且带替代符", REPL in sec1, sec1[:120])
        chk("成品：包里不留坏字节残片", "坏章" not in sec1, sec1[:120])

    # 坏章交给渲染层也不许抛（PDF 那一支的第一跳）
    try:
        book_export.md_to_html(bad)
        render_ok = True
    except Exception as e:                                   # noqa: BLE001
        render_ok = False
        render_err = e
    chk("成品：坏章正文能过 md_to_html 不抛",
        render_ok, "" if render_ok else repr(render_err))

    # ── D 回归：正常 UTF-8 的书一个字符都不动 ────────────────────
    clean = book_export.read_chapters(clean_book)
    chk("回归：干净的书章节数不变", len(clean) == 2, clean)
    chk("回归：干净的书正文与源码字符串完全相同",
        clean[0][1] == GOOD1 and clean[1][1] == GOOD2, [c[1] for c in clean])
    chk("回归：干净的书里不许出现替代符",
        all(REPL not in t for _n, t in clean))

    # 缺 chapters 目录 / 空目录都回空表，不许抛
    empty_book = os.path.join(tmp, "empty")
    os.makedirs(empty_book)
    chk("边界：没有 chapters 目录回空表", book_export.read_chapters(empty_book) == [])
    os.makedirs(os.path.join(empty_book, "chapters"))
    chk("边界：空 chapters 目录回空表",
        book_export.read_chapters(empty_book) == [])
finally:
    shutil.rmtree(tmp, ignore_errors=True)

print()
print("导出章节读取容错：" + ("全部通过" if not FAIL else "%d 项未过：%s" % (len(FAIL), FAIL)))
sys.exit(1 if FAIL else 0)
