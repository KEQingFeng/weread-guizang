#!/usr/bin/env python3
"""随机漫步：微信读书划线之外，也能抽本机（取书）的读书笔记（1.2.1 · R4-7）。

用户要的是：随机漫步不只从微信读书的划线里抽，本机取书时自己划的线、自己记的
笔记也要能抽到。于是分三处收口：

  · 后端 local_note_items()：扫本机书目录的 notes.json，把 marks / entries
    收成和划线索引同形状的条目（kind 前缀「取书」以示来源）。
  · /api/notes_random 支持 src=all|wx|local；/api/notes_index 多报 local_count，
    没建过微信索引、但本机有划线时随机漫步照样进得去。
  · 前端加「全部 / 微信 / 取书」档位，取书那一路的卡与徽章分色。

不依赖浏览器：后端读源码，前端读 ui.html，归静态段。
"""
import pathlib
import re
import sys

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent
SRC = (ROOT / "ui.html").read_text(encoding="utf-8")
SERVER = (ROOT / "ui_server.py").read_text(encoding="utf-8")

checks = []


def chk(name, ok, extra=""):
    checks.append((bool(ok), name, "" if ok else str(extra)[:300]))
    print(("  ok  " if ok else " FAIL ") + name + ("" if ok else "  <- " + str(extra)[:300]))


def fn_body(name):
    m = re.search(r"function\s+%s\s*\([^)]*\)\s*\{(.*?)\n\}" % re.escape(name), SRC, re.S)
    return m.group(1) if m else ""


# ── ① 后端：本机笔记收成和索引同形状的条目 ──────────────────────
loc = re.search(r"def local_note_items\(.*?\n(?=\n# |\ndef |\nclass )", SERVER, re.S)
loc = loc.group(0) if loc else ""
chk("后端有 local_note_items() 这个入口", bool(loc), "ui_server.py 里找不到")
chk("local_note_items 读本机书目录的 notes.json（book_notes.load_notes）",
    "book_notes.load_notes" in loc, loc[:240])
chk("local_note_items 把 marks 与 entries 两路都收进来",
    "doc.get(\"marks\")" in loc and "doc.get(\"entries\")" in loc, loc[:240])
chk("取书那一路的 kind 带「取书」前缀，来源一眼分得出来",
    '"取书" + book_notes._kind_label' in loc and '"取书笔记"' in loc, loc[:240])
chk("条目带 local:True 标记（前端据此分色）",
    '"local": True' in loc, loc[:240])
chk("扫盘结果按时间缓存（连点换一批不重复扫）",
    "max_age" in loc and "_LOCAL_NOTES" in loc, loc[:200])

# ── ② 后端路由：随机漫步 / 索引状态 / 单本条目都认本地那一路 ─────
rand = re.search(r'if path == "/api/notes_random":(.*?)if path ==', SERVER, re.S)
rand = rand.group(1) if rand else ""
chk("/api/notes_random 认 src=wx|local，默认两边并抽",
    'q.get("src"' in rand and "local_note_items()" in rand
    and 'load_notes_index().get("items")' in rand, rand[:240])
chk("/api/notes_random 回带 wx / local 两个条数",
    '"wx": len(wx)' in rand and '"local": len(lc)' in rand, rand[:240])

idx = re.search(r'if path == "/api/notes_index":(.*?)\n\n', SERVER, re.S)
idx = idx.group(1) if idx else ""
chk("/api/notes_index 多报 local_count（没建索引也能随机漫步）",
    "local_count" in idx and "local_note_items()" in idx, idx[:240])

ofer = re.search(r'if path == "/api/notes_of":(.*?)\n\n', SERVER, re.S)
ofer = ofer.group(1) if ofer else ""
chk("/api/notes_of 把本机的划线/笔记并进来（卡片回顾也看得到）",
    "local_note_items()" in ofer and "seen" in ofer, ofer[:240])
chk("/api/notes_of 合并后按时间倒序（两路混排）",
    "items.sort" in ofer, ofer[:240])

# ── ③ 前端：档位 + 计数 + 分色徽章 ──────────────────────────────
rr = fn_body("renderRandomView")
chk("随机漫步顶栏有「全部 / 微信 / 取书」三档",
    'id="rwSeg"' in rr and 'data-s="all"' in rr and 'data-s="wx"' in rr
    and 'data-s="local"' in rr, rr[:300])
chk("换一批按当前档位取数（src= 带上）",
    "'/api/notes_random?n=9&src='" in rr or "/api/notes_random?n=9&src=" in rr, rr[:300])
chk("空状态判据把 local_count 也算上（没建索引也能玩）",
    "(st.count || 0) + (st.local_count || 0)" in rr or "st.local_count" in rr, rr[:300])
chk("表头计数两类分开报（微信 N · 取书 M）",
    "`微信 ${wxN} · 取书 ${lcN}`" in rr, rr[:300])
chk("档位高亮跟着 rwSrc 走（切回来不停在旧档）",
    "b.dataset.s === rwSrc" in rr, rr[:300])

prw = fn_body("paintRandomWall")
chk("取书来源的卡徽章挂 .lc 分色",
    "x.local ? ' lc' : ''" in prw, prw[:240])

chk("样式表里有 .qcard .m .k.lc 这条分色（含暗色）",
    ".qcard .m .k.lc{" in SRC and 'html[data-theme="dark"] .qcard .m .k.lc' in SRC)

# 卡片回顾不用改：/api/notes_of 已经并了本地那一路
chk("卡片回顾仍走 notesOf（后端已并入本地条目，无需另写一路）",
    "notesOf(bookId)" in fn_body("renderReview"))

bad = [c for c in checks if not c[0]]
print("\n随机漫步本地笔记体检：%d 项通过，失败 %d 项" % (len(checks) - len(bad), len(bad)))
for _, n, x in bad:
    print("  ✗ " + n + ("  | " + x if x else ""))
sys.exit(1 if bad else 0)
