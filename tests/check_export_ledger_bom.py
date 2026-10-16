#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""取书那几本账的读口认 BOM：`_catalog.json` / `_progress.json` / `_stuck.json` / `raw/*.json`。

为什么要单开这一条
    这四份 JSON 都住在书目录里，用户会拿记事本直接改 —— `_catalog.json` 的
    docstring 自己写着「用户会手工编辑」。Windows 记事本另存 UTF-8 默认带三字节
    BOM，而它们的读口原先写的是裸 `utf-8`：

      · `_catalog.json` 一解不动，`_catalog_items` 就回空表 —— 续传锚点
        （`load_last_catalog_title` / `first_catalog_title` / `resume_catalog_index`）
        全丢，引擎要么从第一页重来、要么钉不到该接着读的那一节；界面上
        「取回多少」的分母同时变成 0。
      · `_progress.json` 一解不动，`write_progress` 以为没有旧读数，把 pages
        盖回 0（进度条倒退），`run_session` 的 `page_base` 也跟着归零。
      · `_stuck.json` / `raw/*.json` 一解不动，「钉不住」的记录与字数清点整份
        当没有。

    与 feed / 便签账 / 视频三份登记档 / 写作册账同一把尺子：读口一律
    `utf-8-sig` —— BOM 只该被当成编码签名剥掉，不该被当成坏档。

这一条钉住
  A. 对照：裸 `utf-8` 读带 BOM 的同一份 JSON 确实抛 JSONDecodeError（洞是真的）；
  B. 引擎的目录四个读口、`load_stuck`、`_seen_image_urls` 在带 BOM 的账上读出与
     干净账一模一样的内容；
  C. `write_progress` 读回带 BOM 的旧读数（pages / chapters 不丢）；
  D. 界面 `list_books` 的 total / chars / progress.pages 在带 BOM 的账上不再归零；
  E. 写口不主动种 BOM（`_dump_json` / `write_progress` 落盘的第一字节不是 EF BB BF）；
  F. 源码接线：引擎与界面这几处读口都写着 `utf-8-sig`。

沙盒全在系统临时目录里，不联网、不起服务、不碰真实书库，故归 --fast 静态段。
要 import export_precise（引擎顶层 import playwright），没装 playwright 的机器上
这一条起不来 —— 那与 `check_export_catalog.py` / `check_export_encoding.py` 同档，
不静默 SKIP。
"""
import codecs
import inspect
import io
import json
import os
import shutil
import sys
import tempfile

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT_DIR)

SB = tempfile.mkdtemp(prefix="guizang-ledger-bom-")
os.environ["GUIZANG_DATA"] = os.path.join(SB, "cache")
os.environ["GUIZANG_BOOKS"] = os.path.join(SB, "books")
os.makedirs(os.environ["GUIZANG_DATA"], exist_ok=True)
os.makedirs(os.environ["GUIZANG_BOOKS"], exist_ok=True)

import export_precise as E   # noqa: E402
import ui_server             # noqa: E402

BOM = codecs.BOM_UTF8
FAIL = []
N = 0


def chk(name, cond, extra=""):
    global N
    N += 1
    print(("  ok  " if cond else " FAIL ") + name + ("" if cond else "  <- " + repr(extra)))
    if not cond:
        FAIL.append(name)


def write_bytes(path, data):
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    with open(path, "wb") as f:
        f.write(data)
    return path


def put(path, obj, bom=True):
    """按 JSON 落盘；bom=True 时前面挂三字节 BOM（模拟记事本另存）。"""
    raw = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    return write_bytes(path, (BOM if bom else b"") + raw)


def read_json(path):
    with io.open(path, encoding="utf-8-sig") as f:
        return json.load(f)


# ── A. 对照：这个洞是真的 ─────────────────────────────────────────
plain = put(os.path.join(SB, "plain.json"), ["甲", "乙"], bom=True)
raised = ""
try:
    with io.open(plain, encoding="utf-8") as f:
        json.load(f)
except Exception as e:
    raised = type(e).__name__
chk("对照：裸 utf-8 读带 BOM 的 JSON 确实解不动（不是理论上的洞）",
    raised == "JSONDecodeError", raised)

# ── B. 引擎：目录 / 钉不住 / raw 三本账 ───────────────────────────
DIR = os.path.join(SB, "book")
CATALOG = ["封面", "第一章 归藏", "第二章 收尾"]
bom_cat = put(os.path.join(DIR, "_catalog.json"), CATALOG, bom=True)
clean_cat = put(os.path.join(DIR, "_catalog.clean.json"), CATALOG, bom=False)

chk("引擎：_catalog_items 在带 BOM 的目录上读出原表",
    E._catalog_items(bom_cat) == CATALOG, E._catalog_items(bom_cat))
chk("引擎：带 BOM 的目录与干净目录逐字相同（不是只「不抛」）",
    E.load_catalog_titles(bom_cat) == E.load_catalog_titles(clean_cat)
    and E.load_catalog_ordered(bom_cat) == E.load_catalog_ordered(clean_cat)
    and E.load_last_catalog_title(bom_cat) == E.load_last_catalog_title(clean_cat)
    and E.first_catalog_title(bom_cat) == E.first_catalog_title(clean_cat))
chk("引擎：续传锚点的两个读口都读得出内容（空表就整本重来）",
    E.load_last_catalog_title(bom_cat) == CATALOG[-1]
    and E.first_catalog_title(bom_cat) == CATALOG[0])
md_dir = os.path.join(DIR, "chapters")
os.makedirs(md_dir, exist_ok=True)
chk("引擎：resume_catalog_index 带 BOM 与干净目录同一落点",
    E.resume_catalog_index(bom_cat, md_dir) == E.resume_catalog_index(clean_cat, md_dir)
    and E.resume_catalog_index(bom_cat, md_dir) == 0)

STUCK = {"catalog": 3, "strikes": {"1": 2, "x": 1}}
bom_stuck = put(os.path.join(DIR, "_stuck.json"), STUCK, bom=True)
clean_stuck = put(os.path.join(DIR, "_stuck.clean.json"), STUCK, bom=False)
chk("引擎：load_stuck 在带 BOM 的记录上读出 {1: 2}",
    E.load_stuck(bom_stuck, 3) == {1: 2}, E.load_stuck(bom_stuck, 3))
chk("引擎：load_stuck 带 BOM 与干净档同一结果",
    E.load_stuck(bom_stuck, 3) == E.load_stuck(clean_stuck, 3))

IMG = {"images": [{"url": "https://img.example.com/a.jpg"}]}
raw_bom = os.path.join(SB, "raw_bom")
raw_clean = os.path.join(SB, "raw_clean")
put(os.path.join(raw_bom, "0001.json"), IMG, bom=True)
put(os.path.join(raw_clean, "0001.json"), IMG, bom=False)
chk("引擎：raw/*.json 带 BOM 也清点出图片地址",
    E._seen_image_urls(raw_bom) == {"https://img.example.com/a.jpg"},
    E._seen_image_urls(raw_bom))
chk("引擎：raw 带 BOM 与干净档同一结果",
    E._seen_image_urls(raw_bom) == E._seen_image_urls(raw_clean))

# ── C. write_progress：旧读数不许被 BOM 冲掉 ──────────────────────
pb = os.path.join(SB, "prog_book")
os.makedirs(pb, exist_ok=True)
put(os.path.join(pb, "_progress.json"), {"pages": 42, "chapters": 3}, bom=True)
E.write_progress(pb, running=True, session_chars=10)
after = read_json(os.path.join(pb, "_progress.json"))
chk("引擎：write_progress 读回带 BOM 的旧读数（pages / chapters 不丢）",
    after.get("pages") == 42 and after.get("chapters") == 3, after)
chk("引擎：write_progress 落盘不主动种 BOM",
    open(os.path.join(pb, "_progress.json"), "rb").read(3) != BOM)

dp = os.path.join(SB, "dump.json")
E._dump_json(dp, {"甲": 1})
chk("引擎：_dump_json 落盘第一字节不是 BOM", open(dp, "rb").read(3) != BOM)

# ── D. 界面：list_books 的 total / chars / progress ──────────────
BOOKS = ui_server.OUT_DIR
MOD = "weread"
BID = "abcdef123456"
bd = os.path.join(BOOKS, ui_server.book_layout.DIR_OF[MOD], BID)
os.makedirs(os.path.join(bd, "chapters"), exist_ok=True)
put(os.path.join(bd, "meta.json"),
    {"title": "带 BOM 的书", "author": "作者甲", "source": "weread", "done": True}, bom=True)
with io.open(os.path.join(bd, "chapters", "0001.md"), "w", encoding="utf-8") as f:
    f.write("# 第一章 归藏\n\n正文\n")
put(os.path.join(bd, "raw", "0001.json"), {"text_len": 123}, bom=True)
put(os.path.join(bd, "_catalog.json"), ["第一章 归藏", "第二章 收尾"], bom=True)
put(os.path.join(bd, "_progress.json"), {"pages": 7, "running": True}, bom=True)

hit = [b for b in ui_server.list_books(module=MOD) if b["id"] == BID]
chk("界面：找到了这本账本带 BOM 的书", len(hit) == 1, hit)
if hit:
    b = hit[0]
    chk("界面：total 按带 BOM 的目录算（2），不是 0", b["total"] == 2, b["total"])
    chk("界面：chars 按带 BOM 的 raw 算（123），不是 0", b["chars"] == 123, b["chars"])
    chk("界面：progress.pages 按带 BOM 的进度算（7）",
        (b["progress"] or {}).get("pages") == 7, b["progress"])
    chk("界面：书名从带 BOM 的 meta.json 读出来（同一把尺子）",
        b["title"] == "带 BOM 的书", b["title"])

# ── F. 源码接线：读口写的是 utf-8-sig，不是一个碰巧能过的读法 ─────
def src(fn):
    return inspect.getsource(fn)


chk("源码：_catalog_items 的读口是 utf-8-sig", 'encoding="utf-8-sig"' in src(E._catalog_items))
chk("源码：load_stuck 的读口是 utf-8-sig", 'encoding="utf-8-sig"' in src(E.load_stuck))
chk("源码：write_progress 读进度用 utf-8-sig", 'encoding="utf-8-sig"' in src(E.write_progress))
chk("源码：_seen_image_urls 读 raw 用 utf-8-sig", 'encoding="utf-8-sig"' in src(E._seen_image_urls))
chk("源码：run_session 的 page_base 读进度用 utf-8-sig",
    'encoding="utf-8-sig"' in src(E.run_session))
chk("源码：list_books 三处取书账本读口都是 utf-8-sig",
    src(ui_server.list_books).count('encoding="utf-8-sig"') >= 3,
    src(ui_server.list_books).count('encoding="utf-8-sig"'))
chk("源码：book_outline 的目录读口是 utf-8-sig",
    'encoding="utf-8-sig"' in src(ui_server.book_outline))

shutil.rmtree(SB, ignore_errors=True)
print()
print("取书账本读口认 BOM：%d 项通过，失败 %d 项" % (N - len(FAIL), len(FAIL)))
if FAIL:
    print("未过：" + " / ".join(FAIL))
sys.exit(1 if FAIL else 0)
