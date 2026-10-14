#!/usr/bin/env python3
"""写作页的结构两件事：大纲（`#` 行）与标题兜底（第一行）。

它们都只读正文，最容易出的错却不在「认不认标题」，而在「把不该认的也认了」：
粘一段带 `# 注释` 的代码进稿子，代码块里的井号被当成分节，大纲里就多出并不存在
的章节；稿子以代码开头时，标题兜底还会把围栏那一串反引号当成标题（`title_from`
返回 ```bash 这种事真的发生过）。导出的 `book_export.md_to_html` 早就懂围栏代码块，
所以这一条同时钉住「导出看到的结构」和「大纲给出的结构」是同一份。

不联网、不动真书库：全部是纯函数，连同导出渲染都在内存里跑。
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import book_export          # noqa: E402
import writer               # noqa: E402

FAIL = []


def chk(name, cond, extra=""):
    print(("PASS  " if cond else "FAIL  ") + name + ("" if cond else "   " + repr(extra)[:220]))
    if not cond:
        FAIL.append(name)


def titles(text):
    return [h["title"] for h in writer.outline(text)]


# ── A 大纲：代码块里的 `#` 不是章节 ─────────────────────────────
CODE_FIRST = "```bash\n# 安装依赖\npip install x\n```\n\n# 真章节\n正文\n"
chk("大纲：反引号围栏里的 `# 注释` 不进大纲",
    titles(CODE_FIRST) == ["真章节"], writer.outline(CODE_FIRST))
chk("大纲：波浪号围栏一视同仁",
    titles("~~~\n# 波浪注释\n~~~\n## 起头\n") == ["起头"])
chk("大纲：行号照原文数（跳过的代码块也占行）",
    [h["line"] for h in writer.outline(CODE_FIRST)] == [6], writer.outline(CODE_FIRST))
chk("大纲：没写收尾的围栏一直吃到文末（CommonMark 口径）",
    titles("```\n未收尾\n# 还是代码\n") == [])
chk("大纲：收尾必须同种字符，`~~~` 关不掉 ` ``` ` 开的块",
    titles("```\n# 代码\n~~~\n# 仍算代码\n") == [])
chk("大纲：更长的同种围栏可以收尾",
    titles("```\n# 代码\n````\n# 出了代码块\n") == ["出了代码块"])
chk("大纲：普通标题仍然一个不落，层级也对",
    titles("# 一\n\n## 二\n") == ["一", "二"]
    and [h["level"] for h in writer.outline("# 一\n\n## 二\n")] == [1, 2])
chk("大纲：七级井号不算标题（与原来一样）",
    titles("####### 不是标题\n# 是\n") == ["是"])

# ── B 标题兜底：围栏与代码都不是标题 ─────────────────────────────
chk("标题：以代码开头时不再返回 ```bash",
    writer.title_from("```bash\necho hi\n```\n# 真标题\n") == "真标题",
    writer.title_from("```bash\necho hi\n```\n# 真标题\n"))
chk("标题：代码块后面第一行正文才是兜底标题",
    writer.title_from("```\necho hi\n```\n这是正文开头\n") == "这是正文开头")
chk("标题：通篇都是代码时给 fallback，不硬凑一行代码",
    writer.title_from("```python\nprint(1)\n```") == "未题名")
chk("标题：普通稿子口径不变（第一行 `#` 优先）",
    writer.title_from("# 我的稿子\n正文") == "我的稿子")
_LONG_PROSE = "这是一段没有标题的开头，但它超过二十四个字了哦"
chk("标题：没有 `#` 时取第一个非空行的前 24 字",
    writer.title_from("\n\n" + _LONG_PROSE + "\n") == _LONG_PROSE[:24])
chk("标题：空稿给 fallback",
    writer.title_from("") == "未题名" and writer.title_from(None) == "未题名")

# ── C 两把尺子对齐：导出认成标题的，大纲也认 ─────────────────────
html = book_export.md_to_html(CODE_FIRST)
chk("对齐：代码块在导出里是 <pre><code>，井号没被渲染成 <h1>",
    "<pre><code" in html and html.count("<h1>") == 1, html[:200])
export_h = sum(html.count("<h%d>" % lvl) for lvl in range(1, 7))
chk("对齐：导出里的标题数与大纲一致",
    export_h == len(writer.outline(CODE_FIRST)),
    (export_h, writer.outline(CODE_FIRST)))

# ── D 接线：两处都走同一个围栏扫描器 ─────────────────────────────
src = open(os.path.join(ROOT, "writer.py"), encoding="utf-8").read()
chk("接线：outline 与 title_from 都用 _prose_lines（围栏规则只有一处）",
    src.count("_prose_lines(") >= 3 and "def _prose_lines" in src,
    src.count("_prose_lines("))

print()
print("写作大纲口径取证：" + ("全部通过" if not FAIL else "%d 项未过：%s" % (len(FAIL), FAIL)))
sys.exit(1 if FAIL else 0)
