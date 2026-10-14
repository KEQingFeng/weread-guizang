# -*- coding: utf-8 -*-
"""导入书的切章：围栏代码块里的 `#` 是注释，不许被当成章节标题。

用户从别处拿到的 Markdown 书常常带代码示例，而代码里的注释就是一行 `# 安装依赖`。
`split_markdown` 原来对全篇一视同仁 —— 先数各层 `#` 出现几次、再照那层切开，于是
一段代码里的注释会：

  · 把「这一层至少两段」凑够，选错切分的层级；
  · 自己变成一章，标题写着「安装依赖」「运行」；
  · 把原本完整的一章从注释处剪成好几块，正文跟着碎掉。

口径按 CommonMark 的围栏：开栏是行首（最多三个空格）三个以上的 ` 或 ~，
收栏必须同种字符、不短于开栏、后面只剩空白；反引号开栏的 info string 里再出现
反引号就不算开栏；没有收栏的块一直吃到文末。

全部是纯函数，不联网、不落盘、不开浏览器。

跑法：.venv/bin/python tests/check_import_fence.py
"""
import atexit
import os
import shutil
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

SANDBOX = tempfile.mkdtemp(prefix="gz-fence-check-")
atexit.register(lambda: shutil.rmtree(SANDBOX, ignore_errors=True))
os.environ["GUIZANG_DATA"] = os.path.join(SANDBOX, "data")

import book_import as B  # noqa: E402

FAIL = []
CHECKED = []


def chk(name, cond, extra=""):
    CHECKED.append(name)
    print(("PASS  " if cond else "FAIL  ") + name + ("" if cond else "   " + repr(extra)[:240]))
    if not cond:
        FAIL.append(name)


BT = chr(96)          # 反引号：三连写容易和 Markdown 记号看混，这里显式拼
FENCE = BT * 3
TILDE = "~" * 3


# ── A _fenced_mask：哪些行属于围栏代码块 ────────────────────────────
chk("A1 反引号围栏：开栏 / 内容 / 收栏都算块内，收栏下一行不算",
    B._fenced_mask(FENCE + "\ncode\n" + FENCE + "\n# 标题") == [True, True, True, False],
    B._fenced_mask(FENCE + "\ncode\n" + FENCE + "\n# 标题"))
chk("A2 波浪号围栏一视同仁",
    B._fenced_mask(TILDE + "\n# 注释\n" + TILDE + "\n# 标题") == [True, True, True, False])
chk("A3 没收栏的围栏一直吃到文末",
    B._fenced_mask(FENCE + "\n# 注释\n正文\n# 还是代码") == [True, True, True, True])
chk("A4 收栏必须同种字符：`~` 关不掉反引号开的块",
    B._fenced_mask(FENCE + "\n# 代码\n" + TILDE + "\n# 仍算代码") == [True, True, True, True])
chk("A5 收栏不短于开栏：三连收不了四连",
    B._fenced_mask(BT * 4 + "\n# 代码\n" + FENCE + "\n# 仍在块内") == [True, True, True, True])
chk("A6 更长的同种围栏可以收尾",
    B._fenced_mask(FENCE + "\n# 代码\n" + BT * 4 + "\n# 出了块") == [True, True, True, False])
chk("A7 反引号开栏的 info 里再有反引号就不算开栏（CommonMark）",
    B._fenced_mask(FENCE + " a" + BT + "b\n# 这是标题") == [False, False],
    B._fenced_mask(FENCE + " a" + BT + "b\n# 这是标题"))
chk("A8 最多三个前导空格仍认得；四个空格不算围栏",
    B._fenced_mask("   " + FENCE + "\n# 块内\n   " + FENCE + "\n# 块外") == [True, True, True, False]
    and B._fenced_mask("    " + FENCE + "\n# 标题") == [False, False])
chk("A9 收栏后面还有别的东西就不算收栏",
    B._fenced_mask(FENCE + "\n# 块内\n" + FENCE + " x\n# 仍在块内") == [True, True, True, True])
chk("A10 info string 可以有内容（```python）",
    B._fenced_mask(FENCE + "python\n# 注释\n" + FENCE) == [True, True, True])


# ── B split_markdown：注释不当章节，正文不丢 ────────────────────────
CODE_BOOK = (
    "# 第一章 起步\n\n"
    + "正文一。\n\n"
    + FENCE + "python\n"
    + "# 安装依赖\n"
    + "pip install x\n"
    + "# 运行\n"
    + "python x.py\n"
    + FENCE + "\n\n"
    + "## 小节\n\n"
    + "正文二。\n\n"
    + "# 第二章 收尾\n\n"
    + "正文三。\n"
)

parts = B.split_markdown(CODE_BOOK)
chk("B1 代码块里的 `# 安装依赖` / `# 运行` 不是章标题",
    [t for t, _b in parts] == ["第一章 起步", "第二章 收尾"], [t for t, _b in parts])
chk("B2 代码块内容留在所属章的正文里，没被剪掉",
    "# 安装依赖" in parts[0][1] and "pip install x" in parts[0][1]
    and "## 小节" in parts[0][1], parts[0][1][:80])
chk("B3 切出来的章数与正常书一致（不多不少）", len(parts) == 2, len(parts))

tilde_parts = B.split_markdown(
    "# 一\n\n" + TILDE + "\n# 注释\n" + TILDE + "\n\n# 二\n\n正文\n")
chk("B4 波浪号围栏里的注释同样不当章", [t for t, _b in tilde_parts] == ["一", "二"],
    [t for t, _b in tilde_parts])

unterm = B.split_markdown("# 一\n\n" + FENCE + "\n# 还没收尾\n")
chk("B5 没收尾的围栏之后没有新章（# 还是代码）", [t for t, _b in unterm] == ["一"],
    [t for t, _b in unterm])

# 用 `##` 当章的书里，代码注释是两个 `#`：旧切法会先挑中第 1 层，把注释当章。
hash_chapters = (
    "## 第一章\n\n正文一\n\n"
    + FENCE + "\n# 注释甲\n# 注释乙\n" + FENCE + "\n\n"
    + "## 第二章\n\n正文二\n")
hp = B.split_markdown(hash_chapters)
chk("B6 代码注释不会把切分层级从 `##` 拉到 `#`",
    [t for t, _b in hp] == ["第一章", "第二章"], [t for t, _b in hp])

normal = B.split_markdown("# 甲\n\n一段\n\n# 乙\n\n二段\n")
chk("B7 正常 md 照旧按一级标题切（回归）", [t for t, _b in normal] == ["甲", "乙"],
    [t for t, _b in normal])

nested = B.split_markdown("# 甲\n\n一段\n\n## 甲一\n\n小段\n\n# 乙\n\n二段\n")
chk("B8 `##` 小节不拆成章，仍留在所属章里",
    [t for t, _b in nested] == ["甲", "乙"] and "## 甲一" in nested[0][1],
    [t for t, _b in nested])

no_head = B.split_markdown("一段没有标题的正文。\n\n" + FENCE + "\n# 假标题\n" + FENCE + "\n")
chk("B9 通篇没有真标题时退回按字数分块，代码注释不冒充章",
    len(no_head) == 1 and no_head[0][0] == "", no_head)

plain = B.split_plain(
    "第一章 开始\n\n正文。\n\n" + FENCE + "\n第二章 假章节\n" + FENCE + "\n\n第二章 真的继续\n\n正文二。\n")
chk("B10 纯文本：围栏里的「第二章」不算章",
    [t for t, _b in plain] == ["第一章 开始", "第二章 真的继续"], [t for t, _b in plain])


# ── C 镜子：不遮住代码块的旧数法确实会多数出标题 ────────────────────
_LINES = CODE_BOOK.split("\n")
naive_lv1 = sum(1 for ln in _LINES
                if (m := B._H_MD.match(ln)) and len(m.group(1)) == 1)
masked_lv1 = sum(1 for ln, code in zip(_LINES, B._fenced_mask(CODE_BOOK))
                 if not code and (m := B._H_MD.match(ln)) and len(m.group(1)) == 1)
chk("C1 镜子：整篇直接数一级 `#` 行会数出 4 条（含两条代码注释）",
    naive_lv1 == 4, naive_lv1)
chk("C2 镜子：遮住代码块之后只剩 2 条真标题",
    masked_lv1 == 2, masked_lv1)


# ── D 接线：切章那条路真的用上了这把尺子 ────────────────────────────
SRC = (REPO / "book_import.py").read_text(encoding="utf-8")
chk("D1 _split_at 会跳过代码块里的行", "if mask[i]:" in SRC)
chk("D2 split_markdown 数标题时也跳过", "for ln, in_code in zip" in SRC)

bad = FAIL
print("\n导入书切章体检：%d 项通过，失败 %d 项" % (len(CHECKED) - len(bad), len(bad)))
for n in bad:
    print("  ✗ " + n)
sys.exit(1 if bad else 0)
