# -*- coding: utf-8 -*-
"""围栏代码块只有一把尺子：md_fence。四处接线必须真的走它，口径不许各抄一份。

起因是一个真会走散的边角：CommonMark 规定**反引号起的围栏，它的 info string 里不许
再出现反引号** —— `` ``` a`b `` 只是一行普通正文。book_import 那把尺子一直认这一条，
md_fence 却漏了。同一行正文于是有了两副面孔：

  · 导入书切章 —— 认得它是普通正文，后面的 `# 真标题` 照切；
  · 写作大纲 / 兜底导图 / 笔记子分支 —— 当它是开栏，把后面的真标题一路吞进代码块，
    大纲里那一节凭空消失（代码块的收尾没人补，块一直吃到文末）。

这一条把四件事钉住，纯逻辑、不联网、不开浏览器，所以归 --fast 静态段：

  A 尺子本身：反引号 info string 里的反引号不算开栏；波浪号没有这条限制；
    正常围栏一个字不改；按 `\\n` 切行时 mask 与原文行数永远对得上（切章靠它对齐）。
  B 一把尺子：writer / book_import 不再自带围栏正则，接线走 md_fence。
  C 四处接线逐行等价：writer._prose_lines / book_import._fenced_mask 的结果与
    md_fence 亲手算出来的完全一致（不是「看着像」，是逐行相同）。
  D 对照与回归：旧尺子确实会吞掉真标题；修好之后正常围栏、缩进代码块、未收尾围栏
    这些老口径一条都没变。
"""
import os
import re
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

SANDBOX = tempfile.mkdtemp(prefix="guizang-fence-rulers-")
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


BT = chr(96)                                  # 反引号：三连写容易看混，显式拼
FENCE = BT * 3


def titles_of(text):
    """正文里的 `#` 标题（先按原文切行、再用这把尺子标出哪些行不在块里）。"""
    lines = text.split("\n")
    mask = md_fence.fence_mask(lines)
    out = []
    for ln, in_code in zip(lines, mask):
        if in_code:
            continue
        m = re.match(r"^(#{1,6})\s+(.*\S)\s*$", ln)
        if m:
            out.append(m.group(2).strip())
    return out


def masked_titles(text):
    """被围栏遮住的那些 `#` 行 —— 它们一个都不许出现在切章结果里。"""
    lines = text.split("\n")
    mask = md_fence.fence_mask(lines)
    return {re.sub(r"^#+\s*", "", ln).strip()
            for ln, in_code in zip(lines, mask)
            if in_code and re.match(r"^#", ln)}


# ── A 尺子本身：CommonMark 的 info string 那条 ───────────────────────
chk("A1 反引号开栏的 info string 里带反引号 —— 不算开栏",
    md_fence.fence_mark(FENCE + " a" + BT + "b") is None,
    md_fence.fence_mark(FENCE + " a" + BT + "b"))
chk("A2 反过来，干净的信息串照旧算开栏",
    md_fence.fence_mark(FENCE + "python") == (BT, 3, "python"),
    md_fence.fence_mark(FENCE + "python"))
chk("A3 波浪号没有这条限制（同上写法仍是围栏）",
    md_fence.fence_mark("~~~ a" + BT + "b") == ("~", 3, " a" + BT + "b"),
    md_fence.fence_mark("~~~ a" + BT + "b"))
chk("A4 那一行之后的标题还在（没被吞成代码）",
    [ln for _i, ln in md_fence.prose_lines(FENCE + " a" + BT + "b\n# 真标题\n正文")]
    == [FENCE + " a" + BT + "b", "# 真标题", "正文"],
    [ln for _i, ln in md_fence.prose_lines(FENCE + " a" + BT + "b\n# 真标题\n正文")])
chk("A5 正常围栏照旧整段跳过（别把这条放宽成不认围栏）",
    [ln for _i, ln in md_fence.prose_lines(FENCE + "\n# 注释\n" + FENCE + "\n# 真标题")]
    == ["# 真标题"])

# mask 与「调用方自己切的行」必须等长：导入切章是 zip(原文行, mask)，短一格就是
# 静默错位 / IndexError。空文本、末尾换行这两种最容易把长度算岔。
for probe in ("", "a\n", "a\n\n", FENCE + "\n# x\n", "一\n" + FENCE + "\n二"):
    lines = probe.split("\n")
    chk("A6 mask 与 split('\\n') 行数一致：%r" % probe[:12],
        len(md_fence.fence_mask(lines)) == len(lines),
        (len(md_fence.fence_mask(lines)), len(lines)))
chk("A7 空行列表给空 mask，不抛",
    md_fence.fence_mask([]) == [], md_fence.fence_mask([]))


# ── B 一把尺子：不再各自抄一份围栏正则 ──────────────────────────────
def source(name):
    with open(os.path.join(ROOT, name), encoding="utf-8") as f:
        return f.read()


for mod, fn in (("writer.py", "prose_lines"), ("book_import.py", "fence_mask")):
    s = source(mod)
    chk("B1 %s 从 md_fence 取 %s" % (mod, fn),
        re.search(r"from md_fence import .*\b%s\b" % fn, s) is not None
        or re.search(r"import md_fence\b", s) is not None, mod)
    chk("B2 %s 不再自带围栏正则（_FENCE = re.compile）" % mod,
        "_FENCE = re.compile(" not in s, mod)

for mod in ("ai_sum.py", "book_notes.py"):
    chk("B3 %s 仍走 md_fence.prose_lines" % mod,
        re.search(r"from md_fence import .*\bprose_lines\b", source(mod)) is not None, mod)


# ── C 四处接线逐行等价 ─────────────────────────────────────────────
CORPUS = {
    "info 里带反引号（不是围栏）": FENCE + " a" + BT + "b\n# 真标题\n正文\n",
    "正常围栏": FENCE + "python\n# 注释\n" + FENCE + "\n# 真标题\n",
    "波浪号 + 反引号信息串": "~~~ a" + BT + "b\n# 注释\n~~~\n# 标题\n",
    "四格缩进不算围栏": "    " + FENCE + "\n    # 缩进代码\n    " + FENCE + "\n# 标题\n",
    "没有收尾的围栏吃到文末": FENCE + "\n# 注释\n# 还有注释\n",
    "更长的同种围栏才能收尾": BT * 4 + "\n# 注释\n" + FENCE + "\n# 还在块里\n" + BT * 4 + "\n# 出来了\n",
    "两种围栏混用": FENCE + "\n# 注释\n~~~\n# 仍算注释\n" + FENCE + "\n# 真标题\n",
    "末尾换行": "# 标题\n\n" + FENCE + "\n# 注释\n" + FENCE + "\n",
}

for name, doc in CORPUS.items():
    chk("C1 writer._prose_lines 与 md_fence 逐行相同：" + name,
        list(writer._prose_lines(doc)) == list(md_fence.prose_lines(doc)))
    chk("C2 book_import._fenced_mask 与 md_fence 逐行相同：" + name,
        book_import._fenced_mask(doc) == md_fence.fence_mask(doc.split("\n")))
    chk("C3 大纲标题 = 尺子算出来的标题：" + name,
        [h["title"] for h in writer.outline(doc)] == titles_of(doc),
        ([h["title"] for h in writer.outline(doc)], titles_of(doc)))
    chk("C4 切章不会把被遮住的 `#` 当章节：" + name,
        not (masked_titles(doc) & {t for t, _b in book_import.split_markdown(doc)}),
        (masked_titles(doc), [t for t, _b in book_import.split_markdown(doc)]))


# ── D 对照 / 回归 ──────────────────────────────────────────────────
def naive_prose(text):
    """老 md_fence 那把尺子：起栏不看 info string 里的反引号（这条就是要修的那一半）。"""
    fence = None
    out = []
    for line in text.splitlines():
        m = re.match(r"^( {0,3})(" + BT + r"{3,}|~{3,})(.*)$", line)
        f = (m.group(2)[0], len(m.group(2)), m.group(3)) if m else None
        if fence is None:
            if f:
                fence = (f[0], f[1])
                continue
            out.append(line)
            continue
        if f and f[0] == fence[0] and f[1] >= fence[1] and not f[2].strip():
            fence = None
    return out


TRICKY = FENCE + " a" + BT + "b\n# 真标题\n正文\n"
chk("D1 镜子：旧尺子会把 ` ``` a`b ` 之后的行整段吞成代码",
    "# 真标题" not in naive_prose(TRICKY), naive_prose(TRICKY))
chk("D2 新尺子把那一段当正文（对照同一份输入）",
    "# 真标题" in [ln for _i, ln in md_fence.prose_lines(TRICKY)])

# 修的是边角，老口径一条都不能变。
chk("D3 回归：正常围栏仍不进大纲",
    [h["title"] for h in writer.outline(FENCE + "bash\n# 安装\npip install x\n" + FENCE + "\n# 真章节\n")]
    == ["真章节"])
chk("D4 回归：波浪号围栏也认",
    [h["title"] for h in writer.outline("~~~\n# 注释\n~~~\n## 起头\n")] == ["起头"])
chk("D5 回归：收尾必须同种字符",
    [h["title"] for h in writer.outline(FENCE + "\n# 注释\n~~~\n# 仍算注释\n")] == [])
chk("D6 回归：`# 注释` 不当 `# 标题`（导入切章老例子）",
    [h["title"] for h in writer.outline("```bash\n# 安装依赖\npip install x\n```\n\n# 真章节\n正文\n")]
    == ["真章节"])
chk("D7 回归：行号仍按原文数（跳过的块也占行）",
    [h["line"] for h in writer.outline("```bash\n# 安装依赖\npip install x\n```\n\n# 真章节\n")]
    == [6])

print()
if FAIL:
    print("围栏尺子一致性：失败 %d 项" % len(FAIL))
    for n in FAIL:
        print("  ✗ " + n)
    sys.exit(1)
print("围栏尺子一致性：通过 %d 项，失败 0 项" % PASSED)
