#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""导出渲染的占位记号不许被正文里的控制字节撞上。

为什么单独一份：`book_export.md_to_html` 是**所有成品**（导出的 HTML、EPUB、
PDF、写作页的预览与截图）共用的正文渲染口。它内部拿两个 C0 控制字节当占位记号：

    _inline     行内代码先收进 stash，原地留 \\x00 序号 \\x00
    _stash_line 行首的 \\X 转义收进 keep，原地留 \\x01 序号 \\x01

正文却是外部内容 —— 导入的书、剪藏回来的网页、用户手改或云同步写回的章节。
0x00 / 0x01 都是合法 UTF-8，`read_chapters` 一个字都不换地读进来，于是正文里
**凑巧**一段「控制字节 + 几位数字 + 控制字节」就会被当成自己的占位：

  ① 序号越界 —— `keep[int(...)]` / `stash[int(...)]` 当场 IndexError，整趟导出
     抛穿到界面上就是「这本书导不出来」，PDB 也指不出是哪一章；
  ② 序号落在范围内 —— 更阴：一段正文被悄悄替换成另一个转义字符，导出的成品
     多出一个不该有的记号，且没有任何报错。

钉五件事：
  A 崩点：带这两种脏形状的正文（同一篇里还要有一处真转义，让 keep / stash 非空）
    进 md_to_html 不抛；另有一组对照，按旧口径重演同一段文本确实越界，防门禁空转。
  B 不悄悄替换：真转义只还原一次，脏段落里的数字留下、控制字节洗掉，两侧正文都在。
  C 回归：不含控制字节的稿子逐字不变（标题 / 粗体 / 行内码 / 转义 / 制表 / 换行）。
  D 洗的口径：\t 与换行保留，其余 C0 与 DEL 一次洗掉。
  E 成品：真打一个 EPUB，两个章节都在、脏章正文仍在、包里的 XHTML 一个控制字节
    都没有（C0 控制符本来也不合法于 XHTML，落进成品可能被阅读器拒收）。

不联网、不起浏览器、不 import 服务：解析用标准库 zipfile，全在系统临时目录里跑。
跑法：python3 tests/check_export_placeholder.py
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

import book_export  # noqa: E402

FAIL = []
CTRL = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def chk(name, cond, extra=""):
    print(("PASS  " if cond else "FAIL  ") + name
          + ("" if cond else "   " + repr(extra)[:220]))
    if not cond:
        FAIL.append(name)


# ── A 崩点：正文里的占位形状 + 同篇一处真转义 ────────────────────────
# \x019\x01 这段数字远超本文里真转义的条数（只有一处 \*），旧口径必然越界。
DIRTY_ESC = "转义\\* 号\x019\x01结束"
DIRTY_CODE = "`x` 举例 \x009\x00"

old_bad = []
for text in (DIRTY_ESC, DIRTY_CODE):
    # 对照：按旧口径重演那两行还原（正文不洗就先进正则），确认这套夹具真能越界。
    try:
        if "\x01" in text:
            re.sub(r"\x01(\d+)\x01", lambda m: ["*"][int(m.group(1))], text)
        else:
            re.sub(r"\x00(\d+)\x00", lambda m: ["x"][int(m.group(1))], text)
    except IndexError:
        old_bad.append(text)
chk("对照：这两段脏正文按旧口径确实越界（门禁不是空转）",
    len(old_bad) == 2, old_bad)

for text in (DIRTY_ESC, DIRTY_CODE):
    err = None
    try:
        book_export.md_to_html(text)
    except Exception as e:  # noqa: BLE001
        err = e
    chk("A 脏正文进渲染不抛：" + repr(text)[:24], err is None, err)


# ── B 不悄悄替换：真转义只还原一次，正文都还在 ─────────────────────
out = book_export.md_to_html(DIRTY_ESC)
chk("B1 真转义只还原一次，脏段落不许再冒出一个 *", out.count("*") == 1, out)
chk("B2 只洗控制字节，两侧正文与数字都留着",
    "转义" in out and "号" in out and "9" in out and "结束" in out, out)
out2 = book_export.md_to_html(DIRTY_CODE)
chk("B3 行内代码照样成对，脏形状只洗不替换",
    out2 == "<p><code>x</code> 举例 9</p>", out2)


# ── C 回归：干净稿子逐字不变 ─────────────────────────────────────
CLEAN = "# 标题\n\n这段有 **粗体** 和 `代码`，还有转义 \\# 号。\n"
chk("C1 干净稿子的渲染结果逐字不变",
    book_export.md_to_html(CLEAN) ==
    "<h1>标题</h1>\n<p>这段有 <strong>粗体</strong> 和 <code>代码</code>，"
    "还有转义 # 号。</p>",
    book_export.md_to_html(CLEAN))
chk("C2 制表与换行保留，换行还按 markdown 折成 <br/>",
    book_export.md_to_html("甲\t乙\n丙\r\n丁") == "<p>甲\t乙<br/>丙<br/>丁</p>",
    book_export.md_to_html("甲\t乙\n丙\r\n丁"))


# ── D 洗的口径 ───────────────────────────────────────────────────
chk("D1 其余 C0 与 DEL 一次洗掉",
    book_export.md_to_html("a\x00b\x01c\x08d\x0be\x0cf\x1fg\x7fh") == "<p>abcdefgh</p>",
    book_export.md_to_html("a\x00b\x01c\x08d\x0be\x0cf\x1fg\x7fh"))
chk("D2 围栏代码块里的控制字节同样洗掉（渲染口只有一处收口）",
    book_export.md_to_html("```\n裸字节 \x01 与 \x00 都在\n```") ==
    "<pre><code>裸字节  与  都在</code></pre>",
    book_export.md_to_html("```\n裸字节 \x01 与 \x00 都在\n```"))


# ── E 成品：真打一个 EPUB ─────────────────────────────────────────
tmp = tempfile.mkdtemp(prefix="guizang-export-ph-")
try:
    book = os.path.join(tmp, "book")
    ch = os.path.join(book, "chapters")
    os.makedirs(ch)
    # 盘上真有一个 0x01 字节（合法 UTF-8），read_chapters 会原样读进来。
    with open(os.path.join(ch, "0001.md"), "wb") as f:
        f.write(DIRTY_ESC.encode("utf-8"))
    with open(os.path.join(ch, "0002.md"), "wb") as f:
        f.write("第二章干干净净。\n".encode("utf-8"))

    read_back = book_export.read_chapters(book)
    chk("E1 夹具：0x01 真的从盘上读进了渲染口（不是被读取层挡掉的）",
        "\x01" in read_back[0][1], repr(read_back[0][1]))

    dest = os.path.join(tmp, "out.epub")
    build_err = None
    try:
        book_export.build_epub(book, dest, title="占位样张", author="归藏")
    except Exception as e:  # noqa: BLE001
        build_err = e
    chk("E2 坏形状的章节照样打得成 EPUB", build_err is None, build_err)

    if build_err is None:
        with zipfile.ZipFile(dest) as z:
            names = z.namelist()
            secs = [n for n in names if n.startswith("OEBPS/sec")]
            bodies = [z.read(n).decode("utf-8") for n in secs]
        chk("E3 两个章节都在，一个不少", len(secs) == 2, names)
        chk("E4 脏章正文还在（洗的是字节，不是这一章）",
            any("转义* 号9结束" in b for b in bodies), bodies)
        chk("E5 包里的 XHTML 一个 C0 控制字节都没有",
            all(CTRL.search(b) is None for b in bodies), bodies)
finally:
    shutil.rmtree(tmp, ignore_errors=True)

print()
print("导出渲染占位收口：" + ("全部通过" if not FAIL
                             else "%d 项未过：%s" % (len(FAIL), FAIL)))
sys.exit(1 if FAIL else 0)
