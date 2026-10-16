#!/usr/bin/env python3
"""封面要「滚到眼前才去取」，不是一进书架就把一整屏封面全砸出去。

这个仓库的瀑布书架每批渲染 60 本，而一屏只放得下十几本。2026-10-15 复现：
进入书架时 60 个 `/api/cover` 请求同时发出（实测 peak concurrency 60），
而首屏可见的只有 12 张 —— 剩下 48 张还在视口外 1250px 以外的地方。
根因是封面图在**游离节点**上就被赋了 `src`（`extCoverImg()` 里那一行），
浏览器一旦给它地址就立刻发出请求：此时卡片还没挂进文档、也不知道自己会落
在哪儿，于是 `loading="lazy"` 形同虚设（Chromium 的懒加载判据是「元素在文档
里的位置」，游离元素没有位置，只能当「马上就要看」处理）。

后果不是「慢一点」：
  · 一屏封面的到达时间被 48 个看不见的请求排队拖慢，用户报「封面半天出不来」；
  · `/api/cover` 会走 `ensure_cover()`，封面没落地的那一批要真去后端取图，
    于是每个请求都占着后端的图床带宽与超时窗口——书架一开就 60 个并发；
  · 封面接口原本回 `Cache-Control: no-store`（实测整页重载后照样 60 个请求），
    这一次性全砸出去的开销每个回合都要重付一遍。

修法两件事，缺一不可：
  ① 先只把地址暂存在 `data-cover`，等卡片挂在文档上再由 `coverField()` /
     `paintCovers()` 按「滚到视口附近」才赋 `src`；
  ② 只按「肉眼能看到的 ±一屏」预留，滚动到跟前再补 —— 这是唯一能同时满足
     「一屏封面立即可见」与「不预取看不见的」的口径。

这一套不依赖浏览器：只读 `ui.html` 源码静态钉住接线（与 check_covers.py 同一路子）。
请求条数那一半没有留成常驻门禁（要在真浏览器里铺 60 本带封面的货架，还得喂一张解得开的
JPEG —— 假字节的图会在 `img.onerror` 上被撤掉，量出来全是「滚一路也不亮」，是货架自己的
坑）。2026-10-16 手工量过一遍：420px 高的视口进书架，首屏 5 张全亮、两屏开外 0 张点亮、
一路滚到底 60 张亮齐，全程 `/api/cover` 正好 60 条，没有白敲的一次。
"""
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
SRC = (ROOT / "ui.html").read_text(encoding="utf-8")

checks = []


def chk(name, ok, extra=""):
    checks.append((bool(ok), name, "" if ok else str(extra)[:300]))
    print(("  ok  " if ok else " FAIL ") + name + ("" if ok else "  <- " + str(extra)[:300]))


def fn_body(name):
    """截出 `function <name>(...) { ... }` 的函数体（到行首的右花括号为止）。"""
    m = re.search(r"function\s+%s\s*\([^)]*\)\s*\{(.*?)\n\}" % re.escape(name), SRC, re.S)
    return m.group(1) if m else ""


def _has_listener(evt):
    """"这个事件有没有人接" —— addEventListener('evt'…) / .on'evt' 都算。"""
    return bool(re.search(r"addEventListener\(\s*['\"]%s['\"]" % re.escape(evt), SRC))


def _listener_body(evt):
    """把 `addEventListener('evt', handler…)` 那一小段取出来，好断言 handler 里接了什么。

    从 addEventListener 后那个左括号开始配平，取到配平为止 —— 贪心匹配会一路吃到
    文件尾，把后面别人的监听也算进来。
    """
    m = re.search(r"addEventListener\(\s*['\"]%s['\"]" % re.escape(evt), SRC)
    if not m:
        return ""
    i = SRC.find("(", m.start())
    if i < 0:
        return ""
    depth = 0
    for j in range(i, min(len(SRC), i + 3000)):
        if SRC[j] == "(":
            depth += 1
        elif SRC[j] == ")":
            depth -= 1
            if depth == 0:
                return SRC[m.start():j + 1]
    return SRC[m.start():m.start() + 400]


# ── ① 封面不再在游离节点上被赋 src ──────────────────────────────
ext = fn_body("extCoverImg")
chk("extCoverImg：只有懒加载标记，不再当场赋 src（游离节点一赋 src 浏览器立刻就发请求）",
    "im.loading = 'lazy'" in ext and "im.decoding = 'async'" in ext
    and "im.referrerPolicy = 'no-referrer'" in ext, ext[:240])
chk("extCoverImg：地址改暂存 data-cover，由视口调度器择时点亮",
    "dataset.cover" in ext and not re.search(r"^\s*im\.src\s*=", ext, re.M), ext[:240])
chk("extCoverImg：仍然走 coverSrc 归一（协议相对地址补 https 那条收口没被拿掉）",
    "coverSrc(" in ext, ext[:240])

# ── ② 有一处「滚到眼前才点亮」的调度器 ────────────────────────────
paint = fn_body("paintCovers")
chk("paintCovers 存在（封面总开关：把该亮的一批点亮）", bool(paint), "找不到 paintCovers")
chk("paintCovers：只挑还没亮过的（img[data-cover]），已点亮的不重发请求",
    "img[data-cover]" in paint and "getAttribute('src')" in paint, paint[:300])
chk("封面点亮范围：视口上下各留一屏，不整墙一次点亮",
    ("h + m" in paint or "h+m" in paint) and "-m" in paint, paint[:300])

field = fn_body("coverField")
chk("coverField 存在（一本书的封面按需点亮）", bool(field), "找不到 coverField")

# ── ③ 这条调度真的接上了已有的兜底路径（不另起炉灶） ───────────────
chk("接线：revealInView 里顺手点亮封面（滚动 / 切回书架 / 后台标签页都从这儿过）",
    "paintCovers" in fn_body("revealInView"), "revealInView 没调 paintCovers")
chk("接线：resize 时补一遍（放大窗口会多露一批卡）",
    _has_listener("resize") and "paintCovers" in _listener_body("resize"),
    "resize 监听没接 paintCovers")
chk("接线：标签页切回来补一遍（后台时 IntersectionObserver 不回调，封面会全空着）",
    _has_listener("visibilitychange") and "paintCovers" in _listener_body("visibilitychange"),
    "visibilitychange 没接 paintCovers")
chk("接线：书架每次建卡/重画之后立刻补一次（首屏不能等第一次滚动才点亮）",
    "paintCovers" in fn_body("revealOnScroll") or "paintCovers" in fn_body("renderShelfGrid"),
    "renderShelfGrid / revealOnScroll 都没调 paintCovers")

# ── ④ 与原先那两代收口不能互相拆台 ───────────────────────────────
chk("逾期图床兜底还在：远端封面 403 时换 /api/cover 再试一次",
    "let alt = t.coverAlt || ''" in SRC
    and "if (alt) { const u = alt; alt = ''; img.src = u; return; }" in SRC,
    "shelfCard 的 onerror 退路被改坏了")
chk("Referer 收口还在：封面仍统一带 no-referrer",
    "im.referrerPolicy = 'no-referrer'" in ext, "extCoverImg 丢了 referrerPolicy")
chk("首字书脊兜底还在：封面全部挂掉也留一个形状（不留破图）",
    "spineHue(" in SRC and "className = 'spine'" in SRC, "生成式封面兜底被拿掉了")

# ── ⑤ 叠卡那一面：画外那 53 张也不算「在画面里」 ──────────────────
# saway / sgone 只有 opacity:0，布局盒还在、也落在视口里，offsetParent 判不住它 ——
# 2026-10-16 实测进一次叠卡书架，60 个 /api/cover 一次性全发出去。这一组按叠卡
# 自己的那条路（paintStackCovers）钉，不拿瀑布那把尺子套它。
chk("叠卡走自己的点亮口 paintStackCovers（不拿 paintCovers 套）",
    "function paintStackCovers(" in SRC, SRC[-400:])
_pa = fn_body("applyStack")
chk("认画外那两枚标记：saway / sgone 一律不发请求",
    "saway" in fn_body("paintStackCovers") and "sgone" in fn_body("paintStackCovers"),
    fn_body("paintStackCovers"))
chk("点亮只认那两个类（判据只留 applyStack 打的那一处，不另算距离）",
    fn_body("paintStackCovers").count("contains(") == 2
    and "viewportTransform" not in fn_body("paintStackCovers"),
    fn_body("paintStackCovers"))
chk("接线：摆完位置就把画面那几张点亮（翻一页也从同一处补）",
    "paintStackCovers" in fn_body("renderShelfStack")
    and "paintCovers" not in fn_body("renderShelfStack"),
    fn_body("renderShelfStack")[-300:])

# ── ⑥ 别把加固做回「一律立刻取」——那一屏的量也钉住 ───────────────
chk("detail 大图与其它屏的封面不受影响：它们本来就要立刻显示",
    "esc(coverSrc(coverUrl))" in SRC, "详情页大图那条路被误伤了")

bad = [c for c in checks if not c[0]]
print("\n封面按需加载体检：%d 项通过，失败 %d 项" % (len(checks) - len(bad), len(bad)))
for _, n, x in bad:
    print("  ✗ " + n + ("  | " + x if x else ""))
sys.exit(1 if bad else 0)
