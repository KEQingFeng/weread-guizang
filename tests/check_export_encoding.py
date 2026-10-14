#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""导出引擎的落盘编码：章节与合并稿必须钉死 UTF-8，不许靠系统默认编码。

为什么单独一份：export_precise.py 写的是用户最后拿到的那几份文件
（chapters/*.md、raw/*.json、output/<书名>.md）。它读回来的一律写明
encoding="utf-8"，可写出去的那几处原来没写 —— `open()` 不给 encoding 时用的是
**系统默认编码**：macOS 上恰好就是 UTF-8，所以一直没露馅；Windows 的代码页
（中文机是 GBK）上就变成「写的是 GBK、读的是 UTF-8」，轻则花屏，重则正文里
有个生僻字 / emoji 就当场 UnicodeEncodeError —— 用户等完整场导出，最后一秒
才看到失败。这条线此前没有任何门禁（平时跑不到，因为 CI 与 macOS 都不是 GBK）。

钉两件事：

  A 静态：export_precise.py 里每一个**文本模式**的 `open()` 都写了 encoding。
    二进制 open 不看编码；urllib / zipfile 的 `.open(` 不是这一处的目标。

  B 真跑：把系统默认编码按到 C（ASCII），走一遍 save_chapter → merge_chapters，
    中文 + emoji 的正文必须不抛，且落到盘上的字节按 UTF-8 解得回原字。
    playwright 没装时 B 明确标 SKIP（这条要 import 引擎，引擎 import playwright），
    绝不把「没测」装成「通过」。

跑法：.venv/bin/python tests/check_export_encoding.py
不联网、不起浏览器、不碰真实书库（全在系统临时目录里）。
"""
import ast
import json
import locale
import os
import pathlib
import shutil
import sys
import tempfile

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent
SRC_PATH = ROOT / "export_precise.py"

FAIL = []
SKIPS = []


def chk(name, ok, extra=""):
    print(("PASS  " if ok else "FAIL  ") + name + ("" if ok else "   " + repr(extra)[:220]))
    if not ok:
        FAIL.append(name)


def skip(name, why):
    SKIPS.append(name)
    print(("SKIP  " + name + "   " + why)[:260])


# ---------- A 静态：文本 open() 必须声明编码 ----------
def text_opens_missing_encoding(src):
    """export_precise.py 里没写 encoding 的文本 open() 行号。

    只认作为内建名出现的 `open`（`ast.Name`）：`urllib` / `zipfile` 的
    `.open(...)` 是别的东西，二进制模式（"b"）也不归这一条管。
    """
    missing = []
    for node in ast.walk(ast.parse(src)):
        if not isinstance(node, ast.Call):
            continue
        f = node.func
        if not (isinstance(f, ast.Name) and f.id == "open"):
            continue
        mode = "r"
        if len(node.args) >= 2 and isinstance(node.args[1], ast.Constant):
            mode = str(node.args[1].value)
        for kw in node.keywords:
            if kw.arg == "mode" and isinstance(kw.value, ast.Constant):
                mode = str(kw.value.value)
        if "b" in mode:
            continue
        if any(kw.arg == "encoding" for kw in node.keywords):
            continue
        missing.append(node.lineno)
    return missing


src = SRC_PATH.read_text(encoding="utf-8")
missing = text_opens_missing_encoding(src)
chk("静态：export_precise.py 每个文本 open() 都写了 encoding", not missing,
    "没写 encoding 的行：" + str(missing))

# ---------- B 真跑：默认编码按到 ASCII，中文正文仍要落得下、读得回 ----------
BODY = "第一章 中文正文：生僻字 㐀，emoji 🧭，书名《归藏》"
CHAPTER_TITLE = "第一章 中文标题"
BOOK_TITLE = "样张·书名 中文"


def run_functional():
    try:
        sys.path.insert(0, str(ROOT))
        import export_precise as E          # noqa: E402
    except Exception as e:                  # 多半是没装 playwright
        skip("真跑：非 UTF-8 默认编码下的 save_chapter / merge_chapters",
             "import export_precise 失败（%s: %s），这条起不来" % (type(e).__name__, e))
        return

    old_locale = locale.setlocale(locale.LC_ALL)
    tmp = tempfile.mkdtemp(prefix="guizang-export-encoding-")
    try:
        locale.setlocale(locale.LC_ALL, "C")
        enc = locale.getpreferredencoding(False)
        if enc.lower().replace("-", "") in ("utf8", "utf32", "utf16"):
            skip("真跑：非 UTF-8 默认编码下的 save_chapter / merge_chapters",
                 "这台机器上 C 语言环境仍给 %s（UTF-8 模式开着），证不了什么" % enc)
            return
        md = os.path.join(tmp, "chapters")
        raw = os.path.join(tmp, "raw")
        out = os.path.join(tmp, "output")
        for d in (md, raw, out):
            os.makedirs(d)

        saved = None
        try:
            saved = E.save_chapter(CHAPTER_TITLE, [{"type": "text", "text": BODY}],
                                   1, md, raw)
        except Exception as e:
            chk("真跑：save_chapter 在默认编码 %s 下不抛" % enc, False,
                "%s: %s" % (type(e).__name__, e))

        ch_path = os.path.join(md, "0001.md")
        if os.path.isfile(ch_path):
            data = open(ch_path, "rb").read()
            try:
                text = data.decode("utf-8")
                chk("真跑：章节 md 是 UTF-8，中文与生僻字原样", BODY in text and "㐀" in text)
            except UnicodeDecodeError as e:
                chk("真跑：章节 md 是 UTF-8", False, "按 UTF-8 解不开：%s" % e)
        else:
            chk("真跑：章节 md 落盘了", False, os.listdir(md) if os.path.isdir(md) else md)

        raw_path = os.path.join(raw, "0001.json")
        if os.path.isfile(raw_path):
            try:
                obj = json.loads(open(raw_path, "rb").read().decode("utf-8"))
                chk("真跑：raw json 是 UTF-8，标题原样", obj.get("title") == CHAPTER_TITLE, obj)
            except UnicodeDecodeError as e:
                chk("真跑：raw json 是 UTF-8", False, "按 UTF-8 解不开：%s" % e)
        else:
            chk("真跑：raw json 落盘了", False, os.listdir(raw) if os.path.isdir(raw) else raw)

        merged = ""
        try:
            merged = E.merge_chapters(md, out, "样张_书名", BOOK_TITLE, "作者 🧭")
        except Exception as e:
            chk("真跑：merge_chapters 在默认编码 %s 下不抛" % enc, False,
                "%s: %s" % (type(e).__name__, e))
        if merged and os.path.isfile(merged):
            try:
                text = open(merged, "rb").read().decode("utf-8")
                chk("真跑：合并稿是 UTF-8，书名/作者/正文都在",
                    BOOK_TITLE in text and "作者 🧭" in text and BODY in text)
            except UnicodeDecodeError as e:
                chk("真跑：合并稿是 UTF-8", False, "按 UTF-8 解不开：%s" % e)
        else:
            chk("真跑：合并稿落盘了", False, str(merged))

        if saved is not None:
            chk("真跑：save_chapter 报的字符数对得上", saved[0] == len(BODY), saved)
    finally:
        try:
            locale.setlocale(locale.LC_ALL, old_locale)
        except Exception:
            pass
        shutil.rmtree(tmp, ignore_errors=True)


run_functional()

print()
if SKIPS:
    print("导出编码取证：跳过 %d 项（%s）" % (len(SKIPS), "、".join(SKIPS)))
print("导出编码取证：" + ("全部通过" if not FAIL else "%d 项未过：%s" % (len(FAIL), FAIL)))
sys.exit(1 if FAIL else 0)
