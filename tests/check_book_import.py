# -*- coding: utf-8 -*-
"""book_import 的离线自测：编码认不错、格式认得出、导进来的书结构齐全。

三段都在系统临时目录的沙盒里跑，不联网、不开浏览器、不碰用户真实书库：

  A `_decode_text` 的编码识别。这一条的重点是「无 BOM 的 utf-16 不许抢在 gb18030
    前头」—— 它几乎不会报错，却会拿两个字节当一个字硬解，把整篇 GBK 中文解成别的字
    （「中文测试」→「탖쓎」，8 个字节刚好是合法 utf-16）。同时守住 utf-8、带 BOM 的
    utf-8、带与不带 BOM 的 utf-16 都还认得出来。
  B `guess_format`：扩展名看不出时按文件头兜底（%PDF、PK + mimetype）。
  C `import_book` 落盘：GBK 的 TXT 导进来，章标题与正文都得是原字，meta / 目录齐全。

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
os.environ["GUIZANG_DATA"] = os.path.join(SANDBOX, "data")   # 兜住按环境推路径的调用

import book_import as B  # noqa: E402

FAIL = []


def chk(name, cond, extra=""):
    print(("PASS  " if cond else "FAIL  ") + name + ("" if cond else "   " + repr(extra)))
    if not cond:
        FAIL.append(name)


# ── A 编码识别 ──────────────────────────────────────────────────────
GBK_SENT = "中文测试"
GBK_LONG = "这是一本中文电子书的正文内容，用来测试编码识别是否正确无误地对上。"
GBK_BOOK = "第一章 开始\n\n这是一个中文测试。\n\n第二章 继续\n\n还有更多内容。\n"

got = B._decode_text(GBK_SENT.encode("gb18030"))
chk("A1 GBK 短句不再被 utf-16 抢走（回归：中文测试 → 乱码）", got == GBK_SENT, got)
chk("A2 GBK 长段落认得对", B._decode_text(GBK_LONG.encode("gb18030")) == GBK_LONG)
chk("A3 GBK 整本书（多章多段）认得对", B._decode_text(GBK_BOOK.encode("gb18030")) == GBK_BOOK)
chk("A4 UTF-8 中文原样", B._decode_text(GBK_SENT.encode("utf-8")) == GBK_SENT)
bom = B._decode_text(GBK_SENT.encode("utf-8-sig"))
chk("A5 UTF-8 BOM 认得出，且 BOM 不留在正文里",
    bom == GBK_SENT and not bom.startswith("\ufeff"), bom)
chk("A6 带 BOM 的 utf-16 认得出", B._decode_text(GBK_SENT.encode("utf-16")) == GBK_SENT)
chk("A7 无 BOM 的 utf-16（带换行）认得出",
    B._decode_text(GBK_BOOK.encode("utf-16-le")) == GBK_BOOK)
chk("A8 无 BOM 的 utf-16（整篇纯汉字、一个 0x00 都没有）也要认得出",
    B._decode_text("中文测试一下编码识别".encode("utf-16-le")) == "中文测试一下编码识别")
chk("A9 无 BOM 的 utf-16 ASCII 认得出",
    B._decode_text("hello world".encode("utf-16-le")) == "hello world")
chk("A10 纯 ASCII 仍按 utf-8", B._decode_text(b"hello world") == "hello world")
chk("A11 空数据给空串", B._decode_text(b"") == "")
chk("A12 认不出的二进制不抛异常、给字符串",
    isinstance(B._decode_text(b"\xff\x00\xfe\x01\x80"), str))

# ── B 格式识别 ──────────────────────────────────────────────────────
chk("B1 扩展名优先：.md", B.guess_format("a.md") == "md")
chk("B2 扩展名大小写不敏感：.EPUB", B.guess_format("A.EPUB") == "epub")
chk("B3 没扩展名时按 %PDF 头认 pdf", B.guess_format("", b"%PDF-1.7 ...") == "pdf")
_buf = io.BytesIO()
with zipfile.ZipFile(_buf, "w") as _z:
    _z.writestr("mimetype", "application/epub+zip")
    _z.writestr("META-INF/container.xml", "<container/>")
chk("B4 撞上 PK 开头再看有没有 mimetype（认 epub）",
    B.guess_format("x.bin", _buf.getvalue()) == "epub")
chk("B5 认不出给空串", B.guess_format("x.xyz", b"\x00\x01") == "")

# ── C 落盘 ──────────────────────────────────────────────────────────
BOOKS = os.path.join(SANDBOX, "books")
info = B.import_book(BOOKS, "一本 GBK 的书.txt", GBK_BOOK.encode("gb18030"))
d = info["dir"]
meta = json.load(open(os.path.join(d, "meta.json"), encoding="utf-8"))
cat = json.load(open(os.path.join(d, "_catalog.json"), encoding="utf-8"))
ch0 = open(os.path.join(d, "chapters", "0000.md"), encoding="utf-8").read()
ch1 = open(os.path.join(d, "chapters", "0001.md"), encoding="utf-8").read()

chk("C1 返回 meta 齐全（id / dir / 章数）",
    info["id"].startswith("imp_") and os.path.isdir(d) and info["chapters"] == 2, info)
chk("C2 落的是一本普通书（meta / 目录 / 进度三件都在）",
    all(os.path.exists(os.path.join(d, n)) for n in
        ("meta.json", "_catalog.json", "_progress.json")))
chk("C3 GBK 正文里的章标题是原字，不是乱码",
    "第一章 开始" in ch0 and "第二章 继续" in ch1, (ch0[:40], ch1[:40]))
chk("C4 目录留的是原字标题", cat == ["第一章 开始", "第二章 继续"], cat)
chk("C5 认出来源与格式", meta.get("source") == "local" and meta.get("format") == "txt", meta)
chk("C6 标题退回文件名", meta.get("title") == "一本 GBK 的书", meta.get("title"))

_md = "# 第一章 开始\n\n正文一。\n\n# 第二章 继续\n\n正文二。\n"
info2 = B.import_book(BOOKS, "草稿.md", _md.encode("utf-8"))
cat2 = json.load(open(os.path.join(info2["dir"], "_catalog.json"), encoding="utf-8"))
chk("C7 Markdown 按一级标题切章", cat2 == ["第一章 开始", "第二章 继续"], cat2)
chk("C8 两本书各自一个目录，不互相覆盖", info2["dir"] != d and os.path.isdir(info2["dir"]))

try:
    B.import_book(BOOKS, "空.txt", b"")
    _raised = False
except ValueError:
    _raised = True
chk("C9 空文件当面报错，不悄悄落一本空书", _raised)

# ── 收摊 ────────────────────────────────────────────────────────────
print()
if FAIL:
    print("失败 %d 项：%s" % (len(FAIL), "、".join(FAIL)))
    sys.exit(1)
print("book_import 编码与导入体检：全部通过")
