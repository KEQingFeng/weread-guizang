#!/usr/bin/env python3
"""划线笔记：列表 → 书籍卡片瀑布流，并把取书划线并进来（1.2.1 · R4-6）。

用户要的是两件事：
  ① 展示方式从「一行一本」的列表换成书籍卡片式瀑布流；
  ② 不只微信读书的划线，「取书」（下载到本机）时自己划的线也要一起展示，
     并且支持同一套功能（多选 / 导入 flomo / 导出 CSV）。

这一套不依赖浏览器：前端读 ui.html 源码，后端直接调 book_notes 落一份笔记再读回来，
所以归静态段，`--fast` 也跑。
"""
import json
import pathlib
import re
import sys
import tempfile

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))
import book_notes  # noqa: E402

SRC = (ROOT / "ui.html").read_text(encoding="utf-8")
SERVER = (ROOT / "ui_server.py").read_text(encoding="utf-8")

checks = []


def chk(name, ok, extra=""):
    checks.append((bool(ok), name, "" if ok else str(extra)[:300]))
    print(("  ok  " if ok else " FAIL ") + name + ("" if ok else "  <- " + str(extra)[:300]))


def fn_body(name):
    """截出 `function <name>(...) { ... }` 的函数体（到行首的右花括号为止）。"""
    m = re.search(r"function\s+%s\s*\([^)]*\)\s*\{(.*?)\n\}" % re.escape(name), SRC, re.S)
    return m.group(1) if m else ""


# ── ① 瀑布流版式：列布局 + 不截断的卡片 ──────────────────────────
wall = re.search(r"\.nbwall\{([^}]*)\}", SRC)
chk("瀑布流容器 .nbwall 用多列排版（columns）",
    bool(wall) and "columns:" in wall.group(1), wall.group(1) if wall else "")
card = re.search(r"\.nbcard\{([^}]*)\}", SRC)
chk("书卡 .nbcard 不跨列拆开（break-inside:avoid）",
    bool(card) and "break-inside:avoid" in card.group(1), card.group(1) if card else "")
chk("窄屏降成两列 / 一列（1000px 与 560px 两档）",
    "@media (max-width:1000px){ .nbwall{columns:2} }" in SRC
    and "@media (max-width:560px){ .nbwall{columns:1} }" in SRC)
chk("卡片封面用 2/3 比例并兜底首字（不留破图）",
    ".nbcard .cv{" in SRC and "aspect-ratio:2/3" in SRC and ".nbcard .cv .ph{" in SRC)

# ── ② 一屏两面墙：微信读书 + 取书，同名并成一张卡 ────────────────
rn = fn_body("renderNotebooks")
chk("renderNotebooks 把本机书（local）里的笔记也并进来",
    "Object.values(local)" in rn and ".notes" in rn, rn[:240])
chk("同名书按书名归一化后合并，不重复出卡",
    "byTitle" in rn and "toLowerCase()" in rn, rn[:240])
chk("列表行 .wrrow 已从这一屏撤掉，改铺 .nbwall 卡墙",
    "wrrow" not in rn and "nbwall" in rn, rn[:240])
chk("没填微信读书 Key 时，只要有本机划线这一屏也能进",
    "hasLocal" in rn and "!keySet && !hasLocal" in rn, rn[:240])

nbc = fn_body("nbCard")
chk("nbCard 一卡出徽章：微信 / 取书两个来源分色标出",
    "微信 ${wn} 条" in nbc and "取书 ${ln} 条" in nbc, nbc[:240])
chk("nbCard 封面按「官方图 → 本地缓存」顺次退",
    "info.cover" in nbc and "/api/cover?book=" in nbc and "img.onerror" in nbc, nbc[:240])
chk("书卡条数把本地 marks + entries 算进去（nbCount）",
    "nbCount" in SRC and "n.marks" in fn_body("nbCount") and "n.entries" in fn_body("nbCount"),
    fn_body("nbCount"))

# ── ③ 详情页：微信 + 取书两路条目混排，功能同一套 ────────────────
on = fn_body("openNotebook")
chk("详情页请求本机笔记 /api/mynotes",
    "/api/mynotes?book=" in on, on[:240])
chk("微信那两路只在有 bookId 时才打（本机书不打空枪）",
    "b.bookId ? [" in on, on[:240])
chk("取书划线带各自 kind 的中文来源标签",
    "取书划线" in on and "KIND[m.kind]" in on, on[:240])
chk("两路条目按时间倒序混排",
    "nbItems.sort" in on, on[:240])
chk("全选 / 清空 / 导入 flomo / 导出 CSV 仍挂在同一份 nbItems 上",
    all(k in SRC for k in ('id="nbAll"', 'id="nbNone"', 'id="nbFlomo"', 'id="nbCsv"'))
    and "nbPicked()" in SRC)

# 前端 KIND 表必须覆盖后端全部 kind，不然某类划线会显示成默认名。
kind_map = re.search(r"const KIND = \{([^}]*)\};", on)
keys = set(re.findall(r"(\w+):", kind_map.group(1))) if kind_map else set()
chk("前端 KIND 表覆盖 book_notes.KINDS 的每一种",
    set(book_notes.KINDS) <= keys, "前端 %s / 后端 %s" % (sorted(keys), sorted(book_notes.KINDS)))

# ── ④ 后端：本机笔记真的数得出来（mark + memo、entry 各算一条）────
with tempfile.TemporaryDirectory() as td:
    doc = {"schema": 1, "marks": [
        {"id": "m1", "text": "第一段划线", "kind": "highlight", "memo": "这里有个想法",
         "created_at": 1700000000},
        {"id": "m2", "text": "第二段划线", "kind": "bold", "created_at": 1700000100},
    ], "entries": [
        {"id": "n1", "title": "一条笔记", "body": "正文", "created_at": 1700000200},
    ]}
    book_notes.save_notes(td, doc)
    back = book_notes.load_notes(td)
    cnt = book_notes.counts(back)
    chk("本机笔记落盘再读回：2 划线 + 1 笔记",
        cnt == {"marks": 2, "entries": 1}, cnt)
    chk("划线的 memo（那句想法）跟着一起存",
        any((m.get("memo") or "") == "这里有个想法" for m in back["marks"]))

chk("/api/mynotes 路由在后端存在（这一屏取书本划线就走它）",
    '"/api/mynotes"' in SERVER)
chk("/api/state 的每本书仍带本机笔记条数（前一屏据此并入瀑布流）",
    '"notes": notes_counts(d)' in SERVER)

bad = [c for c in checks if not c[0]]
print("\n划线笔记瀑布流体检：%d 项通过，失败 %d 项" % (len(checks) - len(bad), len(bad)))
for _, n, x in bad:
    print("  ✗ " + n + ("  | " + x if x else ""))
sys.exit(1 if bad else 0)
