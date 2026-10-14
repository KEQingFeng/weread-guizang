#!/usr/bin/env python3
"""划线笔记「导出 Markdown」的离线自测：引用行的括号、落盘、接线。

`book_notes.export_markdown` 是「笔记条目 → 一份能直接进阅读器 / Git / flomo 的
Markdown」那条路唯一的排版处，界面上打一发 `mynotes.export` 就走它。它最容易出的
毛病不在「有没有导出」，而在**排版时把标点吃掉了**：

  条目下面那条「> 引用：……」原来写成
      ("（%s）" % titles.get(ch) if ch in titles else "").rstrip("（）")
  ——`rstrip` 是拿来做「万一标题带括号」的收尾的，可它连**刚拼上去的右括号**也一起
  啃掉，于是正常导出里永远少一个 `）`；而目录里那一章标题恰好是空串时，它又只剩一个
  孤零零的 `（`。两种都不报错、只是文件里悄悄缺一角，最该被钉死。

所以这一条专挑三种标题（正常 / 没有 / 空串）各看一遍引用行，再顺带守住「引用正文里
的换行不许把一行撑成两行」「空引用不占位置」「导出的文件与内存里那份一字不差」。

全程离线：只调 book_notes 的纯函数 + 一个临时书目录，不 import 服务、不联网。
"""
import os
import pathlib
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import book_notes as bn  # noqa: E402

FAIL = []
PASSED = 0


def chk(name, cond, extra=""):
    global PASSED
    print(("PASS  " if cond else "FAIL  ") + name + ("" if cond else "   " + str(extra)[:220]))
    if cond:
        PASSED += 1
    else:
        FAIL.append(name)


def refs_lines(md):
    """导出文本里所有「> 引用：…」那一行，原样给出，方便逐字比。"""
    return [ln for ln in md.splitlines() if ln.startswith("> 引用：")]


def doc_with(refs, body="这是一条笔记的正文", title="一条笔记"):
    return {"marks": [], "entries": [
        {"title": title, "body": body, "refs": refs, "updated_at": 1756864800}]}


# ── 1. 引用行：章节名要加就成对地加 ────────────────────────────
md = bn.export_markdown(doc_with([{"text": "这句话值得记", "ch": "0001.md"}]),
                        titles={"0001.md": "第一章"})
chk("E1 有章节名时括号成对（右括号没被 rstrip 吃掉）",
    refs_lines(md) == ["> 引用：这句话值得记（第一章）"], refs_lines(md))

md = bn.export_markdown(doc_with([{"text": "这句话值得记", "ch": "0009.md"}]),
                        titles={"0001.md": "第一章"})
chk("E2 章节名对不上就不加括号（不留半截）",
    refs_lines(md) == ["> 引用：这句话值得记"], refs_lines(md))

md = bn.export_markdown(doc_with([{"text": "这句话值得记", "ch": "0001.md"}]),
                        titles={"0001.md": ""})
chk("E3 目录里标题是空串时也不留孤零零的左括号",
    refs_lines(md) == ["> 引用：这句话值得记"], refs_lines(md))

md = bn.export_markdown(doc_with([{"text": "这句话值得记"}]), titles={"0001.md": "第一章"})
chk("E4 引用没带章节号时不加括号", refs_lines(md) == ["> 引用：这句话值得记"], refs_lines(md))

# 标题本身带括号/空白，也照原样放进去，不再被啃
md = bn.export_markdown(doc_with([{"text": "q", "ch": "0001.md"}]),
                        titles={"0001.md": "第三章（下）"})
chk("E5 标题自带括号时原样保留",
    refs_lines(md) == ["> 引用：q（第三章（下））"], refs_lines(md))

md = bn.export_markdown(doc_with([{"text": "q", "ch": "0001.md"}]),
                        titles={"0001.md": "  第一章  "})
chk("E6 标题首尾空白先修掉再拼", refs_lines(md) == ["> 引用：q（第一章）"], refs_lines(md))

# ── 2. 引用正文里的换行 / 空引用 ───────────────────────────────
md = bn.export_markdown(doc_with([{"text": "第一行\n第二行", "ch": "0001.md"}]),
                        titles={"0001.md": "第一章"})
chk("E7 引用正文的换行换成空格，不许把一行撑成两行",
    refs_lines(md) == ["> 引用：第一行 第二行（第一章）"], refs_lines(md))

md = bn.export_markdown(doc_with([{"text": "   ", "ch": "0001.md"},
                                  {"text": "有效的一句", "ch": "0001.md"}]),
                        titles={"0001.md": "第一章"})
chk("E8 空白引用不占位置，有效那条照出", refs_lines(md) == ["> 引用：有效的一句（第一章）"],
    refs_lines(md))

md = bn.export_markdown(doc_with([{"not_text": "x"}, "坏条目", None]))
chk("E9 坏引用（缺字段 / 不是字典）既不抛也不出引用行", refs_lines(md) == [], refs_lines(md))

# ── 3. 划线那一段照旧，标点不被误伤 ────────────────────────────
doc = {"marks": [{"ch": "0001.md", "text": "被划的那句", "tag": "doubt", "kind": "highlight",
                  "memo": "我的批注", "created_at": 1756864800}],
       "entries": []}
md = bn.export_markdown(doc, titles={"0001.md": "第一章"})
chk("E10 划线行仍是「标签：「正文」」的成对括号",
    "- **疑问**：「被划的那句」" in md, [l for l in md.splitlines() if l.startswith("- **")])
chk("E11 批注与类型/时间两行都还在",
    "  - 我的批注" in md and "<划线>" in md, md[-400:])

# ── 4. 整篇的收尾与落盘 ────────────────────────────────────────
md = bn.export_markdown(doc_with([{"text": "q", "ch": "0001.md"}]), titles={"0001.md": "第一章"},
                        meta={"url": "https://example.com/a"})
chk("E12 结尾恰好一个换行、行尾无多余空白",
    md.endswith("\n") and not md.endswith("\n\n") and md == md.rstrip() + "\n", repr(md[-30:]))
chk("E13 原书链接还在", "原文链接：https://example.com/a" in md)

with tempfile.TemporaryDirectory(prefix="gz-notes-export-") as book_dir:
    doc_saved = bn.empty_notes()
    doc_saved["entries"] = [{"title": "T", "body": "b", "refs": [{"text": "q", "ch": "0001.md"}],
                             "updated_at": 1756864800, "created_at": 1756864800}]
    fs = {"marks": [], "entries": doc_saved["entries"], "note": "",
          "created_at": doc_saved["created_at"], "updated_at": doc_saved["updated_at"]}
    out = bn.export_notes(book_dir, fs, titles={"0001.md": "第一章"})
    p = os.path.join(book_dir, bn.EXPORT_MD)
    with open(p, encoding="utf-8") as f:
        written = f.read()
    chk("E14 落盘文件名就是 notes.md", os.path.isfile(p), p)
    chk("E15 文件内容与内存里那份一字不差", written == bn.export_markdown(fs, titles={"0001.md": "第一章"}),
        written[-200:])
    chk("E16 回执带路径与字数", out.get("path") == p and out.get("chars") == len(written), out)

print()
print("划线笔记导出 Markdown：通过 %d 项，失败 %d 项" % (PASSED, len(FAIL)))
if FAIL:
    for f in FAIL:
        print("  ✗ " + f)
    sys.exit(1)
print("ok  引用行括号成对、空引用/坏引用不越位、划线段与落盘一字不差")
