# -*- coding: utf-8 -*-
"""笔记思维导图（book_notes.build_mindmap_svg）的离线自测：一行一条结论，失败就非零退出。

为什么单独一份：另外那份 `check_mindmap.py` 打的是独立的 `mindmap.py`，而这一条是
「笔记 → 树 → 自足 SVG」那条路（界面上的「生成思维导图」、MCP 导图、命令行都走它），
此前一条断言都没有。它最会出的不是布局难看，而是**产出的 SVG 根本不是合法 XML**：

    `_esc(title)[:60]` 是先转义、后截断。标题里带 `&` / `<` / `"` 时，转义会把一个字
    胀成 `&amp;` 五个字，第 60 个字符正好落在某个实体中间，切出来就是 `&am` 这种
    半截实体 —— 文件照样写下去、界面照样不报错，打开却是一张破图（XML 解析直接失败）。

这一条把「标题边界处的实体不许被切断」与「正文里的尖括号 / 引号 / & 一律转义」
都按成会失败的断言，产出的 SVG 用标准库 ElementTree 真解析一遍。纯逻辑套件：
不起服务、不开浏览器、不联网、不截图；书目录落在系统临时沙盒里。

跑法：.venv/bin/python tests/check_notes_mindmap.py
"""
import atexit
import os
import re
import shutil
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

SANDBOX = tempfile.mkdtemp(prefix="gz-notes-map-check-")
atexit.register(lambda: shutil.rmtree(SANDBOX, ignore_errors=True))

import book_notes as bn  # noqa: E402

FAIL = []
N = 0


def chk(name, cond, extra=""):
    global N
    N += 1
    print(("PASS  " if cond else "FAIL  ") + name + ("" if cond else "   " + repr(extra)[:220]))
    if not cond:
        FAIL.append(name)


def parses(svg):
    """能不能当 XML 解析。回 (True, None) 或 (False, 异常)。"""
    try:
        ET.fromstring(svg)
        return True, None
    except Exception as e:                     # noqa: BLE001 —— 报错文本要显示出来
        return False, e


def title_text(svg):
    """把标题那一行 <text fill="#8b8171">…</text> 解出来的可见文字取回来。

    走 ElementTree 解析而不是正则抠串：正则拿到的是**还没解实体**的原文
    （`A&amp;B`），拿它跟 `A&B` 比就会把「转义正确」误判成失败。
    """
    try:
        root = ET.fromstring(svg)
    except Exception:
        return None
    for el in root.iter():
        if el.tag.rsplit("}", 1)[-1] == "text" and el.get("fill") == "#8b8171":
            return el.text
    return None


DOC = {"marks": [
    {"id": "m1", "text": "<划线> & \"引号\"", "tag": "quote", "ch": "0000.md",
     "memo": "带 <尖括号> 的批注"},
    {"id": "m2", "text": "第二条", "tag": "doubt", "ch": "0001.md"},
], "entries": [
    {"id": "e1", "title": "条目 <一> & 二", "body": "# 小标题 <x>\n正文"},
]}

TREE = bn.note_tree(DOC)

# ── 1. 标题：转义实体的截断边界不许切成半截 ────────────────────────
# `"a" + "&"*20` 转义后是 `a` + `&amp;`×20，第 60 个字符正落在第 12 个实体的中间。
# 旧代码 `_esc(title)[:60]` 在这里产出 `…&amp;&amp;&amp…&am`，解析必挂。
BOUNDARY = "a" + "&" * 20
svg = bn.build_mindmap_svg(TREE, BOUNDARY)
ok, err = parses(svg)
chk("标题在实体中间被截断时产出的 SVG 仍是合法 XML（旧代码在这里切出半截实体）",
    ok, err)
chk("标题是合法 XML 且可见文字就是原标题的前 60 字（不是被转义胀大后的半截）",
    title_text(svg) == BOUNDARY[:60], title_text(svg))

for raw in ("<书>", "A&B", '引号"在这里', "&" * 40, "长" * 200, "", None):
    s = bn.build_mindmap_svg(TREE, raw if raw is not None else "")
    ok, err = parses(s)
    chk("标题 %r 出得来合法 XML" % (raw,), ok, err)
    # 有标题时可见文字必须与「原文前 60 字」一致：既没有漏转义，也没有被截到实体里
    if raw:
        chk("标题 %r 的可见文字等于原文前 60 字" % (raw,), title_text(s) == raw[:60],
            (title_text(s), raw[:60]))

# ── 2. 正文标签里的尖括号 / & / 引号一律转义 ────────────────────────
svg = bn.build_mindmap_svg(TREE, "一本书")
ok, err = parses(svg)
chk("正文里带 < > & \" 的划线 / 批注 / 条目出的 SVG 是合法 XML", ok, err)
chk("正文里没有未转义的裸 `<划线>`（会被当成标签）", "<划线>" not in svg, svg[:200])
chk("正文里没有未转义的裸 `&`（后面必须跟实体名）",
    not re.search(r"&(?!(?:amp|lt|gt|quot|apos|#\d+|#x[0-9a-fA-F]+);)", svg), svg[:200])
chk("转义后的划线文字在可见层里读得回来",
    "&lt;划线&gt;" in svg and "&amp;" in svg, svg[:200])

# ── 3. 三种切法都出得来合法 XML ────────────────────────────────────
for cut in ("tag", "chapter", "entry"):
    t = bn.note_tree(DOC, cut=cut)
    s = bn.build_mindmap_svg(t, "一本书")
    ok, err = parses(s)
    chk("cut=%s 出的 SVG 是合法 XML" % cut, ok, err)
    chk("cut=%s 有根节点且有分支" % cut, len(t["kids"]) >= 1, t["kids"])
    chk("cut=%s 的 SVG 首尾闭合" % cut,
        s.startswith("<svg") and s.rstrip().endswith("</svg>"), s[:60])

# ── 4. 空笔记者一棵只有根节点的树也要出图 ─────────────────────────
empty = bn.note_tree({"marks": [], "entries": []})
s = bn.build_mindmap_svg(empty, "还没有笔记")
ok, err = parses(s)
chk("一条笔记都没有时仍出一张合法 SVG（不抛、不空串）", ok and len(s) > 50, err)

# ── 5. 落盘那条路：mindmap() 写进书目录的那份也要能解析 ─────────────
BOOK = os.path.join(SANDBOX, "book-001")
os.makedirs(BOOK, exist_ok=True)
info = bn.mindmap(BOOK, DOC, meta={"title": "A&B 的书"}, book_label="A&B 的书")
ok = os.path.isfile(info["path"])
chk("mindmap() 把 SVG 落到了书目录里", ok, info)
if ok:
    with open(info["path"], encoding="utf-8") as f:
        disk = f.read()
    pok, perr = parses(disk)
    chk("落盘那份 SVG 也能被标准库解析", pok, perr)
    chk("落盘那份可见标题就是书名", title_text(disk) == "A&B 的书",
        title_text(disk))
    chk("落盘字节数与返回的 bytes 对得上",
        info["bytes"] == len(disk.encode("utf-8")), (info["bytes"], len(disk)))
chk("mindmap() 报的节点数等于树上真实节点数",
    info["nodes"] == bn._count(bn.note_tree(DOC)), info["nodes"])

print()
print("笔记思维导图：跑了 %d 条断言" % N)
if FAIL:
    print("失败 %d 项：%s" % (len(FAIL), "、".join(FAIL)))
    sys.exit(1)
print("全部通过")
