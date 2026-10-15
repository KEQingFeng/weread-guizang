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
    · 行号一律按**原文**数，调用方要「跳到这一节」时用的就是它。原文行按 `\\n` 切，
      全仓一处实现（`split_lines`）：`splitlines()` 还认 \\x0c \\u2028 那一串 Unicode
      换行，行号会比切章 / 导出那边多，`前言\\x0c``` 这类还会凭空多出一个开栏行。

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


def split_lines(text):
    """原文行的那把尺子：只按 `\\n` 切。

    为什么不是 `str.splitlines()`：那个还认 \\x0b \\x0c \\x1c-\\x1e \\x85 \\u2028 \\u2029
    这一串「Unicode 换行」，同一段稿子在它眼里行数更多、行号更大 —— 而切章与导出
    两边都是按 `\\n` 切行、行号对着原文算的。差别不是小事：

      · 大纲「跳到这一节」用的是行号，PDF 抽文里的换页符（\\x0c，抽页时的常客）
        每出现一次就把后面的行号推后一行，跳过去就偏一行；
      · 换页符正好挨在 ``` 前面时（`前言\\x0c```），`splitlines` 会凭空切出一个
        「行首是围栏」的行，把后面的真标题整段吞进代码块；而切章那边（按 `\\n` 切、
        这一行是 `前言\\x0c```，行首不是围栏）看得见 —— 又回到了「切章看得见、
        大纲 / 导图看不见」这个本模块就是为了消灭的走散。

    所以两个出口共用这一条：行只按 `\\n` 切，谁也别再 `splitlines`。

    注意这里对空串给 `[""]`（`"".split("\\n")` 就是这样），因为切章要拿 mask 跟
    `text.split("\\n")` 一行行对；而「空文档没有正文行」这层语义由 `prose_lines`
    自己处理（它对空 / None 回空表）。两处要求不同，别再合并。
    """
    return str(text if text is not None else "").split("\n")


def fence_mask(lines):
    """逐行标出「这一行是围栏标记或在块内」；行由调用方切，好和原文行一一对齐。

    行自己切时请走 `split_lines`（只按 `\\n`），别用 `str.splitlines()` —— 那会让
    mask 的行数与切章 / 大纲那边对不上，两处对不齐就等于各判各的。
    """
    return [in_code for _line, in_code in _scan(lines)]


def prose_lines(text):
    """逐行给 (原文行号, 行内容)，整段跳过围栏代码块（含围栏那两行）。

    行号从 1 数起，而且始终对着**原文**（`\\n` 切出来的行）—— 所以要 enumerate
    原始行再筛，不能筛完再编号，否则「跳到这一节」会跳错地方。切行本身走
    `split_lines`：与 `fence_mask` 的调用方（导入切章、导出渲染）同一条，行号才对得上。

    空 / None 回空表（不是「一行空行」）：空文档本来就没有正文行，调用方按行号
    索引时也不必为它防一手。`split_lines` 那边给 `[""]` 是为了切章对齐，不冲突。
    """
    text = "" if text is None else str(text)
    if not text:
        return
    for i, (line, in_code) in enumerate(_scan(split_lines(text)), start=1):
        if not in_code:
            yield i, line
