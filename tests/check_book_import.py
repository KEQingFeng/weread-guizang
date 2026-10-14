# -*- coding: utf-8 -*-
"""导入书：PDF 抽出来的硬换行不能把英文单词粘成一团。

为什么单独一份：PDF 抽文字是按绘制行吐出来的，每一行都是独立一行；`_normalize_lines`
要把「同一段被硬换行劈开」的行拼回去。老实现是一律直接相加，于是

    The quick brown / fox jumps over / the lazy dog.

变成了 `The quick brownfox jumps overthe lazy dog.` —— 整本英文 PDF 的词全粘在一起，
阅读器里、导出 EPUB / PDF 里都是这个毛病。中文反过来：字间本无空格，拼回去时补空格
才是错的。

这里钉的口径：
  A 拉丁词之间的接缝补一个空格
  B 中日韩 / 全角标点之间的接缝一个字都不加
  C 连字符换行（`multi-` / `agent`）保持连字符、不空格
  D 左括号 / 上引号结尾、右括号 / 收尾标点开头时，接缝不补空格
  E 老行为一样都不许丢：句末标点仍断段、空行仍留段、CRLF 仍归一
  F 真走一遍 import_book 的 PDF 分支：落盘的章节正文里单词是分开的
全程离线，不联网、不碰真实书库。
"""
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import book_import as bi  # noqa: E402

PASSED = 0
FAILED = []


def chk(name, cond, extra=""):
    global PASSED
    if cond:
        PASSED += 1
        print("  ok  " + name)
    else:
        FAILED.append(name)
        print("  ✗  " + name + ("" if cond else "   " + repr(extra)))


# ── A. 英文硬换行：词之间补空格 ─────────────────────────────────
chk("英文：词间接缝补一个空格",
    bi._normalize_lines("The quick brown\nfox jumps over\nthe lazy dog.")
    == "The quick brown fox jumps over the lazy dog.")
chk("英文：行尾是逗号也补空格",
    bi._normalize_lines("This is a long\nsentence, and it\ncontinues here.")
    == "This is a long sentence, and it continues here.")
chk("英文：数字与字母之间同样看待",
    bi._normalize_lines("Chapter 12\nthe year 2026") == "Chapter 12 the year 2026")
chk("英文：一个空格都不多不少（不会补成两个）",
    bi._normalize_lines("hello\nworld") == "hello world")

# ── B. 中文 / 全角标点：接缝不补空格 ───────────────────────────
chk("中文：字间不补空格",
    bi._normalize_lines("第一章\n这是中文段落\n继续接着写。")
    == "第一章这是中文段落继续接着写。")
chk("中文：全角引号与标点间不补空格",
    bi._normalize_lines("他说：“今天\n天气不错。”") == "他说：“今天天气不错。”")
chk("中文：中文收尾接英文开头也不补（保持原样）",
    bi._normalize_lines("正文结束\nNext line here") == "正文结束Next line here")
chk("中文：英文收尾接中文开头也不补",
    bi._normalize_lines("see the note\n就是这句") == "see the note就是这句")
chk("中文：全角逗号开头不补空格",
    bi._normalize_lines("hello,\n你好") == "hello,你好")

# ── C. 连字符换行 ───────────────────────────────────────────────
chk("连字符：multi- / agent 拼成 multi-agent（不加空格）",
    bi._normalize_lines("multi-\nagent systems\nare useful.")
    == "multi-agent systems are useful.")
chk("连字符：破折号 -- 与 en dash 同理",
    bi._normalize_lines("state-of-\u2013\nthe-art") == "state-of-\u2013the-art")

# ── D. 括号 / 引号边界 ──────────────────────────────────────────
chk("括号：左括号结尾不加空格",
    bi._normalize_lines("word\n(note) here") == "word (note) here")
chk("括号：右括号开头不加空格",
    bi._normalize_lines("first (one)\n) tail") == "first (one)) tail")
chk("标点：右括号 / 逗号开头不补空格",
    bi._normalize_lines("hello\n, world") == "hello, world")
chk("引号：上引号结尾不加空格",
    bi._normalize_lines("He said \u201chello\nworld\u201d today.")
    == "He said \u201chello world\u201d today.")

# ── E. 老行为一个都不许丢 ───────────────────────────────────────
chk("断段：句末标点仍把两行分成两段",
    bi._normalize_lines("Hello.\nNew sentence.") == "Hello.\nNew sentence.")
chk("断段：中文句号同样断段",
    bi._normalize_lines("第一句。\n第二句。") == "第一句。\n第二句。")
chk("空行：段落之间仍留一个空行",
    bi._normalize_lines("first paragraph\n\nsecond line\nwrapped.")
    == "first paragraph\n\nsecond line wrapped.")
chk("归一：CRLF / CR 仍按换行处理",
    bi._normalize_lines("a\r\nb\rc") == "a b c")
chk("空输入不炸", bi._normalize_lines("") == "" and bi._normalize_lines(None) == "")

# ── F. 真走一遍 import_book 的 PDF 分支 ─────────────────────────
TMP = tempfile.mkdtemp(prefix="guizang-import-")
real_pdf_text = bi._pdf_text
try:
    body = ("The quick brown\nfox jumps over\nthe lazy dog. " * 4).strip()
    bi._pdf_text = lambda _data: body
    info = bi.import_book(TMP, "english-book.pdf", b"%PDF-1.4 fake")
    with open(os.path.join(info["dir"], "chapters", "0000.md"), encoding="utf-8") as f:
        saved = f.read()
    chk("端到端 PDF：落盘正文里单词是分开的",
        "brown fox" in saved and "over the" in saved and "brownfox" not in saved,
        saved[:200])
finally:
    bi._pdf_text = real_pdf_text
    shutil.rmtree(TMP, ignore_errors=True)

print("\n导入书门禁：%d 项通过，失败 %d 项" % (PASSED, len(FAILED)))
for f in FAILED:
    print("  ✗ " + f)
sys.exit(1 if FAILED else 0)
