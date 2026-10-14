# -*- coding: utf-8 -*-
"""围栏代码块：`# 注释` 不许被当成 `# 标题`（一把尺子 + 两处接线）。

Markdown 里这两行长得一模一样，区别只在「在不在围栏代码块里」。归藏有两处是
「拿正文当结构读」的：

  · ai_sum.head_tree —— 模型没答 / 答得不像树时，拿这一章 / 这一篇原文兜一张
    思维导图。它把 `^#{1,4} ` 当分支，代码块里的井号就成了并不存在的章节。
  · book_notes._headings —— 笔记条目正文里的一到三级标题，挂成导图的子分支。
    条目里贴一段带注释的代码是常事，同样会凭空多出小节。

两处原先都是「逐行跑正则」，谁也没跳代码块。这一套先钉死尺子本身（两串围栏、
收尾必须同种字符且不短于开头、没等到收尾吃到文末、行号仍按原文数、四格缩进不算
围栏），再钉死上面两处真的走这把尺子 —— 只有尺子对、接线没接，等于没修。

全程纯逻辑、不联网、不开浏览器，所以归 --fast。
"""
import os
import re
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ROOT = tempfile.mkdtemp(prefix="guizang-mdfence-")
os.environ["GUIZANG_DATA"] = os.path.join(ROOT, "cache")
os.environ["GUIZANG_BOOKS"] = os.path.join(ROOT, "books")

import ai_sum                                                     # noqa: E402
import book_notes                                                 # noqa: E402
import md_fence                                                   # noqa: E402

FAIL = []
PASSED = 0


def chk(name, cond, extra=""):
    global PASSED
    print(("PASS  " if cond else "FAIL  ") + name + ("" if cond else "   " + repr(extra)[:300]))
    if cond:
        PASSED += 1
    else:
        FAIL.append(name)


def kept(text):
    """只留正文行内容，丢掉行号 —— 大多数断言关心的是「哪些行还在」。"""
    return [ln for _i, ln in md_fence.prose_lines(text)]


# ── A. 尺子本身 ──────────────────────────────────────────────
chk("围栏里的行整段跳过（含起收两行）",
    kept("前\n```\n# 注释\npip install x\n```\n后") == ["前", "后"],
    kept("前\n```\n# 注释\npip install x\n```\n后"))

chk("起围栏带信息串（```python）也算围栏，信息串不进正文",
    kept("```python\n# 注\n```") == [], kept("```python\n# 注\n```"))

chk("波浪号围栏同样认",
    kept("~~~\n# 注\n~~~\n正文") == ["正文"], kept("~~~\n# 注\n~~~\n正文"))

chk("收尾必须同种字符：~~~ 里的 ``` 不结尾",
    kept("~~~\n# 注\n```\n# 还在块里\n~~~\n正文") == ["正文"],
    kept("~~~\n# 注\n```\n# 还在块里\n~~~\n正文"))

chk("收尾不短于开头：` ```` ` 里的 ``` 不结尾",
    kept("````\n# 注\n```\n# 还在块里\n````\n正文") == ["正文"],
    kept("````\n# 注\n```\n# 还在块里\n````\n正文"))

chk("收尾后面有别的字就不算收尾",
    kept("```\n# 注\n``` 还没完\n# 还在块里\n```\n正文") == ["正文"],
    kept("```\n# 注\n``` 还没完\n# 还在块里\n```\n正文"))

chk("等不到收尾就一直吃到文末（块外的行不吞）",
    kept("正文一\n```\n# 注\n正文二") == ["正文一"],
    kept("正文一\n```\n# 注\n正文二"))

chk("四格缩进是缩进代码块，不是围栏（原样吐出来给人判）",
    kept("    ```\n# 标题\n    ```") == ["    ```", "# 标题", "    ```"],
    kept("    ```\n# 标题\n    ```"))

chk("两个反引号不是围栏",
    kept("``\n# 标题\n``") == ["``", "# 标题", "``"], kept("``\n# 标题\n``"))

chk("行号仍按原文数（跳到这一节靠它）",
    [(i, ln) for i, ln in md_fence.prose_lines("一\n```\n代码\n```\n五")]
    == [(1, "一"), (5, "五")],
    [(i, ln) for i, ln in md_fence.prose_lines("一\n```\n代码\n```\n五")])

chk("空 / None 不炸，给空表而不是异常",
    kept("") == [] and kept(None) == [], (kept(""), kept(None)))

chk("fence_mark：不是围栏给 None，是围栏给字符 / 长度 / 信息串",
    md_fence.fence_mark("# 标题") is None
    and md_fence.fence_mark("```python") == ("`", 3, "python"),
    md_fence.fence_mark("```python"))


# ── B. 接线一：ai_sum.head_tree ──────────────────────────────
SRC = ("# 第一章\n\n这一章讲分配。\n\n```python\n# 这只是代码注释\n"
       "- pip install fake\nprint(1)\n```\n\n## 要点一\n- 碎片\n- 合并\n")
tree = ai_sum.head_tree("一本有目录的书", SRC)
labels = [k["label"] for k in tree["kids"]]
chk("head_tree：代码块里的 # 不成分支",
    labels == ["第一章", "要点一"], labels)
chk("head_tree：代码块里的假列表项也不成叶子",
    "pip install fake" not in [x["label"] for k in tree["kids"] for x in k["kids"]],
    tree)
chk("head_tree：块外的列表照旧进图（别把整段都跳没了）",
    [k["label"] for k in tree["kids"][1]["kids"]] == ["碎片", "合并"],
    tree["kids"][1])

unclosed = ai_sum.head_tree("书", "# 标题\n正文。\n```\n# 注释\n- 假列表\n")
chk("head_tree：没等到收尾的围栏也不许漏成结构",
    [k["label"] for k in unclosed["kids"]] == ["标题"], unclosed)
chk("head_tree：围栏前那句正文仍然进图",
    [k["label"] for k in unclosed["kids"][0]["kids"]] == ["正文"], unclosed["kids"][0])

# ── C. 接线二：book_notes._headings ─────────────────────────
body = ("开场一段。\n\n```bash\n# 安装依赖\npip install x\n```\n\n"
        "## 真小节\n\n    # 四格缩进，也不是标题\n\n### 另一个真小节\n")
heads = book_notes._headings(body)
chk("_headings：代码块里的 # 不算条目标题",
    heads == ["真小节", "另一个真小节"], heads)
chk("_headings：四格缩进的 # 也不算（缩进代码块）",
    "四格缩进，也不是标题" not in heads, heads)
chk("_headings：没等到收尾的围栏同样跳过",
    book_notes._headings("正文\n```\n# 注释\n# 还是注释\n") == [],
    book_notes._headings("正文\n```\n# 注释\n# 还是注释\n"))

# ── D. 一把尺子：两处都真的走 md_fence，不是各抄一份正则 ──────
def source(name):
    with open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                           name), encoding="utf-8") as f:
        return f.read()


for mod in ("ai_sum.py", "book_notes.py"):
    s = source(mod)
    chk("%s 走 md_fence.prose_lines（不是自己再写一遍围栏）" % mod,
        re.search(r"from md_fence import .*\bprose_lines\b", s) is not None, mod)

print()
if FAIL:
    print("围栏代码块：失败 %d 项" % len(FAIL))
    for n in FAIL:
        print("  ✗ " + n)
    sys.exit(1)
print("围栏代码块：通过 %d 项，失败 0 项" % PASSED)
