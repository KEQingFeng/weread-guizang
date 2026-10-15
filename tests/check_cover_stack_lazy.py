#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""叠卡书架也只点亮扇形里看得见的封面，不是把整批一起砸出去。

`check_cover_lazy.py` 修的是瀑布那一面：封面不在游离节点上赋 src，改由 `paintCovers()`
按「滚到视口附近」择时点亮。但叠卡那一面当时留了一句未经核对的假设 ——
「叠卡一次摊 7 张，都在视口里，所以这里直接把这一屏的封面都点亮」，于是写成了：

    paintCovers(box.querySelector('.sstage'), Infinity);

这句注释的前半句是错的。`renderShelfStack()` 拿到的 `shown` 是**整批**（`shelfShown`
起步就是 `SHELF_BATCH = 60`），`fillGrid()` 把这 60 张全塞进 `.sstage`，`applyStack()`
只是给离得远的那几张贴上 `opacity:0` 的 `saway` / `sgone` —— 它们仍留在文档里，
`.sstage>.wcard` 又是 `position:absolute`，所以 `offsetParent` 不为 null。margin 传
`Infinity` 等于「谁都不用量」，60 张封面一个不落地全点亮。

2026-10-16 用 Playwright + 60 本假书实测（拦 `/api/cover` 记数，1x1 真 PNG 回状）：

    修前  叠卡：60 个 /api/cover 一次发出    瀑布：25 个（视口 ± 一屏）
    修后  叠卡：4 个（光标在最前，扇形 = s0..s3）  翻 4 下 +4（每次翻进来一张）

也就是说：同一条「不预取看不见的那一批」的规矩，在叠卡这一面被漏掉了，用户进一次叠卡
书架照样是 60 个并发。修法是新增 `paintStackCovers()`：只遍历 `stage.children`，跳过
`applyStack()` 自己打上 `saway` / `sgone` 的那几张 —— 判据只留一处，容器查询把 ±3 收窄
成 ±2 / ±1 时也不会分叉；`shelfStep()` 翻完再点一次，新进扇形的那几张就赶上了。

这一套不依赖浏览器：只读 `ui.html` 源码静态钉住接线（与 `check_cover_lazy.py` 同一路子）。
请求条数那一半仍然不常驻（要真浏览器 + 60 本带封面的货架），上面的实测数字写在注释里。
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


stack = fn_body("renderShelfStack")
step = fn_body("shelfStep")
stackpaint = fn_body("paintStackCovers")

# ── ① 前提：叠卡舞台里坐着的是整批，不是 7 张 ────────────────────────
chk("renderShelfStack 把整批 shown 交给 fillGrid（「一次只摊 7 张」那句注释是错的）",
    re.search(r"fillGrid\(\s*box\.querySelector\('\.sstage'\)\s*,\s*shown\s*\)", stack),
    stack[:300])
chk("applyStack 把画外那几张仍留在文档里（opacity:0 的 saway / sgone，不是 display:none）",
    ".sstage>.wcard{--sw:" in SRC and ".saway{opacity:0" in SRC
    and ".sgone{opacity:0" in SRC, "叠卡画外的收法变了")

# ── ② 那个「整批全点亮」的口子必须堵上 ──────────────────────────────
chk("没有任何 paintCovers(...) 调用再把 margin 传成 Infinity（那就是整批全点亮）",
    not re.search(r"paintCovers\([^)]*Infinity", SRC), "还能找到 paintCovers(…, Infinity)")
chk("renderShelfStack 不再用 paintCovers 点叠卡的封面，改用受限的 paintStackCovers",
    "paintStackCovers" in stack and "paintCovers" not in stack, stack[:300])

# ── ③ 受限的点亮器：跳过画出扇形的那几张 ───────────────────────────
chk("paintStackCovers 存在（叠卡封面按扇形点亮的总开关）", bool(stackpaint), "找不到")
chk("paintStackCovers 跳过 saway / sgone（判据复用 applyStack 打的类，不另算一遍距离）",
    "saway" in stackpaint and "sgone" in stackpaint and "continue" in stackpaint,
    stackpaint[:300])
chk("paintStackCovers 只走 stage.children，逐张卡取封面（不对整墙做一次大查询）",
    "stage.children" in stackpaint and "c.querySelectorAll('img[data-cover]')" in stackpaint,
    stackpaint[:300])
chk("paintStackCovers 走的是同一个 coverField（与瀑布共用一处赋 src，不另开一条路）",
    "coverField(" in stackpaint, stackpaint[:300])

# ── ④ 翻页时新进扇形的那几张要赶上 ─────────────────────────────────
chk("shelfStep 翻完（applyStack 之后）补一次 paintStackCovers",
    "applyStack(stage)" in step and "paintStackCovers(stage)" in step,
    "翻进来的卡片会停在没封面的空卡上")
step_paint_at = step.find("paintStackCovers(stage)")
step_stack_at = step.find("applyStack(stage)")
chk("这次补点亮排在 applyStack 之后（先换类，再按新扇形点亮）",
    0 <= step_stack_at < step_paint_at, "顺序反了：会按旧扇形点亮")

# ── ⑤ 瀑布那一面不能被动到 ─────────────────────────────────────────
chk("瀑布那面照旧：renderShelfGrid 仍用 paintCovers(grid) 做视口预留",
    "paintCovers(grid)" in fn_body("renderShelfGrid"), "瀑布的视口预留被误伤")
ext = fn_body("extCoverImg")
chk("extCoverImg 仍然只暂存 data-cover，不在游离节点上赋 src",
    "dataset.cover" in ext and not re.search(r"^\s*im\.src\s*=", ext, re.M), ext[:240])

bad = [c for c in checks if not c[0]]
print("\n叠卡封面按需加载体检：%d 项通过，失败 %d 项" % (len(checks) - len(bad), len(bad)))
for _, n, x in bad:
    print("  ✗ " + n + ("  | " + x if x else ""))
sys.exit(1 if bad else 0)
