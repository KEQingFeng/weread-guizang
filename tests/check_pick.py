#!/usr/bin/env python3
"""归藏静态体检：推荐这一屏只许有六本书，前面不许再横一行字（1.1.0 · R2）。

用户的原话是「优化推荐书籍页面，只出现六本书，前边不得出现任何内容」。这句话在
源码里有两个能一眼钉死的落点，所以不必起浏览器：

  ① 数据这一趟必须真的切到 6。官方 /book/recommend 偶尔一次回十几本，旧的
     `if (list.length >= 6) return list;` 会把整批原样摆上去 —— 用户看到的不是
     「为我推荐六本」而是两排半。所以凡是「够六本」的那条早退路，返回的都得是
     `list.slice(0, 6)`。
  ② 这一屏不许铺页头。`viewPane('pick', '')` 第二个参数是页头 HTML，原来传的是
     `viewHeader('为我推荐')`，于是六本书上面常驻「为我推荐 / 一次 N 本」两行。
     这里按死「传空串」，并顺手挡住有人把 viewHeader 再塞回来。

这一套只读 ui.html，起不了服务也跑得动（归静态段）。
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
    """截一个顶层 function 的函数体：到第一个顶格的 `}` 为止。
    这些函数内部的块都缩进着，所以这个口径够用，也省得引一个解析器。"""
    m = re.search(r"function\s+%s\s*\([^)]*\)\s*\{(.*?)\n\}" % re.escape(name), SRC, re.S)
    return m.group(1) if m else ""


# ── ① 够六本的那条早退路必须切到 6 ─────────────────────────────
picks = fn_body("fetchPicks")
chk("找得到 fetchPicks（推荐这一路的数据入口）", bool(picks))
chk("够六本的早退路返回 list.slice(0, 6)（不会把十几本原样摆上去）",
    "return list.slice(0, 6);" in picks and "if (list.length >= 6)" in picks,
    picks[:220])

# 一眼看死：那条早退路不许是裸的 return list。
early = re.search(r"if\s*\(list\.length\s*>=\s*6\)\s*return\s*([^;]+);", picks)
chk("够六本时不许裸 return list（旧写法会把整批摆上来）",
    bool(early) and early.group(1).strip() == "list.slice(0, 6)",
    early.group(0) if early else "没找到早退路")

# ── ② 推荐这一屏不许铺页头 ─────────────────────────────────────
pickview = fn_body("renderPickView")
chk("找得到 renderPickView（推荐这一屏的渲染入口）", bool(pickview))
chk("viewPane('pick', '')：这一屏不铺页头", "viewPane('pick', '')" in pickview,
    pickview[:200])
chk("renderPickView 里不再出现 viewHeader（标题 / 计数都让掉）",
    "viewHeader" not in pickview, pickview[:200])

# 骨架也是六张：冷启动先看到的那一排，数量得跟成品一致。
chk("冷启动骨架是 skelCards(6)（先看到的就是六张）",
    "skelCards(6)" in pickview, pickview[:200])

# ── ③ 推荐 / 搜索的书都能直接加进微信书架（1.2.1 · R4-9，本轮扩到搜索）──
# 卡片动作条上的「加入书架」不是另起一套：复用详情页那颗的 addToShelf，
# 状态判据也复用 onOwnShelf（拿到过真书架就以它为准）。
# 名字从 pickShelfButtons 改成 addShelfButtons —— 因为它不再只服务推荐那一屏。
paint = fn_body("paintPick")
chk("paintPick 铺完卡会补书架按钮（addShelfButtons）",
    "addShelfButtons(grid)" in paint, paint[:220])
psb = fn_body("addShelfButtons")
chk("找得到 addShelfButtons（卡片书架动作，推荐与搜索共用）", bool(psb))
chk("书架按钮复用 addToShelf（与详情页同一套写回）",
    "addToShelf(t.store, t.title, b)" in psb, psb[:240])
chk("书架按钮认 cardIds 里的书城短 id（阅读器 id 加不了）",
    "cardIds.get" in psb and "/^\\d{5,20}$/" in psb, psb[:240])
chk("已在书架的显「已在书架」并禁用（不重复写）",
    "onOwnShelf(t.store)" in psb and "b.disabled = own" in psb, psb[:240])
chk("老节点复用时不重复补按钮（.tkadd 只一颗）",
    "strip.querySelector('.tkadd')" in psb, psb[:240])
chk("按钮挂在卡片动条 .strip 上（沿用摊开动作条那套交互）",
    "querySelector('.strip')" in psb, psb[:240])
# 用户这条是「现在微信搜索的书籍也支持加入微信书架了」：搜索结果那一路必须真接上，
# 而不是只有推荐还在用 —— 断在 searchWeRead 里（铺完 grid 之后补一次）。
sw = re.search(r"async function searchWeRead\(.{0,1800}?\n  \}", SRC, re.S)
chk("微信读书搜索那一路也补了书架按钮（searchWeRead → addShelfButtons）",
    bool(sw) and "fillGrid(g, books)" in sw.group(0)
    and "addShelfButtons(g)" in sw.group(0), sw.group(0)[:260] if sw else "没找到 searchWeRead")
chk("pickShelfButtons 这个旧名字不再出现（改名要改干净，别留两副本）",
    "pickShelfButtons" not in SRC)

bad = [c for c in checks if not c[0]]
print("\n推荐页体检：%d 项通过，失败 %d 项" % (len(checks) - len(bad), len(bad)))
for _, n, x in bad:
    print("  ✗ " + n + ("  | " + x if x else ""))
sys.exit(1 if bad else 0)
