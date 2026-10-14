#!/usr/bin/env python3
"""控件质感与交互反馈（1.2.1 · R4-5）。

用户的原话是「UI 整体更有质感的控件设计，每次交互过程中都能体现一定的反馈动画效果」。
这件事最容易做歪成「给每颗按钮各补一条 hover」，散、漏、还互相打架。这一套只读 ui.html
源码，把收口后的那套底座钉成会失败的断言：

  ① 动效令牌进 :root（时长 / 缓动 / 按压比例），组件只引用，不再散落魔法数字。
  ② 所有能点的东西共用一条按下反馈：`:active` 缩放，且选择器串用 `:where()` 把权重压到 0
     —— 否则全局那条会把 `.ainavbtn` 的居中位移这类组件细调一起盖掉。
  ③ 键盘焦点 `:focus-visible` 有描边（鼠标点不描）。
  ④ 落点涟漪 `.tapfx`：JS 委托 pointerdown 在指针坐标插一个 fixed 的圆，演完自己移除。
  ⑤ 内容入场 `.fx-in` 与成功回弹 `.fx-pop` 各有一处真实接线（瀑布流卡片 / 推荐卡 /
     加书架按钮），不是只躺在 CSS 里的死代码。
  ⑥ 反馈只许走 transform / opacity / box-shadow / filter，且在 `prefers-reduced-motion`
     下全部关掉 —— 前者是 check_hover_shift 那条「悬停不许顶动版面」的同一把尺子。

只读源码，不起服务、不开浏览器，所以归静态段，`--fast` 也跑。
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


# 只圈我们这一块（从段头到通用的 box-sizing 复位之间），免得误伤别处同名属性。
# 段内自带 `/* 1) … */` 小块注释，所以按起止串切片，不用一条正则会到第一个 /* 就停。
_i = SRC.find("/* ── 控件质感与交互动效")
_j = SRC.find("*{box-sizing:border-box}")
FX = SRC[_i:_j] if 0 <= _i < _j else ""

# ── ① 动效令牌 ────────────────────────────────────────────────
for tok in ("--fx-fast", "--fx-dur", "--fx-out", "--press"):
    chk("动效令牌 %s 进 :root" % tok, re.search(re.escape(tok) + r"\s*:", FX) is not None)
chk("段落确实被截出来（不是空串）", bool(FX) and len(FX) > 400, len(FX))

# ── ② 通用按压反馈（低权重，组件可覆盖） ───────────────────────
press = re.search(r"[^\n]*:active:not\(:disabled\)[^\n]*\{([^}]*)\}", FX)
chk("有一条通用的 :active 按压规则", press is not None)
chk("按压只改 transform（不动宽高 / 边距）",
    press is not None and "transform" in press.group(1)
    and not re.search(r"\b(width|height|margin|padding|top|left)\s*:", press.group(1)))
chk("按压缩放引用令牌 --press", press is not None and "scale(var(--press))" in press.group(0))
chk("通用规则的选择器用 :where() 压权重（不盖组件自己的写法）",
    press is not None and ":where(" in press.group(0))

# ── ③ 键盘焦点 ───────────────────────────────────────────────
chk("有 :focus-visible 描边（键盘走查才画）",
    ":focus-visible" in FX and re.search(r":focus-visible[^{]*\{[^}]*outline\s*:", FX) is not None)

# ── ④ 落点涟漪 ───────────────────────────────────────────────
chk(".tapfx 是 fixed、不吃指针、不占布局",
    re.search(r"\.tapfx\{[^}]*position:fixed", FX) is not None
    and "pointer-events:none" in FX)
chk("涟漪有 @keyframes tapfx", "@keyframes tapfx" in FX)
tap = fn_body("tapfx")
chk("tapfx 在指针坐标插圆（clientX / clientY）",
    "e.clientX" in tap and "e.clientY" in tap, tap[:200])
chk("tapfx 演完自己移除（不往 body 里堆节点）",
    "setTimeout(" in tap and ".remove()" in tap, tap[:220])
chk("tapfx 委托 pointerdown 且 passive（不拖慢滚动）",
    "addEventListener('pointerdown', tapfx" in SRC and "{passive: true}" in SRC)
chk("tapfx 认得出能点的东西（closest(FX_TAP) / .disabled 跳过）",
    "closest(FX_TAP)" in tap and "t.disabled" in tap, tap[:220])

# ── ⑤ 入场与回弹真接线 ───────────────────────────────────────
chk("有 .fx-in 与 @keyframes fxup", ".fx-in{" in FX and "@keyframes fxup" in FX)
chk("有 .fx-pop 与 @keyframes fxpop", ".fx-pop{" in FX and "@keyframes fxpop" in FX)
fxin = fn_body("fxIn")
chk("fxIn 一个节点只演一次（data-fx 记档）", "dataset.fx" in fxin, fxin[:200])
chk("fxIn 演完摘类，别压住悬停的 transform",
    "animationend" in fxin and "classList.remove('fx-in')" in fxin, fxin[:260])
chk("瀑布流卡片走 fxIn", "wall.append(fxIn(nbCard(b), i))" in SRC)
chk("推荐卡走 fxIn", re.search(r"\.\.\.grid\.children\]\.forEach\(\(c, i\) => fxIn\(c, i\)\)", SRC) is not None)
chk("加书架成功走 fxPop", "fxPop(btn);" in fn_body("addToShelf"))

# ── ⑥ 减少动效 + 反馈不动版面 ────────────────────────────────
chk("prefers-reduced-motion 下涟漪 / 入场 / 回弹 / 按压全关",
    "@media (prefers-reduced-motion:reduce)" in FX
    and ".tapfx{display:none}" in FX and ".fx-in{animation:none}" in FX
    and ".fx-pop{animation:none}" in FX)
# 这一块里任何 :hover 都不许改流内属性（与 check_hover_shift 同一口径）。
LAYOUT = ('width', 'height', 'margin', 'padding', 'display', 'flex', 'grid',
          'position', 'top', 'left', 'right', 'bottom', 'font-size', 'line-height',
          'gap', 'inset', 'overflow')
bad_hover = []
for m in re.finditer(r"([^\n{]*:hover[^\n{]*)\{([^}]*)\}", FX):
    hit = [p for p in LAYOUT if re.search(r"(?<![-\w])%s\s*:" % p, m.group(2))]
    if hit:
        bad_hover.append(m.group(1).strip() + " -> " + ",".join(hit))
chk("这一块没有「悬停改流内属性」的规则", not bad_hover, bad_hover)

bad = [c for c in checks if not c[0]]
print("\n控件质感与交互反馈体检：%d 项通过，失败 %d 项" % (len(checks) - len(bad), len(bad)))
for _, n, x in bad:
    print("  ✗ " + n + ("  | " + x if x else ""))
sys.exit(1 if bad else 0)
