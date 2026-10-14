#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""归藏门禁：划线索引账本（cache/notes_index.json）的脏条目收口 + 原子落盘。

一句话：**索引里混进一条脏数据，划线搜索 / 随机漫步 / 卡片回顾 / MCP 摘要都不许 500。**

这份账本不只是「建一次就不动」：整份重建与「单本现拉」的增量合并交替落盘，用户也
可能手工编辑，旧版本还落过别的形状。原先 `load_notes_index` 只验顶层 `items` 是不是
表就原样递出去，于是索引里只要有一条不是对象的脏条目：

  · /api/notes_search  —— `x.get("text")` 抛 AttributeError，划线搜索整条 500；
  · /api/notes_of 与 merge_notes_index —— 去重用的 `(bookId, text)` 元组算不出；
  · local_context（MCP / Agent 摘要）与 /api/notes_random —— `-(x.at)` 排不了序；
  · 导出 .apkg —— `x["text"]` 直接 KeyError。

这一条把坏形状逐样喂进 load，要求认得出的条目照留、字段收成下游能直接用的形状，
认不出的整条丢掉；再把「重建 / 合并」两条落盘路径走一遍，确认写出来的仍是好 JSON、
不留半截临时件，且写失败时旧索引一个字都不动。最后把「单本现拉」这一路也钉住：
网关把 createTime 写成字符串时，/api/notes_of 的 `-(x.at)` 排序不许把详情页打成 500。
全程临时沙盒，不起服务、不联网（现拉那一路用假 weread_call 顶掉）。
"""
import json
import os
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import selftest  # noqa: E402

sys.path.insert(0, str(selftest.REPO))    # 进程内 import ui_server 验后端
selftest.export_sandbox_env()             # 必须在 import ui_server 之前设好环境变量

import ui_server as U  # noqa: E402

FAIL = []
PASSED = 0


def chk(name, cond, extra=""):
    global PASSED
    print(("PASS  " if cond else "FAIL  ") + name + ("" if cond else "   " + str(extra)[:220]))
    if cond:
        PASSED += 1
    else:
        FAIL.append(name)


IDX = U.NOTES_IDX
CACHE = os.path.dirname(IDX)
os.makedirs(CACHE, exist_ok=True)


def write_raw(text):
    with open(IDX, "w", encoding="utf-8") as f:
        f.write(text)


def write_obj(obj):
    with open(IDX, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False)


def read_obj():
    with open(IDX, encoding="utf-8") as f:
        return json.load(f)


def leftovers():
    """只认这份索引名下的半截临时件，别的套件留在沙盒里的 .tmp 不算它头上。"""
    return [n for n in os.listdir(CACHE)
            if n.startswith("notes_index.json.") and n.endswith(".tmp")]


def try_load():
    """调 load 并把异常带出来，测试里要看到「抛没抛」而不是被吞掉。"""
    try:
        return U.load_notes_index(), None
    except Exception as e:
        return None, e


# ── A：整份坏档（形状不对 / 半截）仍当空账，绝不抛 ─────────────────
if os.path.exists(IDX):
    os.remove(IDX)
d, err = try_load()
chk("A1 文件不存在：回空账且不抛", err is None and d == {"built_at": 0, "items": [], "books": []},
    (d, err))

write_obj([1, 2, 3])
d, err = try_load()
chk("A2 顶层是数组：回空账且不抛", err is None and d["items"] == [] and d["books"] == [], (d, err))

write_obj({"built_at": 1, "items": "不是表"})
d, err = try_load()
chk("A3 items 不是表：回空账且不抛", err is None and d["items"] == [], (d, err))

write_raw('{"built_at": 1, "items": [{"text": "半截')
d, err = try_load()
chk("A4 半截 JSON：回空账且不抛", err is None and d["items"] == [], (d, err))

# ── B：表里混脏条目：认得出的照留、字段收口、认不出的丢掉 ────────────
write_obj({"built_at": "oops", "items": [
    "裸字符串",
    None,
    5,
    {"bookId": "b1", "title": 7, "author": None, "kind": 0,
     "text": "第一条好划线", "at": "1750000000"},
    {"bookId": "b2", "text": "时间戳认不出", "at": "昨天"},
    {"bookId": "b3", "text": "浮点秒", "at": 1750000000.9},
    {"bookId": "b4"},                       # 缺正文：仍是对象，条目不许丢
], "books": {"bookId": "b1"}})

d, err = try_load()
items_ok = err is None and d is not None and len(d["items"]) == 4
chk("B1 逐条洗：非对象整条丢、对象条目一条不丢", items_ok, (d, err))
d = d or {"built_at": 0, "items": [], "books": []}   # 真抛了也走到最后，把失败报全
chk("B2 一条都不是脏对象（下游拿到的全是 dict）",
    err is None and all(isinstance(x, dict) for x in d["items"]), d)
chk("B3 字段都收成字符串（数字 7 → \"7\"、None → \"\"、假的 kind 回默认「划线」）",
    d["items"][0]["title"] == "7" and d["items"][0]["author"] == ""
    and d["items"][0]["kind"] == "划线", d["items"][0])
chk("B4 数字串时间戳认得出（1750000000 不是 0）",
    d["items"][0]["at"] == 1750000000, d["items"][0]["at"])
chk("B5 认不出的时间戳回 0（不当负数、不抛）", d["items"][1]["at"] == 0, d["items"][1]["at"])
chk("B6 浮点秒收成整数（不截断成 0）", d["items"][2]["at"] == 1750000000, d["items"][2]["at"])
chk("B7 缺正文的条目仍在（条目不因一个字段坏掉就整条消失）",
    d["items"][3].get("text") == "", d["items"][3])
chk("B8 books 被写成对象时回一张空表", d["books"] == [], d["books"])
chk("B9 built_at 脏字符串回 0（不是 \"oops\"）", d["built_at"] == 0, d["built_at"])

# 下游四种真实用法：同一份脏索引，一个都不许抛。
low = "划线"
try:
    hit = [x for x in d["items"]
           if low in (x.get("text") or "").lower()
           or low in (x.get("title") or "").lower()]
    search_ok = len(hit) == 1
except Exception as e:
    hit, search_ok = e, False
chk("B10 /api/notes_search 的筛选不再抛且命中好条目", search_ok, hit)

try:
    order = sorted(d["items"], key=lambda x: -(x.get("at") or 0))
    sort_ok = order[0]["at"] == 1750000000
except Exception as e:
    order, sort_ok = e, False
chk("B11 /api/notes_random 与 MCP 摘要的排序键不再抛", sort_ok, order)

try:
    seen = {(x.get("bookId"), x.get("text")) for x in d["items"]}
    dedup_ok = ("b1", "第一条好划线") in seen
except Exception as e:
    seen, dedup_ok = e, False
chk("B12 /api/notes_of / merge 的去重元组算得出", dedup_ok, seen)

try:
    apkg_ok = all(x["text"].strip() is not None for x in d["items"])
except Exception as e:
    apkg_ok = e
chk("B13 导出 .apkg 的 x[\"text\"] 一定取得到（不再 KeyError）", apkg_ok is True, apkg_ok)

# 顶层形状没坏、但 items 全是脏的：回空表而不是回脏东西
write_obj({"built_at": 12, "items": ["a", None, 3], "books": ["书名", {"bookId": "b1", "title": "好"}]})
d, err = try_load()
chk("B14 脏表洗空后 items=[] 但不误判成整份坏档（built_at 认得出）",
    err is None and d["items"] == [] and d["built_at"] == 12, (d, err))
chk("B15 books 里只留对象条目", d["books"] == [{"bookId": "b1", "title": "好"}], d["books"])

# ── C：合并落盘：脏账上增量合并不许抛，写出来的是好 JSON、不留临时件 ────
write_obj({"built_at": "oops", "items": ["裸字符串", {"bookId": "b1", "text": "旧", "at": "x"}],
           "books": {"bad": 1}})
try:
    U.merge_notes_index([{"bookId": "b9", "title": "新书", "author": "谁", "kind": "划线",
                          "text": "新来的", "at": 1750000001}])
    merge_err = None
except Exception as e:
    merge_err = e
after = read_obj() if os.path.exists(IDX) else {}
chk("C1 脏账上做增量合并不抛", merge_err is None, merge_err)
chk("C2 合并后文件仍是好 JSON 且新条目在", len(after.get("items") or []) == 2, after)
chk("C3 合并后 built_at 补成真时间（脏 0 会被替换）",
    isinstance(after.get("built_at"), (int, float)) and after["built_at"] > 0, after.get("built_at"))
chk("C4 books 被收成表并带上新书", any(b.get("bookId") == "b9" for b in after.get("books") or []),
    after.get("books"))
chk("C5 合并落盘不留半截 .tmp", leftovers() == [], leftovers())

# ── D：整份重建的落盘与失败路径（stub 掉网关，不联网） ───────────────
orig_weread = U.weread_call
orig_write = U._write_json
U.NOTES_STATE["running"] = False

built, built_ok = {}, False
try:
    U.weread_call = lambda *a, **k: {}          # 网关回空：一趟也不请求真网络
    U.build_notes_index()
    built = read_obj()
    built_ok = (isinstance(built.get("items"), list) and built["items"] == []
                and isinstance(built.get("built_at"), (int, float)) and built["built_at"] > 0
                and built.get("books") == [])
finally:
    U.weread_call = orig_weread
chk("D1 重建走原子写：写出好 JSON（items/books/built_at 齐全）", built_ok, built)
chk("D2 重建成功不留 .tmp", leftovers() == [], leftovers())
chk("D3 重建结束后 running 复位", U.NOTES_STATE["running"] is False, U.NOTES_STATE)

# 已有一份好索引，重建在「落盘那一步」失败：旧索引一个字都不许动。
write_obj({"built_at": 1750000000, "items": [{"bookId": "keep", "title": "旧书", "author": "",
                                              "kind": "划线", "text": "要保住", "at": 1}],
           "books": [{"bookId": "keep", "title": "旧书"}]})
with open(IDX, "rb") as f:
    before_bytes = f.read()
U.NOTES_STATE["running"] = False


def boom(*a, **k):
    raise OSError("盘满")


try:
    U.weread_call = lambda *a, **k: {}
    U._write_json = boom
    U.build_notes_index()                        # 内部把异常吞成一句日志
    fail_err = None
except Exception as e:
    fail_err = e
finally:
    U.weread_call = orig_weread
    U._write_json = orig_write
with open(IDX, "rb") as f:
    after_bytes = f.read()
chk("D4 落盘失败不抛穿（重建自己收住）", fail_err is None, fail_err)
chk("D5 落盘失败时旧索引字节不动（不拿半截覆盖辛苦建好的划线）",
    after_bytes == before_bytes, (len(before_bytes), len(after_bytes)))
chk("D6 失败后 running 复位（不会卡在「正在建立」）",
    U.NOTES_STATE["running"] is False, U.NOTES_STATE)

# ── E：现拉这一路（网关把 createTime 写成字符串）也不许把排序打崩 ────
def fake_weread(api, params=None, timeout=None):
    if api == "/book/bookmarklist":
        return {"updated": [{"markText": "现拉划线", "createTime": "1750000000"}]}
    if api == "/review/list/mine":
        return {"reviews": [{"review": {"content": "现拉想法", "createTime": "oops"}}]}
    return {}


U.weread_call = fake_weread
live, live_ok, sort_live = [], False, False
try:
    live = U.fetch_book_notes("b1", "书", "谁")
    live_ok = (len(live) == 2 and all(isinstance(x["at"], int) for x in live)
               and [x["at"] for x in live] == [1750000000, 0])
    try:
        live.sort(key=lambda x: -(x.get("at") or 0))     # /api/notes_of 那一行
        sort_live = True
    except Exception:
        sort_live = False
finally:
    U.weread_call = orig_weread
chk("E0a 现拉划线的 at 收成整数（数字串认得出、脏值回 0）", live_ok, live)
chk("E0b 现拉条目能被 /api/notes_of 的排序键排出来", sort_live, live)

# ── F：源码走查：索引的每一处落盘都走同一把原子写 ────────────────────
src = (selftest.REPO / "ui_server.py").read_text(encoding="utf-8")
chk("F1 不再有裸 open(NOTES_IDX, \"w\") 直接覆写",
    'open(NOTES_IDX, "w"' not in src, "仍存在直接覆写")
chk("F2 重建与合并两处都走 _write_json(NOTES_IDX, …)",
    src.count("_write_json(NOTES_IDX,") >= 2, src.count("_write_json(NOTES_IDX,"))

if os.path.exists(IDX):
    os.remove(IDX)
print()
print("笔记索引脏账本收口：通过 %d 项，失败 %d 项" % (PASSED, len(FAIL)))
if FAIL:
    for f in FAIL:
        print("  ✗ " + f)
    sys.exit(1)
print("ok  脏条目不再让划线搜索翻车，索引落盘走原子写")
