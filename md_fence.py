# -*- coding: utf-8 -*-
"""围栏代码块唯一的一把尺子：哪些行在代码块里，不算正文。

为什么必须有这一层
    Markdown 里 `# 注释` 与 `# 标题` 长得一模一样，区别只在「它在不在围栏代码块里」。
    仓库里凡是按 `^#+ ` 认结构的地方（写作大纲、导入书切章、导出渲染，以及这里新接上
    的兜底思维导图与笔记条目子分支），漏掉这一层就会把程序代码的注释当成章节：大纲里
    多出并不存在的分节，导图里挂上一串 `pip install`。这类错误不报错、也不丢数据，
    只是把结构讲错 —— 最不容易被发现，所以尺子只留这一把，谁要认标题谁来过。

CommonMark 里与「是不是正文」有关的那几条
    · 围栏最多缩进三格；四格缩进是**缩进代码块**，不是围栏 —— 那种整行都算代码，
      本模块不管它，原样吐给调用方，调用方的 `^#` 自然命不中它开头的空格；
    · 起围栏是三个以上的反引号或波浪号，两种都认；
    · 反引号起的围栏，它的信息串（```python 里的 python）里不许再出现反引号：
      `` ``` a`b `` 只是普通正文。这一条原先只有 book_import 那把尺子认，md_fence
      漏了，同一行在「导入切章」眼里是正文、在「大纲 / 导图」眼里却成了开栏，
      把后面真正的标题一路吞进代码块。现在两处共用这一份；
    · 收围栏必须**同种字符、不短于起围栏**，且后面不能再有别的东西（缩进以外的空白不算）；
    · 起围栏那行后面的信息串（```python）不是正文，整行跳过；
    · 一直等不到收围栏就吃到文末 —— 「进了块就不出来」，但也不会把块外的正文吞掉；
    · 行号一律按**原文**数，调用方要「跳到这一节」时用的就是它。

上面这四处（写作大纲 / 兜底导图 / 笔记子分支 / 导入切章）都只走这里；谁要认标题谁来过，
别在别处再抄一份状态机 —— 抄两份就会在这些 CommonMark 边角上慢慢走散。

本模块刻意不 import 任何第三方库：门禁要能在没装 playwright 的机器上跑。
"""
import re

# 最多三格缩进 + 三个以上的 ` 或 ~ + 余下那串（信息串 / 空白）。
_FENCE = re.compile(r"^( {0,3})(`{3,}|~{3,})(.*)$")


def fence_mark(line):
    """这一行是围栏标记的话给 (字符, 长度, 后面那串)；不是就给 None。

    CommonMark 对反引号起的围栏另有一条：它的信息串里不许再出现反引号，
    `` ``` a`b `` 因此不是围栏。这一条从前只有 book_import 那把尺子认，md_fence
    漏了，于是同一行会被这里当成开栏、把后面的标题整段吞掉 —— 两处必须是同一把尺子。
    """
    m = _FENCE.match(str(line if line is not None else ""))
    if not m:
        return None
    ch, run, info = m.group(2)[0], len(m.group(2)), m.group(3)
    if ch == "`" and "`" in info:
        return None
    return ch, run, info


def _scan(lines):
    """围栏状态机：吃一串行，逐行给 (行内容, 是否属于围栏行 / 块内行)。

    这是**唯一**一份状态机：`prose_lines` 把 True 的行整段跳掉，`fence_mask` 把它们
    标出来给「要和原文行一一对齐」的调用方（导入切章按 `\\n` 切行、行号要对上），
    两个出口都从这里长出来，规矩只写一遍。
    """
    fence = None
    for line in lines:
        f = fence_mark(line)
        if fence is None:
            if f:
                fence = (f[0], f[1])
                yield line, True
                continue
            yield line, False
            continue
        # 在代码块里：只有「同种字符、不短于开头、后面没别的东西」才算收尾。
        # 收尾那一行自己也是围栏，标成 True。
        if f and f[0] == fence[0] and f[1] >= fence[1] and not f[2].strip():
            fence = None
        yield line, True


def fence_mask(lines):
    """逐行标出「这一行是围栏标记或在块内」；行由调用方切，好和原文行一一对齐。"""
    return [in_code for _line, in_code in _scan(lines)]


def prose_lines(text):
    """逐行给 (原文行号, 行内容)，整段跳过围栏代码块（含围栏那两行）。

    行号从 1 数起，而且始终对着**原文** —— 所以要 enumerate 原始行再筛，
    不能筛完再编号，否则「跳到这一节」会跳错地方。
    """
    text = str(text if text is not None else "")
    for i, (line, in_code) in enumerate(_scan(text.splitlines()), start=1):
        if not in_code:
            yield i, line
