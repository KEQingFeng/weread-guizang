#!/usr/bin/env python3
"""换屏一律落在开头：交接动画期间不许再把窗口滚走（1.2.1 · R5）。

用户原话是「每次切换不同页面都会上下闪动的问题 每次更迭要更加自然一些 不需要用户频繁上下
滑动 而是默认在开始的地方」。这一屏一屏是怎么接的在 `paneEl`：

  上一版为了「回来时停在上次那一屏」，记了一份 `paneScroll`，并在**交接动画跑起来之后**
  把窗口滚回那个深度。两段叠在一起就是那下上下闪：卡片正从左往右滑进来，版面却同时往上
  跳；而且每进一屏都要重新找一次位置，用户得再滚一遍才回到要看的地方。

这一轮改成：换屏先把窗口归零，再做交接；那份「停在上次那一屏」的记忆整个删掉。
只读 ui.html 的静态段（`--fast` 也跑），钉这几条：

  A 旧的那份记忆不再存在（paneScroll 一个字都不留）。
  B 归零发生在交接之前，且只在「真的换了屏」那一支里。
  C 本来就在顶部就不发起一次多余滚动。
  D 阅读器那一屏不算它 —— 续读要把人送回读到那一行，那一路发生在 paneEl 之后。
  E 「一屏刚好」那几屏各自栏内滚，整页不动（这才叫不需要频繁上下滑动）。
  F 切回书架仍补一遍揭示（归零后 IntersectionObserver 那一套不能留白）。
"""
import pathlib
import re
import sys

SRC = (pathlib.Path(__file__).resolve().parent.parent / "ui.html").read_text(encoding="utf-8")

checks = []


def chk(name, ok, extra=""):
    checks.append((bool(ok), name, "" if ok else str(extra)[:300]))
    print(("  ok  " if ok else " FAIL ") + name + ("" if ok else "  <- " + str(extra)[:300]))


def fn_body(name):
    m = re.search(r"function\s+%s\s*\([^)]*\)\s*\{(.*?)\n\}" % re.escape(name), SRC, re.S)
    return m.group(1) if m else ""


pane = fn_body("paneEl")
chk("找得到 paneEl（每屏一个常驻 pane 的那处开关）", bool(pane), pane[:120])

# ── A 那份「停在上次那一屏」的记忆删干净了 ──────────────────────
chk("paneScroll 这个变量不再出现（旧记忆整块删掉）", "paneScroll" not in SRC, SRC.count("paneScroll"))
chk("也没有别处悄悄记滚动深度（离开时存 window.scrollY 那一句没了）",
    not re.search(r"paneScroll\[[^\]]+\]\s*=", SRC))

# ── B 归零在交接之前，且只在真的换屏那一支 ──────────────────────
idx_top = pane.find("window.scrollTo(0, 0)")
idx_slide = pane.find("slideView(slideDir, name)")
idx_changed = pane.rfind("if (changed)", 0, idx_top)
chk("换屏时归零这一句在", idx_top >= 0, pane[:200])
chk("交接动画那一句在", idx_slide >= 0, pane[:200])
chk("归零排在交接之前（动画从头到尾待在同一个地方）", 0 <= idx_top < idx_slide,
    {"top": idx_top, "slide": idx_slide})
chk("两句都在 `if (changed)` 这一支里（同屏重渲染不许打回顶部）",
    0 <= idx_changed < idx_top < idx_slide, {"changed": idx_changed, "top": idx_top})
chk("那一支开头就写死 shownPane（换没换过只判一次）",
    re.search(r"if \(changed\) \{\s*shownPane = name;", pane) is not None, pane[200:520])

# ── C 已在顶部就不发起一次多余滚动 ────────────────────────────
chk("归零前先看一眼 scrollY（本来在顶就不动）",
    re.search(r"if \(window\.scrollY\) window\.scrollTo\(0, 0\);", pane) is not None, pane[:300])

# ── D 阅读器那一屏另说：续读要回到读到那一行 ───────────────────
rd = fn_body("rdPaint")
chk("阅读器铺正文时仍把位置送回该停的那一行（归零抢不走它）",
    "sc.scrollTop = y" in rd and "RD.resumeY" in rd, rd[:260])
chk("那一笔偏移只用一次，用完就清（翻同一章不被旧位置拽着）",
    "RD.resumeY = 0;" in rd, rd[:300])
chk("图异步落位会顶走那一行，到齐后补一次（续读落点不能靠运气）",
    "rdHoldResume(bg, y)" in rd and "function rdHoldFix" in SRC)
chk("注释里写明了阅读器不算在归零这一路里（后来人别顺手删）",
    "它自己在渲染时" in pane, pane[200:560])

# ── E 一屏刚好：那几屏整页不动，栏内自己滚 ─────────────────────
ff = re.search(r"document\.body\.classList\.toggle\('feedfit',([^;]+);", SRC)
chk("feedfit 这一处开关还在（哪几屏不进整页滚）", bool(ff), ff)
for name in ("feed", "video", "flomo", "write", "search", "browse"):
    chk("「%s」这一屏整页不动（列表在栏内滚）" % name,
        bool(ff) and "name === '%s'" % name in ff.group(1), ff.group(1) if ff else "")
chk("阅读器整页不动（body.reading 那一路）",
    "classList.toggle('reading', name === 'read')" in pane, pane[:260])
chk("「安娜的档案」不在名单里（它本来就该整页滚）",
    bool(ff) and "anna" not in ff.group(1), ff.group(1) if ff else "")

# ── F 切回书架仍补揭示 ────────────────────────────────────────
chk("切回书架补一遍 revealInView（面板藏着时被全判成不在眼前）",
    "if (name === 'shelf') revealInView();" in pane and idx_slide < pane.find("revealInView()"),
    pane[-260:])

# ── G 交接动画自己不滚（滚的来源只有一处） ─────────────────────
slide = fn_body("slideView")
chk("slideView 里不碰滚动（归零只发生在 paneEl 那一处）",
    "scrollTo" not in slide and "scrollTop" not in slide, slide[:260])
chk("全界面 window.scrollTo 只出现在 paneEl 归零这一处",
    len(re.findall(r"window\.scrollTo\(", SRC)) == 1, re.findall(r"window\.scrollTo\([^\n]*", SRC))

bad = [c for c in checks if not c[0]]
print("\n换屏落点体检：%d 项通过，失败 %d 项" % (len(checks) - len(bad), len(bad)))
for _, n, x in bad:
    print("  ✗ " + n + ("  | " + x if x else ""))
sys.exit(1 if bad else 0)
