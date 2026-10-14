#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""归藏门禁：手改 / 另存过的账本带上 UTF-8 BOM 时，整份账不许被当成空账。

一句话：这些 JSON 账本原先都用 `open(..., encoding="utf-8")` 读。用户拿记事本
或别的编辑器「另存」、某些云同步客户端回写时，文件头会多出三个字节 EF BB BF；
`json.load` 碰到它当场抛 JSONDecodeError，而每个读取者都有「兜底成空账」那一手 ——
于是书架、订阅、书笔记、导图、画板、阅读计划一起变空。

真正要命的不是这一屏空白，而是**下一次保存会把空账写回去**：用户手改前的数据
就此没了。这不是崩溃，是静默清空，比崩溃难查得多。旧账本里 `_catalog.json` /
`meta.json` 这些由别的工具经手过的文件尤其容易中这一枪。

修法一处：读账本的 `open` 改用 `utf-8-sig`（开头三个字节是 BOM 就摘掉，其余与
utf-8 一字不差；写盘口径不变，不会给文件加 BOM）。这条门禁给每本账做一次
「干净 → 加 BOM → 再读」的往返：

  · 先证明夹具本身读得出（否则这条测例什么也没测 —— 不能靠「两边都是空」蒙过去）；
  · 夹具确实没带 BOM（加 BOM 那一步是我们自己做的）；
  · 带 BOM 读回来与干净那份**每个字段都相同**。

只碰临时目录，不起服务、不联网、不碰真实 cache/ 与书库。归静态段，--fast 也带。
"""
import json
import os
import pathlib
import sys
import tempfile

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))          # 被测模块在仓库根

ROOT = tempfile.mkdtemp(prefix="guizang-bom-")
os.environ["GUIZANG_DATA"] = os.path.join(ROOT, "cache")
os.environ["GUIZANG_BOOKS"] = os.path.join(ROOT, "books")
os.environ["GUIZANG_SHOT_DIR"] = os.path.join(ROOT, "shots")
os.environ["GUIZANG_ANNA_NO_WINDOW"] = "1"

import ui_server as us             # noqa: E402
import anna_state                  # noqa: E402
import book_import                 # noqa: E402
import book_layout                 # noqa: E402
import book_notes                  # noqa: E402
import board                       # noqa: E402
import feed                        # noqa: E402
import flomo_notes                 # noqa: E402
import login                       # noqa: E402
import mindmap                     # noqa: E402

BOM = b"\xef\xbb\xbf"
S = "BOM哨兵7f3"          # 每份夹具里都带它：读出来必须还认得出

FAIL = []
PASSED = 0


def chk(name, cond, extra=""):
    global PASSED
    print(("PASS  " if cond else "FAIL  ") + name + ("" if cond else "   " + repr(extra)[:220]))
    if cond:
        PASSED += 1
    else:
        FAIL.append(name)


def raw_write(path, obj, bom=False):
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    blob = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    with open(path, "wb") as f:
        f.write((BOM if bom else b"") + blob)


def roundtrip(name, path, read, ok, seed):
    """干净 → 加 BOM → 再读：要求三关都过。`seed` 负责在 `path` 造出一份合法账。"""
    seed()
    clean = read()
    chk(name + "：干净账读得出（夹具有效）", ok(clean), clean)
    raw = open(path, "rb").read()
    chk(name + "：夹具本身没带 BOM", not raw.startswith(BOM), raw[:6])
    with open(path, "wb") as f:
        f.write(BOM + raw)
    bom = read()
    chk(name + "：带 BOM 读回来与干净那份逐字段相同", bom == clean, (bom, clean))


# ── ui_server 直读的那几本账：路径挂到临时目录，绝不碰真实 cache/ ──────
CACHE = os.path.join(ROOT, "cache")
us.LIB_PATH = os.path.join(CACHE, "library.json")
us.CFG_PATH = os.path.join(CACHE, "config.json")
us.READSTAT_PATH = os.path.join(CACHE, "readstat.json")
us.ACTIVITY_PATH = os.path.join(CACHE, "activity.json")
us.PLAN_PATH = os.path.join(CACHE, "readplan.json")
us.NOTES_IDX = os.path.join(CACHE, "notes_index.json")

roundtrip("library.json（书架）", us.LIB_PATH, us.load_lib,
          lambda v: S in repr(v),
          lambda: raw_write(us.LIB_PATH, {
              "books": [{"id": "bk1", "title": "书" + S, "author": "a"}],
              "folders": [], "read": [], "fav": [], "recent": []}))

roundtrip("config.json（配置）", us.CFG_PATH, us.load_cfg,
          lambda v: v.get("export_dir") == "/tmp/" + S,
          lambda: raw_write(us.CFG_PATH, {"export_dir": "/tmp/" + S}))

roundtrip("readstat.json（本机阅读时长）", us.READSTAT_PATH, us.load_readstat,
          lambda v: v.get("total") == 42,
          lambda: raw_write(us.READSTAT_PATH, {"total": 42, "days": {"2026-10-15": 42}}))

roundtrip("activity.json（时长账）", us.ACTIVITY_PATH, us.load_activity,
          lambda v: (v.get("study") or {}).get("total") == 10,
          lambda: raw_write(us.ACTIVITY_PATH,
                            {"study": {"days": {"2026-10-15": 10}, "total": 10}}))

roundtrip("readplan.json（阅读计划）", us.PLAN_PATH, us.load_plan,
          lambda v: S in repr(v),
          lambda: raw_write(us.PLAN_PATH, {
              "version": 1, "plans": {"bk1": {"title": "计" + S, "current": 1, "total": 3}}}))

roundtrip("notes_index.json（划线索引）", us.NOTES_IDX, us.load_notes_index,
          lambda v: S in repr(v),
          lambda: raw_write(us.NOTES_IDX,
                            {"built_at": 1, "items": [{"book": "bk1", "text": S}]}))

# ── 各模块自己的账本 ─────────────────────────────────────────────────
BOOK_DIR = os.path.join(ROOT, "books", "bk1")

roundtrip("feed.json（订阅账）", feed.STORE, feed._load,
          lambda v: S in repr(v),
          lambda: raw_write(feed.STORE, {
              "feeds": [{"id": "f1", "url": "http://a/rss", "title": "订" + S}],
              "entries": {}, "seen": {}, "settings": {}}))

FLOMO = os.path.join(ROOT, "flomo")
roundtrip("flomo notes.json（便签账）", flomo_notes.path_in(FLOMO),
          lambda: flomo_notes.load(FLOMO),
          lambda v: S in repr(v),
          lambda: raw_write(flomo_notes.path_in(FLOMO),
                            {"memos": [{"id": "m1", "content": "便" + S, "md": "便" + S}]}))

roundtrip("flomo portrait.json（记忆画像）",
          os.path.join(FLOMO, flomo_notes.PORTRAIT_DIR, flomo_notes.PORTABLE_JSON),
          lambda: flomo_notes.read_portrait(FLOMO),
          lambda v: S in repr(v),
          lambda: raw_write(os.path.join(FLOMO, flomo_notes.PORTRAIT_DIR,
                                         flomo_notes.PORTABLE_JSON),
                            {"tags": [S], "total": 1}))

roundtrip("mindmap.json（思维导图）", mindmap.path_of(BOOK_DIR),
          lambda: mindmap.load_map(BOOK_DIR),
          lambda v: S in repr(v),
          lambda: raw_write(mindmap.path_of(BOOK_DIR),
                            {"title": "图" + S, "nodes": [{"id": "n1", "text": S}]}))

roundtrip("boards/*.json（画板）", board.board_path(BOOK_DIR, "b1"),
          lambda: board.load_board(BOOK_DIR, "b1"),
          lambda v: bool(v) and S in repr(v),
          lambda: board.save_board(BOOK_DIR, board.empty("b1", title="板" + S)))

roundtrip("output/*/meta.json（书目录）", os.path.join(BOOK_DIR, "meta.json"),
          lambda: book_layout.read_meta(BOOK_DIR),
          lambda v: S in repr(v),
          lambda: raw_write(os.path.join(BOOK_DIR, "meta.json"),
                            {"title": "目" + S, "author": "a"}))

LOGIN = os.path.join(ROOT, "login_state.json")
roundtrip("login_state.json（登录态）", LOGIN, lambda: login.read_state(LOGIN),
          lambda v: bool(v) and S in (v.get("url") or ""),
          lambda: login.write_state(True, "http://127.0.0.1/" + S, path=LOGIN))

ANNA = os.path.join(ROOT, "anna")
roundtrip("anna.json（安娜账本）", anna_state.state_path(ANNA),
          lambda: anna_state.read_state(ANNA),
          lambda v: S in (v.get("keyword") or ""),
          lambda: anna_state.write_state(ANNA, {"keyword": S, "caught": 3}))

roundtrip("anna_cmd.json（安娜口令条）", anna_state.cmd_path(ANNA),
          lambda: anna_state.read_cmd(ANNA),
          lambda v: S in repr(v),
          lambda: anna_state.write_cmd(ANNA, S))

roundtrip("notes.json（书笔记）", book_notes.notes_path(BOOK_DIR),
          lambda: book_notes.load_notes(BOOK_DIR),
          lambda v: S in repr(v),
          lambda: book_notes.save_notes(BOOK_DIR,
                                        {"marks": [{"id": "m1", "ch": "c1", "text": S}]}))

roundtrip("_catalog.json（章节题名）", os.path.join(BOOK_DIR, "_catalog.json"),
          lambda: book_notes.chapter_titles(BOOK_DIR),
          lambda v: S in repr(v),
          lambda: raw_write(os.path.join(BOOK_DIR, "_catalog.json"), ["章" + S]))

TPL = os.path.join(ROOT, "tpl-cache")
roundtrip("note_templates.json（笔记模板）", book_notes._custom_path(TPL),
          lambda: book_notes._custom_list(TPL),
          lambda v: S in repr(v),
          lambda: book_notes.save_template(TPL, {"name": S, "body": "正文"}))

# 书架卡片那句计数走 ui_server.notes_counts（它自己再读一次 notes.json）
NOTES_DIR = os.path.join(ROOT, "books", "counted")
os.makedirs(NOTES_DIR, exist_ok=True)
roundtrip("notes_counts（书架笔记计数）", book_notes.notes_path(NOTES_DIR),
          lambda: us.notes_counts(NOTES_DIR),
          lambda v: v.get("marks") == 1,
          lambda: book_notes.save_notes(NOTES_DIR,
                                        {"marks": [{"id": "m1", "ch": "c1", "text": S}]}))

# ── 「收成书」两处内部重读：已有书目录的 meta 带 BOM，旧字段不许被抹掉 ──
F2 = os.path.join(ROOT, "flomo-shelf")
raw_write(flomo_notes.path_in(F2),
          {"memos": [{"id": "m1", "md": "便" + S, "content": "便" + S, "tags": ["t"]}]})
FDIR = os.path.join(ROOT, "books", "flomo-bom")


def fake_import(out_root, name, blob, title=None, author=None,
                book_id_prefix=None, source=None):
    os.makedirs(FDIR, exist_ok=True)
    raw_write(os.path.join(FDIR, "meta.json"), {"title": "旧" + S}, bom=True)
    return {"id": "flomo-bom", "dir": FDIR, "title": title}


info = flomo_notes.to_shelf(F2, "m1", os.path.join(ROOT, "books"), import_fn=fake_import)
chk("收成书（flomo）：带 BOM 的旧 meta 不再被整份抹掉",
    S in repr(info.get("meta")), info.get("meta"))

FSTORE = os.path.join(ROOT, "feed-shelf.json")
feed.STORE = FSTORE
raw_write(FSTORE, {
    "feeds": [{"id": "f1", "url": "http://a/rss", "title": "订"}],
    "entries": {"f1": [{"id": "e1", "title": "条", "link": "http://a/1",
                        "content_md": "正" + S, "published": "2026-10-15"}]},
    "seen": {}, "settings": {}})
FDIR2 = os.path.join(ROOT, "books2", "feed-bom")


def fake_import2(out_root, name, blob, **kw):
    os.makedirs(FDIR2, exist_ok=True)
    raw_write(os.path.join(FDIR2, "meta.json"), {"title": "旧" + S}, bom=True)
    return {"id": "feed-bom", "dir": FDIR2, "title": kw.get("title")}


_orig_import = book_import.import_book
book_import.import_book = fake_import2
try:
    finfo = feed.to_shelf("e1", os.path.join(ROOT, "books2"))
finally:
    book_import.import_book = _orig_import

fmeta = {}
try:
    with open(os.path.join(finfo.get("dir", ""), "meta.json"), encoding="utf-8-sig") as f:
        fmeta = json.load(f)
except Exception as e:
    fmeta = {"_err": repr(e)}
chk("收成书（feed）：带 BOM 的旧 meta 不再被整份抹掉，且照打上 source",
    S in repr(fmeta) and fmeta.get("source") == "feed", fmeta)

print()
print("账本 BOM 体检：通过 %d 项，失败 %d 项" % (PASSED, len(FAIL)))
if FAIL:
    for f in FAIL:
        print("  ✗ " + f)
    sys.exit(1)
print("ok  手改 / 另存过的账本带 BOM 不再被当成空账（读 utf-8-sig，写盘口径不变）")
