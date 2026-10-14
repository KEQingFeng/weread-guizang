#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""归藏门禁：notes.json 里**字段级**的脏值不许把导出 / 导图带走。

`book_notes.load_notes` 原来只在「是不是字典、有没有 id」这一层筛脏值，字段本身
什么样就什么样往下游递。可 notes.json 就住在这本书的目录里，用户手改得动，旧版本
也落过别的形状 —— 于是一个坏字段不抛在读盘这一步，而是抛在下游：

  · 划线的 `created_at` 一半是数字、一半是「昨天」→ `export_markdown` 按
    `x.get("created_at") or 0` 排序，`int < str` 当场 TypeError，
    `/api/mynotes/export` 回一句「导出没做成」；
  · `ch` 给成数组 → `by_ch.setdefault(list)` 与 `note_tree(cut="chapter")` 的
    `titles.get(list)` 都抛 unhashable，导出与导图一起坏；
  · 顶层 `created_at: 1e400` 是合法 JSON（解析成 inf）→ `json.dumps` 原样印成
    `Infinity`，那不是合法 JSON，界面 `JSON.parse` 整份 /api/mynotes 直接失败；
  · `color` 给数字 → 写盘那条 `_hex` 的 `(v or "").strip()` 抛 AttributeError。

这一条把这几样一起喂进 `load_notes`，要求：读得开、条目一条不少、认得出的字段照认，
然后 `export_markdown` / `export_notes` / `note_tree` / `mindmap` 四条下游都不抛，
落盘的 notes.md 真的生成；另有一条对照，确认越界值确实不在 JSON 里留 `Infinity`。

全程离线：只调 book_notes 的纯函数 + 临时书目录，不 import 服务、不联网。
"""
import json
import os
import pathlib
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import book_notes as bn  # noqa: E402

FAIL = []
PASSED = 0


def chk(name, cond, extra=""):
    global PASSED
    print(("PASS  " if cond else "FAIL  ") + name + ("" if cond else "   " + str(extra)[:220]))
    if cond:
        PASSED += 1
    else:
        FAIL.append(name)


def write_raw(book_dir, doc):
    with open(os.path.join(book_dir, bn.NOTES_FILE), "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False)


def try_call(fn):
    """跑一下，返回 (ok, 异常字符串)。"""
    try:
        fn()
        return True, ""
    except Exception as e:                           # noqa: BLE001
        return False, "%s: %s" % (type(e).__name__, e)


DIRTY = {
    "created_at": 1e400,                             # 合法 JSON，解析成 inf
    "marks": [
        {"id": "m1", "text": "第一句", "ch": ["0001.md"],
         "created_at": "昨天", "tag": "point", "color": 5},
        {"id": "m2", "text": "第二句", "ch": "0001.md",
         "created_at": 1756864800, "tag": "doubt"},
        {"id": "m3", "text": "第三句", "ch": "0001.md",
         "created_at": {"x": 1}, "kind": {"bad": 1}},
        {"text": "没 id 的照旧在门口被丢掉"},          # 旧口径：无 id 不留
    ],
    "entries": [
        {"id": "n1", "title": "条目", "body": {"a": 1}, "updated_at": "oops",
         "refs": "不是列表"},
        {"id": "n2", "title": "条目2", "body": "正文", "updated_at": 1756864800,
         "refs": {"a": 1}},
    ],
}

# ── 1. 读盘：字段级脏值收口，一条不丢 ────────────────────────────
with tempfile.TemporaryDirectory(prefix="gz-notes-dirty-") as td:
    write_raw(td, DIRTY)
    try:
        doc = bn.load_notes(td)
        chk("R1 字段脏值不抛：load_notes 读得开", True)
    except Exception as e:                           # noqa: BLE001
        doc = {"marks": [], "entries": []}
        chk("R1 字段脏值不抛：load_notes 读得开", False, e)

    chk("R2 有 id 的划线一条不少（不因为一个字段坏掉丢整条）",
        [m.get("id") for m in doc["marks"]] == ["m1", "m2", "m3"], doc["marks"])
    chk("R3 有 id 的条目一条不少",
        [e.get("id") for e in doc["entries"]] == ["n1", "n2"], doc["entries"])
    chk("R4 认不出的时间戳回整数 0，不猜「现在」",
        [m.get("created_at") for m in doc["marks"]] == [0, 1756864800, 0]
        and doc["marks"][0].get("updated_at") == 0, [m.get("created_at") for m in doc["marks"]])
    chk("R5 非字符串的 ch 收成字符串（分组键要是可哈希的）",
        all(isinstance(m.get("ch"), str) for m in doc["marks"]), doc["marks"])
    chk("R6 认得出的标签 / 自己挑的合法颜色照旧",
        doc["marks"][0]["tag"] == "point" and doc["marks"][1]["tag"] == "doubt",
        [m.get("tag") for m in doc["marks"]])
    chk("R7 认不出的 kind 回默认 highlight",
        doc["marks"][2]["kind"] == "highlight", doc["marks"][2])
    chk("R8 顶层越界时间戳收成 0，json.dumps 不再产出 Infinity",
        doc.get("created_at") == 0 and doc.get("updated_at") == 0
        and "Infinity" not in json.dumps(doc, ensure_ascii=False),
        (doc.get("created_at"), doc.get("updated_at")))
    chk("R9 坏 refs（字符串 / 对象）当空表，条目本身照留",
        doc["entries"][0]["refs"] == [] and len(doc["entries"]) == 2, doc["entries"])

# ── 2. 下游四条路都不抛（修前 export_markdown 在排序上抛） ────────
with tempfile.TemporaryDirectory(prefix="gz-notes-down-") as td:
    write_raw(td, DIRTY)
    doc = bn.load_notes(td)
    ok, err = try_call(lambda: bn.export_markdown(doc))
    chk("D1 export_markdown 排序不再抛（int 与「昨天」混排）", ok, err)
    ok, err = try_call(lambda: bn.note_tree(doc, cut="chapter"))
    chk("D2 note_tree 按章节切（ch 是可哈希的字符串）不抛", ok, err)
    ok, err = try_call(lambda: bn.note_tree(doc, cut="tag"))
    chk("D3 note_tree 按标签切不抛", ok, err)
    ok, err = try_call(lambda: bn.mindmap(td, doc))
    chk("D4 mindmap 不抛", ok, err)
    ok, err = try_call(lambda: bn.export_notes(td, doc))
    chk("D5 export_notes 真把 notes.md 落下来", ok and os.path.isfile(
        os.path.join(td, bn.EXPORT_MD)), err)
    if os.path.isfile(os.path.join(td, bn.EXPORT_MD)):
        body = open(os.path.join(td, bn.EXPORT_MD), encoding="utf-8").read()
        chk("D6 三条划线都进了导出稿", all(t in body for t in
                                       ("第一句", "第二句", "第三句")), body[:300])

# ── 3. 好数据一个字不改（收口是格式整理，不是重写内容） ──────────
with tempfile.TemporaryDirectory(prefix="gz-notes-good-") as td:
    write_raw(td, {"marks": [{"id": "ok", "text": "原样", "ch": "0002.md",
                              "created_at": "1756864800", "tag": "quote",
                              "kind": "bold", "color": "#AABBCC", "memo": "备注"}],
                   "entries": [{"id": "n", "title": "题", "body": "正文",
                                "updated_at": 1756864800,
                                "refs": [{"text": "引用", "ch": "0002.md"}]}]})
    doc = bn.load_notes(td)
    m = doc["marks"][0]
    chk("G1 数字串时间戳认得出来；没给的那个不拿「现在」编（回 0）",
        m["created_at"] == 1756864800 and m["updated_at"] == 0, m)
    chk("G2 合法标签 / 类型 / 颜色 / 备注原样保留",
        m["tag"] == "quote" and m["kind"] == "bold" and m["color"] == "#AABBCC"
        and m["memo"] == "备注" and m["text"] == "原样", m)
    chk("G3 正常 refs 原样保留",
        doc["entries"][0]["refs"] == [{"text": "引用", "ch": "0002.md",
                                       "mark": "", "entry": ""}], doc["entries"][0])

# ── 4. 写盘那条同口径的边：color 给非字符串不该带崩整次保存 ──────
ok, err = try_call(lambda: bn._normalize({"marks": [{"text": "x", "color": 5}]}))
chk("W1 _normalize 的 color 给数字不抛", ok, err)
if ok:
    out = bn._normalize({"marks": [{"text": "x", "color": 5, "tag": "point"}]})
    chk("W2 认不出的颜色退到标签色，划线本身照存",
        out["marks"][0]["color"] == bn.TAG_BY_KEY["point"]["color"], out["marks"][0])
ok_h, err_h = try_call(lambda: (bn._hex(5), bn._hex(["#fff"]), bn._hex(None)))
chk("W3 _hex 对数字 / 数组 / None 都回空串，不抛",
    ok_h and bn._hex(5) == "" and bn._hex(["#fff"]) == "" and bn._hex(None) == "",
    err_h or "ok")

# ── 5. 存读回环：收口过的那一份再存一次仍然读得回来 ──────────────
with tempfile.TemporaryDirectory(prefix="gz-notes-loop-") as td:
    write_raw(td, DIRTY)
    ok, err = try_call(lambda: bn.save_notes(td, bn.load_notes(td)))
    chk("L0 脏档（含顶层越界数字）存得回去，不抛", ok, err)
    if ok:
        back = bn.load_notes(td)
        chk("L1 脏档过一遍存读回环：条目数不缩水、时间戳是整数",
            len(back["marks"]) == 3 and len(back["entries"]) == 2
            and all(isinstance(m["created_at"], int) for m in back["marks"]), back["marks"])
    else:
        chk("L1 脏档过一遍存读回环：条目数不缩水、时间戳是整数", False, err)

print()
print("笔记读盘脏字段收口：通过 %d 项，失败 %d 项" % (PASSED, len(FAIL)))
if FAIL:
    for f in FAIL:
        print("  ✗ " + f)
    sys.exit(1)
print("ok  字段级脏值读得开、四条下游不抛、好数据原样保留")
