# -*- coding: utf-8 -*-
"""围栏那件事的第二半：**行怎么切**也只有一把尺子。

上一轮（check_md_fence / check_fence_rulers）收的是「哪些行算围栏里的行」。可那条尺子
的入参是「一串行」，行是谁切的、按什么切，一直没人管 —— 于是同一段稿子在两个出口那里
行数不同：

  · `md_fence.prose_lines`（写作大纲 / 兜底导图 / 笔记子分支）用 `str.splitlines()`；
  · `book_import._fenced_mask`（导入切章）与 `book_export.md_to_html`（导出渲染）
    都只按 `\\n` 切，行号也按它算。

`str.splitlines()` 比 `\\n` 多认一串「Unicode 换行」：\\x0b \\x0c \\x1c \\x1d \\x1e
\\x85 \\u2028 \\u2029。差别不是把行号数错一位那么轻：

  · 大纲「跳到这一节」靠行号。PDF 抽文里的换页符（\\x0c，抽页时到处都是）每出现一次，
    后面的行号就比原文多一行，跳过去正好偏一行。
  · 换页符正好挨在 ``` 前面时（`前言\\x0c```）`splitlines` 会凭空切出一个「行首是围栏」
    的行，把后面的真标题一路吞进代码块；切章那边（这一行是 `前言\\x0c```，行首不是围栏）
    却看得见 —— 又是「切章看得见、大纲 / 导图看不见」，正是这个模块为了消灭才存在的病。

这一条把行尺子钉成一条：`md_fence.split_lines` 只按 `\\n` 切，两个出口共用；再拿
含各种 Unicode 换行的语料验证 `prose_lines` 与 `fence_mask(split_lines(t))` 逐行等价
（判定与行号都相同），并留一面「旧口径确实会走散」的镜子。纯逻辑、不联网、不开浏览器。
"""
import os
import re
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

SANDBOX = tempfile.mkdtemp(prefix="guizang-fence-lines-")
os.environ["GUIZANG_DATA"] = os.path.join(SANDBOX, "cache")
os.environ["GUIZANG_BOOKS"] = os.path.join(SANDBOX, "books")

import book_import        # noqa: E402
import md_fence           # noqa: E402
import writer             # noqa: E402

FAIL = []
PASSED = 0


def chk(name, cond, extra=""):
    global PASSED
    print(("PASS  " if cond else "FAIL  ") + name + ("" if cond else "   " + repr(extra)[:300]))
    if cond:
        PASSED += 1
    else:
        FAIL.append(name)


def source(name):
    with open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                           name), encoding="utf-8") as f:
        return f.read()


BT = chr(96)
FENCE = BT * 3

# `str.splitlines()` 多认的那一串「Unicode 换行」，外加正常的 \n / \r\n。
UNICODE_BREAKS = ("\x0b", "\x0c", "\x1c", "\x1d", "\x1e", "\x85", "\u2028", "\u2029")

# 语料：正常稿子 + 每一种换行符都来一份「挨在围栏前面」的刁钻写法 + 混排。
CORPUS = [
    ("正常围栏", "前\n" + FENCE + "\n# 注释\n" + FENCE + "\n# 真标题\n正文\n"),
    ("未收尾", "正文一\n" + FENCE + "\n# 注\n正文二"),
    ("波浪号", "~~~\n# 注\n~~~\n# 真标题\n"),
    ("info 串带反引号", FENCE + " a" + BT + "b\n# 真标题\n正文\n"),
    ("无围栏", "第一行\n\n# 标题\n正文\n"),
]
for _b in UNICODE_BREAKS + ("\r\n",):
    CORPUS.append(("换行符 %r 挨在围栏前" % _b, "前言" + _b + FENCE + "\n# 标题\n正文\n"))
    CORPUS.append(("换行符 %r 在正文中间" % _b, "第一行" + _b + "第二行\n# 真标题\n尾巴\n"))

# ── A. 行尺子本身 ───────────────────────────────────────────────
lines = md_fence.split_lines("a\nb\n")
chk("A1 split_lines 只按 \\n 切：行数 = \\n 数 + 1",
    lines == ["a", "b", ""], lines)
chk("A2 split_lines 不把 \\x0c / \\u2028 当换行",
    md_fence.split_lines("a\x0cb\u2028c") == ["a\x0cb\u2028c"],
    md_fence.split_lines("a\x0cb\u2028c"))
chk("A3 split_lines 与 str.split('\\n') 逐字相同",
    all(md_fence.split_lines(t) == t.split("\n") for _n, t in CORPUS))
chk("A4 split_lines None / 空串都收得下",
    md_fence.split_lines(None) == [""] and md_fence.split_lines("") == [""],
    (md_fence.split_lines(None), md_fence.split_lines("")))
chk("A5 空文档仍是「没有正文行」（split_lines 给 ['']，prose_lines 给空表）",
    list(md_fence.prose_lines("")) == [] and list(md_fence.prose_lines(None)) == [],
    (list(md_fence.prose_lines("")), list(md_fence.prose_lines(None))))

# ── B. 两个出口逐行等价（判定 + 行号）────────────────────────────
def expect_prose(text):
    """按 `\\n` 切行、尺子标块，亲手算出「该留下的 (行号, 行)」。"""
    hs = md_fence.split_lines(text)
    return [(i, ln) for i, (ln, code) in enumerate(zip(hs, md_fence.fence_mask(hs)), 1)
            if not code]


for name, t in CORPUS:
    got = list(md_fence.prose_lines(t))
    chk("B 行号与判定都按 \\n 切：%s" % name, got == expect_prose(t), (got, expect_prose(t)))

chk("B2 fence_mask 与 split_lines 的行数永远相等",
    all(len(md_fence.fence_mask(md_fence.split_lines(t))) == len(md_fence.split_lines(t))
        for _n, t in CORPUS))

# 换页符挨在围栏前：`# 标题` 必须留在正文里（旧口径会把它吞掉）
t_ff = "前言\x0c" + FENCE + "\n# 标题\n正文\n"
chk("B3 换页符挨着 ```：真标题没被吞（大纲 / 导图看得见）",
    "# 标题" in [ln for _i, ln in md_fence.prose_lines(t_ff)],
    list(md_fence.prose_lines(t_ff)))
chk("B4 切章那边不会凭空多出一个开栏（mask 全是 False）",
    book_import._fenced_mask(t_ff) == [False] * len(md_fence.split_lines(t_ff)),
    book_import._fenced_mask(t_ff))

# ── C. 行号能索引回原文（「跳到这一节」靠它）────────────────────
for name, t in CORPUS:
    nl = t.split("\n")
    ok = True
    for h in writer.outline(t):
        ln = h["line"]
        if not (1 <= ln <= len(nl)) or h["title"] not in nl[ln - 1]:
            ok = False
            break
    chk("C 大纲行号索引回原文那一行：%s" % name, ok,
        [(h["line"], h["title"]) for h in writer.outline(t)])

chk("C2 换页符不再把行号推后：`# 真标题` 报第 2 行（不是第 3 行）",
    [h["line"] for h in writer.outline("第一行\x0c第二行\n# 真标题\n")] == [2],
    [h["line"] for h in writer.outline("第一行\x0c第二行\n# 真标题\n")])

# ── D. 对照：旧口径（str.splitlines）确实会走散 ─────────────────
old_mask = md_fence.fence_mask(t_ff.splitlines())
new_mask = md_fence.fence_mask(md_fence.split_lines(t_ff))
chk("D1 镜子：旧的 splitlines 会把 `# 标题` 标进代码块，新的不会",
    old_mask != new_mask and new_mask.count(True) == 0,
    (t_ff.splitlines(), old_mask, new_mask))
chk("D2 镜子：旧口径下大纲看不见那个标题",
    "# 标题" not in [ln for _i, ln in
                     ((i, ln) for i, (ln, c) in enumerate(zip(t_ff.splitlines(), old_mask), 1)
                      if not c)])

# ── E. 接线：一处实现，两个出口都走它 ───────────────────────────
src = source("md_fence.py")


def called_splitlines(text):
    """源码里真的**调用**了 splitlines 的地方（注释 / docstring 里提一句不算）。"""
    import ast
    hits = []
    for node in ast.walk(ast.parse(text)):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "splitlines"):
            hits.append(getattr(node, "lineno", "?"))
    return hits


chk("E1 md_fence 里不再**调用** splitlines()（行只按 \\n 切；注释里提一句不算）",
    called_splitlines(src) == [], called_splitlines(src))
chk("E2 prose_lines 走 split_lines",
    re.search(r"_scan\(split_lines\(", src) is not None)
chk("E3 book_import._fenced_mask 走 md_fence.split_lines（不是自己 .split('\\n')）",
    "md_fence.split_lines(" in source("book_import.py"))
chk("E4 导出那条路（book_export）也仍按 \\n 切",
    'split("\\n")' in source("book_export.py"))

# 大语料对照：随便拼一段长稿，两个出口必须逐行等价
LONG = "\n".join(["# 头", "引子\x0c" + FENCE + "bash", "# 代码里的注释", "pip install x",
                  FENCE, "\u2028", "# 真章节", "正文", "~~~", "# 图里的注", "~~~",
                  "# 尾章", ""])
chk("E5 长稿：prose_lines == fence_mask(split_lines) 亲手算的那份",
    list(md_fence.prose_lines(LONG)) == expect_prose(LONG))
chk("E6 长稿：切章用的 mask 与导出 / 大纲行数对齐",
    len(book_import._fenced_mask(LONG)) == len(LONG.split("\n")))

print()
if FAIL:
    print("围栏行尺子：失败 %d 项" % len(FAIL))
    for n in FAIL:
        print("  ✗ " + n)
    sys.exit(1)
print("围栏行尺子：通过 %d 项，失败 0 项" % PASSED)
