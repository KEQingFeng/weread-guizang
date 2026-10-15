#!/usr/bin/env python3
"""地址那一行的字不许被挤掉：订阅框与视频框起步一行，长内容自己长（1.2.1 · R5）。

用户原话是「RSS订阅文本框的文字为自适应 不再挤压 或者默认文字展示正常」。这一格原来是
`height:34px` 写死 + `resize:none`，占位符里还塞了两个 `&#10;`（两行话），于是：

  · 默认状态：一行高的框顶着两行占位符，字被裁掉半截 —— 就是「默认文字展示不正常」；
  · 贴进去 B 站那种「链接 + 一串分享文案」：超出 34px 的部分直接看不见了，
    用户以为没贴全，反复重贴。

改成：占位符一句话（默认那行看得清），起步一行高，内容长了 `clipGrow` 把高度顶到
scrollHeight，长到 CSS 那个上限后交给框自己滚。只读源码的静态段（`--fast` 也跑）：

  A clipGrow 只做一件事：先收回去、再按 scrollHeight 撑开；不是 .one 的那一格不许碰。
  B 上限只写一处（CSS 里那个 max-height），JS 里不重复那个数。
  C 两格都用同一套：#fTa / #vTa 都是 `clipta one` + rows="1"。
  D 占位符是一行话（旧的两行 `&#10;` 不在这两格里）。
  E 该接的都接上了：输入、程序填值、清空之后都补一次。
  F 长链接靠断字，不靠横向滚动条。
"""
import pathlib
import re
import sys

SRC = (pathlib.Path(__file__).resolve().parent.parent / "ui.html").read_text(encoding="utf-8")

checks = []


def chk(name, ok, extra=""):
    checks.append((bool(ok), name, "" if ok else str(extra)[:300]))
    print(("  ok  " if ok else " FAIL ") + name + ("" if ok else "  <- " + str(extra)[:300]))


def rule(sel):
    m = re.search(re.escape(sel) + r"\{([^}]*)\}", SRC)
    return m.group(1) if m else ""


def fn_body(name):
    m = re.search(r"function\s+%s\s*\([^)]*\)\s*\{(.*?)\n\}" % re.escape(name), SRC, re.S)
    return m.group(1) if m else ""


# ── A clipGrow ────────────────────────────────────────────────
grow = fn_body("clipGrow")
chk("clipGrow 在（这一行的自适应只有这一处实现）", bool(grow), grow)
chk("先把高度收回 auto 再量（否则只会越撑越长、缩不回去）",
    "ta.style.height = 'auto';" in grow and grow.index("= 'auto'") < grow.index("scrollHeight"),
    grow)
chk("按 scrollHeight 撑开（内容多少就多高）", "ta.scrollHeight" in grow, grow)
chk("只碰 .one 那一格（写作 / 便签那种多行框不许被顺手压扁）",
    "classList.contains('one')" in grow, grow)

# ── B 数字只写一处 ────────────────────────────────────────────
one = rule(".clipta.one")
chk("单行那一档的样式在（.clipta.one）", bool(one), one)
chk("起步就是一行高：min-height 归零、height 34px",
    "min-height:0" in one and "height:34px" in one, one)
chk("长到上限交给框自己滚（overflow-y:auto + 一个 max-height）",
    "overflow-y:auto" in one and re.search(r"max-height:\d+px", one) is not None, one)
chk("上限这个数全界面只写一次", SRC.count("max-height:118px") == 1, SRC.count("max-height:118px"))
chk("JS 里不重复那个数（clipGrow 只加边框那 2px）",
    "118" not in grow and "max-height" not in grow, grow)
chk("这一档不许拖手柄（resize:none），高度由内容说话", "resize:none" in one, one)

# ── C 两格共用 ────────────────────────────────────────────────
for tid in ("fTa", "vTa"):
    m = re.search(r'<textarea class="clipta one" id="%s" rows="1"' % tid, SRC)
    chk("「%s」用的是同一套（clipta one + rows=1）" % tid, bool(m), m)
chk("没有残留 rows=2/3 那种写死的行数",
    re.search(r'id="(fTa|vTa)"[^>]*rows="[2-9]', SRC) is None)

# ── D 占位符是一行话 ──────────────────────────────────────────
for tid, phrase in (("fTa", "粘贴站点首页或订阅地址"), ("vTa", "视频链接")):
    blk = SRC[SRC.index('id="%s"' % tid) - 120:SRC.index('id="%s"' % tid) + 260]
    chk("「%s」的占位符带着这句：%s" % (tid, phrase), phrase in blk, blk[:220])
    chk("「%s」的占位符里没有换行（一行高的框顶两行话就是挤压）" % tid,
        "&#10;" not in blk, blk[:220])
chk("旧那句两行占位符彻底不在了", "粘贴站点首页&#10;" not in SRC)

# ── E 该接的都接上 ────────────────────────────────────────────
sites = re.findall(r"clipGrow\((?:FEED\.ta|VIDEO\.ta|ta)\)", SRC)
chk("调用点够多（输入 / 程序填值 / 清空之后各补一次）", len(sites) >= 5, sites)
chk("订阅那一格：打字就长", "clipGrow(FEED.ta))" in SRC and
    "FEED.ta.addEventListener('input', () => clipGrow(FEED.ta));" in SRC)
sub = fn_body("feedSubscribe")
chk("订阅成功清空这行后也缩回去（不留一截空高的框）",
    "FEED.ta.value = ''; clipGrow(FEED.ta);" in sub, sub[:220])
chk("视频那一格：输入时会长（input 那一路）",
    "clipGrow(VIDEO.ta);" in fn_body("wireVideo"), fn_body("wireVideo")[:260])
chk("手指从剪贴板捞到链接、以及内置浏览器送回链接，都补一次",
    "clipGrow(VIDEO.ta);" in fn_body("vidFinger")
    and "clipGrow(VIDEO.ta);" in fn_body("vidUseLink"), "")

# ── F 断字 ────────────────────────────────────────────────────
base = rule(".clipta")
chk("长地址会断行而不是横向溢出（word-break:break-all 在那一层）",
    "word-break:break-all" in base, base[:260])
chk("占位符的颜色在（别灰到看不见）", ".clipta::placeholder{color:var(--ink-3)}" in SRC)

bad = [c for c in checks if not c[0]]
print("\n地址行自适应体检：%d 项通过，失败 %d 项" % (len(checks) - len(bad), len(bad)))
for _, n, x in bad:
    print("  ✗ " + n + ("  | " + x if x else ""))
sys.exit(1 if bad else 0)
