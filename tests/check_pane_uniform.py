# -*- coding: utf-8 -*-
"""切页一致性：每个视图脚下是同一块舞台，不许按页换宽度。

用户报的是「前几个页面正常，突然出现一个左右变动极大的情况，希望每个页面变化一致」。
根子在 main 的 max-width：书架 / 笔记 / 统计这些留在 1080，而订阅 / 视频 / 便签 / 写作 /
搜索 / 阅读器 / 内置浏览器被单独放宽到 1440 —— 侧边栏连同整页左右边随页面来回横跳。
另一条横移来源是滚动条：整页滚的屏有、定高「一屏刚好」的屏没有，一有（约 15px）一没有
整页也要挪一下。

这一套把「一致性」钉成会失败的检查（全静态，不必起浏览器）：

  A 舞台宽度 —— --main-w 只有一处定义；main 的 max-width 只认它；除 main 自己那条外，
     任何选择器（含 body.feedfit / body.reading 这类按页开的口子）都不许再给 main 写
     max-width。上一轮正是这两条按页开的口子，改之前这一套会逮住。
  B 滚动条 —— html 要有 scrollbar-gutter:stable，免得有 / 无滚动条把整页推一条宽度。
  C 常驻视图 —— #view 不再被 innerHTML 整块抹掉（那是「点一下闪一下」的根），
     视图靠 PANES 常驻、只挪 hidden；隐藏用 [!hidden]{display:none !important} 焊死。
  D 交接动画 —— 同一块 pane 上只留一条在跑（连点不许反复拽回半透明），
     且「减少动态效果」时 WAAPI 自己也要关掉。

用法: .venv/bin/python tests/check_pane_uniform.py
"""
import pathlib
import re
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent
UI = (REPO / "ui.html").read_text(encoding="utf-8")

checks = []


def chk(name, ok, extra=""):
    checks.append((bool(ok), name))
    print(("  ok  " if ok else " FAIL ") + name + ("" if ok else "  <- " + str(extra)[:300]))


# 只取 <style> 里那一块 CSS，免得把 JS 里同名的字符串也算进来。
m = re.search(r"<style>(.*?)</style>", UI, re.S)
CSS = m.group(1) if m else ""
chk("界面里有 <style> 块", bool(CSS))

# ── A 舞台宽度 ────────────────────────────────────────────────
main_w = re.findall(r"--main-w\s*:\s*([^;]+);", CSS)
chk("--main-w 只定义一次", len(main_w) == 1, main_w)
chk("--main-w 是 min(1440px, 100%)（够宽的那一档，阅读器三栏与订阅三栏都放得下）",
    bool(main_w) and "1440px" in main_w[0] and "100%" in main_w[0], main_w)

# 所有「作用于 main」的规则里写没写 max-width。
main_rules = []
for body in re.findall(r"([^{}]+)\{([^{}]*)\}", CSS):
    sel, decl = body[0].strip(), body[1]
    # 选择器里 main 必须是最后一个复合选择器（main / body.x main），别把 .mainx 也算进来。
    if re.search(r"(^|[\s,>~+])main\s*$", sel) and "max-width" in decl:
        main_rules.append((sel, re.search(r"max-width\s*:\s*([^;]+);", decl).group(1).strip()))
chk("main 的 max-width 只写在一条规则里", len(main_rules) == 1, main_rules)
chk("main 的 max-width 认 --main-w",
    len(main_rules) == 1 and main_rules[0][1] == "var(--main-w)", main_rules)
bad = [r for r in main_rules if r[1] != "var(--main-w)"]
chk("没有任何按页给 main 另开宽度（body.feedfit / body.reading 那两个口子已封）",
    not bad, bad)

# ── B 滚动条槽位 ─────────────────────────────────────────────
html_decls = ""
for sel, decl in re.findall(r"([^{}]+)\{([^{}]*)\}", CSS):
    if re.search(r"(^|[\s,>~+])html\s*$", sel.strip()):
        html_decls += decl
chk("html 上留了常驻滚动条槽位（scrollbar-gutter:stable）",
    "scrollbar-gutter" in html_decls and "stable" in html_decls, html_decls[:200])

# ── C 常驻视图 ────────────────────────────────────────────────
# 注释里引用旧写法（`$('#view').innerHTML = …`）不算数，只抓还在跑的代码。
def wipe_sites(src):
    hits = []
    for i, line in enumerate(src.splitlines(), 1):
        for mm in re.finditer(r"""\$\(['"]#view['"]\)\.innerHTML\s*=""", line):
            pre = line[:mm.start()]
            if "//" in pre:
                continue                       # 行注释里提一句
            if pre.count("`") % 2 == 1:
                continue                       # 模板串 / 反引号里的引用
            hits.append(i)
    return hits


chk("#view 不再被整块 innerHTML 抹掉（抹一次就是几十张卡一起重生）",
    not wipe_sites(UI), wipe_sites(UI))
chk("视图挂在 PANES 里常驻", "const PANES = {}" in UI and "function paneEl(" in UI)
chk("隐藏用 [!hidden]{display:none !important} 焊死（display:flex 会盖掉默认 hidden）",
    re.search(r"\[hidden\]\s*\{\s*display\s*:\s*none\s*!important", CSS) is not None)
chk("切换只挪 .vpane 的 hidden", bool(re.search(r"PANES\[k\]\.hidden\s*=", UI)))

# ── D 交接动画 ────────────────────────────────────────────────
chk("同一块 pane 上只留一条动画在跑（playState === 'running' 时续着不重开）",
    "slideAnim" in UI and "playState === 'running'" in UI)
chk("「减少动态效果」时 WAAPI 也得自己关",
    "prefersReduced()" in UI and "prefers-reduced-motion:reduce" in UI)

FAIL = [n for ok, n in checks if not ok]
print()
print("切页一致性：%d 项，失败 %d 项" % (len(checks), len(FAIL)))
for n in FAIL:
    print("  ✗ " + n)
sys.exit(1 if FAIL else 0)
