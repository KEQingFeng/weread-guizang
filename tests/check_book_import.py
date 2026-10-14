# -*- coding: utf-8 -*-
"""book_import 的离线自测：编码认不错、格式认得出、PDF 硬换行不粘词、导进来的书结构齐全。

全程在系统临时目录的沙盒里跑，不联网、不开浏览器、不碰用户真实书库：

  · `_decode_text` 的编码识别。重点是「无 BOM 的 utf-16 不许抢在 gb18030 前头」——
    它几乎不会报错，却会拿两个字节当一个字硬解，把整篇 GBK 中文解成别的字
    （「中文测试」→「탖쓎」，8 个字节刚好是合法 utf-16）。同时守住 utf-8、带 BOM 的
    utf-8、带与不带 BOM 的 utf-16 都还认得出来。
  · `guess_format`：扩展名看不出时按文件头兜底（%PDF、PK + mimetype）。
  · `import_book` 落盘：GBK 的 TXT 导进来，章标题与正文都得是原字，meta / 目录齐全。
  · PDF 抽出来的硬换行：`_normalize_lines` 要把「同一段被硬换行劈开」的行拼回去。
    老实现是一律直接相加，于是

        The quick brown / fox jumps over / the lazy dog.

    变成了 `The quick brownfox jumps overthe lazy dog.` —— 整本英文 PDF 的词全粘在一起。
    中文反过来：字间本无空格，拼回去时补空格才是错的。

跑法：.venv/bin/python tests/check_book_import.py
"""
import atexit
import io
import json
import os
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

SANDBOX = tempfile.mkdtemp(prefix="gz-import-check-")
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
        print("  ✗  " + name + ("" if cond else "   " + repr(extra)))


# ── A. PDF 硬换行 · 英文：词之间补空格 ──────────────────────────────
chk("英文：词间接缝补一个空格",
    B._normalize_lines("The quick brown\nfox jumps over\nthe lazy dog.")
    == "The quick brown fox jumps over the lazy dog.")
chk("英文：行尾是逗号也补空格",
    B._normalize_lines("This is a long\nsentence, and it\ncontinues here.")
    == "This is a long sentence, and it continues here.")
chk("英文：数字与字母之间同样看待",
    B._normalize_lines("Chapter 12\nthe year 2026") == "Chapter 12 the year 2026")
chk("英文：一个空格都不多不少（不会补成两个）",
    B._normalize_lines("hello\nworld") == "hello world")

# ── B. PDF 硬换行 · 中文 / 全角标点：接缝不补空格 ───────────────────
chk("中文：字间不补空格",
    B._normalize_lines("第一章\n这是中文段落\n继续接着写。")
    == "第一章这是中文段落继续接着写。")
chk("中文：全角引号与标点间不补空格",
    B._normalize_lines("他说：“今天\n天气不错。”") == "他说：“今天天气不错。”")
chk("中文：中文收尾接英文开头也不补（保持原样）",
    B._normalize_lines("正文结束\nNext line here") == "正文结束Next line here")
chk("中文：英文收尾接中文开头也不补",
    B._normalize_lines("see the note\n就是这句") == "see the note就是这句")
chk("中文：全角逗号开头不补空格",
    B._normalize_lines("hello,\n你好") == "hello,你好")

# ── C. PDF 硬换行 · 连字符换行 ─────────────────────────────────────
chk("连字符：multi- / agent 拼成 multi-agent（不加空格）",
    B._normalize_lines("multi-\nagent systems\nare useful.")
    == "multi-agent systems are useful.")
chk("连字符：破折号 -- 与 en dash 同理",
    B._normalize_lines("state-of-\u2013\nthe-art") == "state-of-\u2013the-art")

# ── D. PDF 硬换行 · 括号 / 引号边界 ────────────────────────────────
chk("括号：左括号结尾不加空格",
    B._normalize_lines("word\n(note) here") == "word (note) here")
chk("括号：右括号开头不加空格",
    B._normalize_lines("first (one)\n) tail") == "first (one)) tail")
chk("标点：右括号 / 逗号开头不补空格",
    B._normalize_lines("hello\n, world") == "hello, world")
chk("引号：上引号结尾不加空格",
    B._normalize_lines("He said \u201chello\nworld\u201d today.")
    == "He said \u201chello world\u201d today.")

# ── E. PDF 硬换行 · 老行为一个都不许丢 ─────────────────────────────
chk("断段：句末标点仍把两行分成两段",
    B._normalize_lines("Hello.\nNew sentence.") == "Hello.\nNew sentence.")
chk("断段：中文句号同样断段",
    B._normalize_lines("第一句。\n第二句。") == "第一句。\n第二句。")
chk("空行：段落之间仍留一个空行",
    B._normalize_lines("first paragraph\n\nsecond line\nwrapped.")
    == "first paragraph\n\nsecond line wrapped.")
chk("归一：CRLF / CR 仍按换行处理",
    B._normalize_lines("a\r\nb\rc") == "a b c")
chk("空输入不炸", B._normalize_lines("") == "" and B._normalize_lines(None) == "")

# ── F. 真走一遍 import_book 的 PDF 分支 ────────────────────────────
TMP = tempfile.mkdtemp(prefix="guizang-import-")
real_pdf_text = B._pdf_text
try:
    body = ("The quick brown\nfox jumps over\nthe lazy dog. " * 4).strip()
    B._pdf_text = lambda _data: body
    info = B.import_book(TMP, "english-book.pdf", b"%PDF-1.4 fake")
    with open(os.path.join(info["dir"], "chapters", "0000.md"), encoding="utf-8") as f:
        saved = f.read()
    chk("端到端 PDF：落盘正文里单词是分开的",
        "brown fox" in saved and "over the" in saved and "brownfox" not in saved,
        saved[:200])
finally:
    B._pdf_text = real_pdf_text
    shutil.rmtree(TMP, ignore_errors=True)

# ── G. 编码识别 ────────────────────────────────────────────────────
GBK_SENT = "中文测试"
GBK_LONG = "这是一本中文电子书的正文内容，用来测试编码识别是否正确无误地对上。"
GBK_BOOK = "第一章 开始\n\n这是一个中文测试。\n\n第二章 继续\n\n还有更多内容。\n"

got = B._decode_text(GBK_SENT.encode("gb18030"))
chk("G1 GBK 短句不再被 utf-16 抢走（回归：中文测试 → 乱码）", got == GBK_SENT, got)
chk("G2 GBK 长段落认得对", B._decode_text(GBK_LONG.encode("gb18030")) == GBK_LONG)
chk("G3 GBK 整本书（多章多段）认得对", B._decode_text(GBK_BOOK.encode("gb18030")) == GBK_BOOK)
chk("G4 UTF-8 中文原样", B._decode_text(GBK_SENT.encode("utf-8")) == GBK_SENT)
bom = B._decode_text(GBK_SENT.encode("utf-8-sig"))
chk("G5 UTF-8 BOM 认得出，且 BOM 不留在正文里",
    bom == GBK_SENT and not bom.startswith("\ufeff"), bom)
chk("G6 带 BOM 的 utf-16 认得出", B._decode_text(GBK_SENT.encode("utf-16")) == GBK_SENT)
chk("G7 无 BOM 的 utf-16（带换行）认得出",
    B._decode_text(GBK_BOOK.encode("utf-16-le")) == GBK_BOOK)
chk("G8 无 BOM 的 utf-16（整篇纯汉字、一个 0x00 都没有）也要认得出",
    B._decode_text("中文测试一下编码识别".encode("utf-16-le")) == "中文测试一下编码识别")
chk("G9 无 BOM 的 utf-16 ASCII 认得出",
    B._decode_text("hello world".encode("utf-16-le")) == "hello world")
chk("G10 纯 ASCII 仍按 utf-8", B._decode_text(b"hello world") == "hello world")
chk("G11 空数据给空串", B._decode_text(b"") == "")
chk("G12 认不出的二进制不抛异常、给字符串",
    isinstance(B._decode_text(b"\xff\x00\xfe\x01\x80"), str))

# ── H. 格式识别 ────────────────────────────────────────────────────
chk("H1 扩展名优先：.md", B.guess_format("a.md") == "md")
chk("H2 扩展名大小写不敏感：.EPUB", B.guess_format("A.EPUB") == "epub")
chk("H3 没扩展名时按 %PDF 头认 pdf", B.guess_format("", b"%PDF-1.7 ...") == "pdf")
_buf = io.BytesIO()
with zipfile.ZipFile(_buf, "w") as _z:
    _z.writestr("mimetype", "application/epub+zip")
    _z.writestr("META-INF/container.xml", "<container/>")
chk("H4 撞上 PK 开头再看有没有 mimetype（认 epub）",
    B.guess_format("x.bin", _buf.getvalue()) == "epub")
chk("H5 认不出给空串", B.guess_format("x.xyz", b"\x00\x01") == "")

# ── I. 落盘 ────────────────────────────────────────────────────────
BOOKS = os.path.join(SANDBOX, "books")
info = B.import_book(BOOKS, "一本 GBK 的书.txt", GBK_BOOK.encode("gb18030"))
d = info["dir"]
meta = json.load(open(os.path.join(d, "meta.json"), encoding="utf-8"))
cat = json.load(open(os.path.join(d, "_catalog.json"), encoding="utf-8"))
ch0 = open(os.path.join(d, "chapters", "0000.md"), encoding="utf-8").read()
ch1 = open(os.path.join(d, "chapters", "0001.md"), encoding="utf-8").read()

chk("I1 返回 meta 齐全（id / dir / 章数）",
    info["id"].startswith("imp_") and os.path.isdir(d) and info["chapters"] == 2, info)
chk("I2 落的是一本普通书（meta / 目录 / 进度三件都在）",
    all(os.path.exists(os.path.join(d, n)) for n in
        ("meta.json", "_catalog.json", "_progress.json")))
chk("I3 GBK 正文里的章标题是原字，不是乱码",
    "第一章 开始" in ch0 and "第二章 继续" in ch1, (ch0[:40], ch1[:40]))
chk("I4 目录留的是原字标题", cat == ["第一章 开始", "第二章 继续"], cat)
chk("I5 认出来源与格式", meta.get("source") == "local" and meta.get("format") == "txt", meta)
chk("I6 标题退回文件名", meta.get("title") == "一本 GBK 的书", meta.get("title"))

_md = "# 第一章 开始\n\n正文一。\n\n# 第二章 继续\n\n正文二。\n"
info2 = B.import_book(BOOKS, "草稿.md", _md.encode("utf-8"))
cat2 = json.load(open(os.path.join(info2["dir"], "_catalog.json"), encoding="utf-8"))
chk("I7 Markdown 按一级标题切章", cat2 == ["第一章 开始", "第二章 继续"], cat2)
chk("I8 两本书各自一个目录，不互相覆盖", info2["dir"] != d and os.path.isdir(info2["dir"]))

try:
    B.import_book(BOOKS, "空.txt", b"")
    _raised = False
except ValueError:
    _raised = True
chk("I9 空文件当面报错，不悄悄落一本空书", _raised)

# ── 收摊 ────────────────────────────────────────────────────────────
print("\nbook_import 编码 / 格式 / 落盘 / PDF 硬换行体检：通过 %d 项，失败 %d 项" % (PASSED, len(FAIL)))
for f in FAIL:
    print("  ✗ " + f)
sys.exit(1 if FAIL else 0)
