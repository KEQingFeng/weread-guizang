#!/usr/bin/env python3
"""叠卡交互的性能与稳定：快速切书不许卡顿闪烁（1.2.1 · R4-4）。

用户报的原话是「在叠卡交互过程中，快速切书导致页面卡顿闪烁」。这一套只读 ui.html
源码，把那几条会随书量放大成卡顿的写法钉死成可失败的断言 —— 它们都不依赖浏览器，
所以归静态段，`--fast` 也跑：

  ① 扇形里所有卡曾常驻 `will-change:transform,opacity`。一屏上百张就是上百个合成层，
     每翻一本浏览器都要重新合成一遍，翻得越急越堵。改成只在翻动那一下由 `.sstage.anim`
     临时提层（applyStack 里挂、680ms 后摘）。
  ② `applyStack` 曾对台上每一张卡无条件「删 9 个状态类再按距离加回去」，哪怕它这一步
     根本没挪窝。改成 WeakMap 记上一次的档位，位置没变就整段跳过（连 aria 都不写）。
  ③ `shelfStep` 每翻一本都 `querySelectorAll('.wcard.open')` 白扫一遍 DOM，改成先
     `document.querySelector('.wcard.open')` 判有没有抽屉再收。
  ④ 快翻到批尾时每一步都同步 `renderShelfGrid()` 重铺一大片（O(已铺数量)），连翻就是
     一路重活。改成 `growStackSoon()` 把它压到连翻停下之后再做（90ms 去抖）。
"""
import pathlib
import re
import sys

SRC_PATH = pathlib.Path(__file__).resolve().parent.parent
SRC = (SRC_PATH / "ui.html").read_text(encoding="utf-8")

checks = []


def chk(name, ok, extra=""):
    checks.append((bool(ok), name, "" if ok else str(extra)[:300]))
    print(("  ok  " if ok else " FAIL ") + name + ("" if ok else "  <- " + str(extra)[:300]))


def fn_body(name):
    """截出 `function <name>(...) { ... }` 的函数体（到行首的右花括号为止）。"""
    m = re.search(r"function\s+%s\s*\([^)]*\)\s*\{(.*?)\n\}" % re.escape(name), SRC, re.S)
    return m.group(1) if m else ""


# ── ① 常驻合成层 → 只在翻动那一下提层 ───────────────────────────
base = re.search(r"\.sstage>\.wcard\{([^}]*)\}", SRC)
chk("叠卡基座样式还在（找得到 .sstage>.wcard{…}）", bool(base), base)
chk("叠卡卡不再常驻 will-change（上百张合成层就是这么堆出来的）",
    bool(base) and "will-change" not in base.group(1), base.group(1) if base else "")
anim = re.search(r"\.sstage\.anim>\.wcard\{([^}]*)\}", SRC)
chk("翻动那一下才由 .sstage.anim 提层（will-change 挪到这里）",
    bool(anim) and "will-change" in anim.group(1), anim.group(1) if anim else "")

# ── ② applyStack 只动真正换位的卡 ───────────────────────────────
ap = fn_body("applyStack")
chk("applyStack 记上一档并在没动时不碰这张卡（stackPos 跳过）",
    "stackPos.get(c) === cls" in ap and "stackPos.set(c, cls)" in ap
    and re.search(r"stackPos\.get\(c\) === cls\)\s*return", ap), ap[:200])
chk("applyStack 把「删类+加类+写 aria」都收进跳过闸之后",
    ap.index("stackPos.set(c, cls)") < ap.index("classList.remove(...STACK_CLS)")
    and ap.index("classList.remove(...STACK_CLS)") < ap.index("classList.add(cls)")
    and ap.index("c.setAttribute('aria-hidden'") > ap.index("stackPos.set(c, cls)"),
    ap[:260])
chk("applyStack 只在真挪动了才挂合成提示（touched → markStackAnim）",
    "let touched = false" in ap and "touched = true" in ap
    and "if (touched) markStackAnim(stage)" in ap, ap[:200])

# ── markStackAnim：挂得上，也摘得掉 ─────────────────────────────
mk = fn_body("markStackAnim")
chk("markStackAnim 给舞台挂 .anim 并设了收尾时钟",
    "classList.add('anim')" in mk and "setTimeout(" in mk and "clearTimeout" in mk,
    mk[:200])
chk("markStackAnim 收尾时把 .anim 摘掉（合成层不留在台上）",
    "classList.remove('anim')" in mk, mk[:200])

# ── ③ shelfStep：收抽屉先判有没有 ───────────────────────────────
st = fn_body("shelfStep")
chk("shelfStep 只在真有抽屉摊着时才收（不再每翻一本白扫 DOM）",
    "if (document.querySelector('.wcard.open')) closeAllCards();" in st, st[:220])
chk("shelfStep 翻页本身仍同步走完（applyStack 紧跟 shelfCursor = want）",
    "shelfCursor = want;" in st and "applyStack(stage);" in st, st[:200])

# ── ④ 批尾补卡去抖：连翻不每一步都重铺 ─────────────────────────
chk("shelfStep 补下一批时走 growStackSoon（不再同步 renderShelfGrid）",
    "growStackSoon();" in st and "renderShelfGrid();" not in st, st[:260])
grow = fn_body("growStackSoon")
chk("growStackSoon 是去抖的（clearTimeout + setTimeout）",
    "clearTimeout(growStackT)" in grow and "setTimeout(" in grow, grow[:220])
chk("growStackSoon 补卡时守住「还叠在书架上」",
    "shelfMode === 'stack'" in grow and "v === 'shelf'" in grow, grow[:220])

# ── 补卡仍是键控复用，不许退回整台重建 ─────────────────────────
rs = fn_body("renderShelfStack")
chk("叠卡仍走键控复用的 fillGrid，不会整台拆了重铺",
    "fillGrid(box.querySelector('.sstage'), shown)" in rs, rs[:200])

bad = [c for c in checks if not c[0]]
print("\n叠卡性能体检：%d 项通过，失败 %d 项" % (len(checks) - len(bad), len(bad)))
for _, n, x in bad:
    print("  ✗ " + n + ("  | " + x if x else ""))
sys.exit(1 if bad else 0)
