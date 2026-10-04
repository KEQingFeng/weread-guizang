#!/usr/bin/env python3
"""归藏门禁：界面文案（对齐 Google 开发者文档那种描述性表述）。

不起服务、不开浏览器：只读 ui.html 的源码，量它的字。守的是这一条：

    设置里的说明文字是「描述这个开关做什么」，不是跟用户聊天。
    因此不该出现语气助词（～啦哦哟呀嘛……）、感叹号、波浪号、颜文字与 emoji。

三条口径：

  · **语气助词与波浪号全文清零** —— 波浪号是「好的～」那类俏皮味的记号，
    要写就写完整句子，不靠尾巴上的符号卖乖。
  · **emoji 只许出现在剪藏正文的清洗正则那一行**（`STRIP`）—— 界面本身一颗都不留。
  · **每条设置说明是一句完整的话，以句号收尾** —— 不给「调低更通透」这种半截标签
    式的写法开口子；描述性表述是整句。

这一份只认静态事实，改文案改错了它会红，改对了不必重跑服务。
"""
import pathlib
import re
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import selftest  # noqa: E402

SRC = (selftest.REPO / "ui.html").read_text(encoding="utf-8")
LINES = SRC.split("\n")

# 语气助词 + 全角波浪号：这些字一出现，多半不是描述而是搭话
TONE = "～啦哦哟呀嘛呗噢嘿诶哈哇嘻"
# 跟着市场话术走的几个词：设置里只描述功能，不催用户动手
HYPE = ("点我", "点这里", "试试", "一下吧")


def emoji_chars(s):
    return [c for c in s if 0x1F000 <= ord(c) <= 0x1FAFF or 0x2600 <= ord(c) <= 0x27BF]


def is_cjk(c):
    return bool(c) and 0x4E00 <= ord(c) <= 0x9FFF


def tilde_on_prose():
    """波浪号贴在汉字边上 —— 「好的~」那种尾巴上的俏皮味。

    只看紧挨着的那一个字：正则里 `[~^]`、模板里 `'~'` 做分隔符、注释里 `h1~h3`
    都不算，因为它们前后不是汉字。这样不必去猜哪一行是代码、哪一行是文案。
    """
    hits = []
    for i, line in enumerate(LINES, 1):
        for m in re.finditer("~", line):
            a = line[m.start() - 1] if m.start() > 0 else ""
            b = line[m.start() + 1] if m.start() + 1 < len(line) else ""
            if is_cjk(a) or is_cjk(b):
                hits.append((i, line.strip()[:70]))
    return hits


FAILED = []
PASSED = 0


def chk(name, cond, extra=""):
    global PASSED
    if cond:
        PASSED += 1
    else:
        FAILED.append(name)
    print(("PASS  " if cond else "FAIL  ") + name + ("" if cond else "   " + repr(extra)))


# ── 1. 全文三条硬口径 ───────────────────────────────────────────
tone_hits = [(i, c) for i, line in enumerate(LINES, 1) for c in TONE if c in line]
chk("全文：语气助词与波浪号一颗都没有", not tone_hits, tone_hits[:8])

emoji_lines = [i for i, line in enumerate(LINES, 1) if emoji_chars(line)]
chk("emoji 只出现在剪藏清洗正则那一行（STRIP）",
    all("STRIP = " in LINES[i - 1] for i in emoji_lines), emoji_lines)

bang_lines = [i for i, line in enumerate(LINES, 1) if "！" in line]
chk("全角感叹号只出现在正则里（不当标点用）",
    all(("RegExp" in LINES[i - 1]) or ("replace(/" in LINES[i - 1]) or ("(/" in LINES[i - 1])
        for i in bang_lines), bang_lines)

prose_tilde = tilde_on_prose()
chk("波浪号不贴在汉字边上（那是「好的～」那类俏皮味）", not prose_tilde, prose_tilde[:6])

# ── 2. 设置弹窗：描述性表述 ─────────────────────────────────────
_pop_from = SRC.index('id="popbody"')
_pop_to = SRC.index("\n<main>")
POP = SRC[_pop_from:_pop_to]

pop_tone = [c for c in TONE if c in POP]
chk("设置区：没有语气助词与波浪号", not pop_tone, pop_tone)
chk("设置区：没有感叹号", "！" not in POP)
chk("设置区：没有 emoji", not emoji_chars(POP), emoji_chars(POP)[:8])
pop_hype = [w for w in HYPE if w in POP]
chk("设置区：不催人动手（点我 / 点这里 / 试试 / 一下吧）", not pop_hype, pop_hype)

# 每条说明是一整句：剥掉标签后以句号收尾，且够长（不是半截标签）。
# 含按钮/输入框/插值的那种「说明」里塞的是控件，不算一句话，跳过。
hints = re.findall(r'class="hint"[^>]*>(.*?)</div>', POP, re.S)
prose_hints = [h for h in hints
               if not any(k in h for k in ("<button", "<input", "<span", "${", "data-idx"))]
short = [re.sub(r"<[^>]+>", "", h).strip() for h in prose_hints
         if len(re.sub(r"<[^>]+>", "", h).strip()) < 10]
chk("设置区：每条说明都够长（是描述，不是一个词）", not short, short)
bad_end = [re.sub(r"<[^>]+>", "", h).strip()[-14:]
           for h in prose_hints
           if not re.sub(r"<[^>]+>", "", h).strip().endswith("。")]
chk("设置区：每条说明都以句号收尾（写成一句完整的话）", not bad_end, bad_end)

sechd = re.findall(r'class="sechd">(.*?)</div>', POP, re.S)
chk("设置区：小节标题不夹语气词与感叹号",
    bool(sechd) and not any((c in s) or ("！" in s) for s in sechd for c in TONE),
    [s for s in sechd if any(c in s for c in TONE)][:5])

# ── 3. 底部个人主界面的「设置」入口还在（这一颗是 1.0.5 挪下来那一颗） ──
chk("个人主界面底部：仍有一颗进入设置的入口", 'id="meSet"' in SRC and ">设置</button>" in SRC)

print("\n文案门禁：%d 项通过，失败 %d 项" % (PASSED, len(FAILED)))
for f in FAILED:
    print("  ✗ " + f)
sys.exit(1 if FAILED else 0)
