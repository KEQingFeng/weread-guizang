#!/usr/bin/env python3
"""书本地笔记的「存盘口」离线自测：坏档读得开、坏字段丢不掉整份、模板满不假报成功。

`book_notes` 是归藏自己那一套划线 / 批注 / 条目（不是微信读书只读的划线），数据就
住在这本书目录里的 notes.json。它有两处「一个坏值就是一次全损」的地方，都不报错，
只是界面上一句话把用户挡回去：

  · `load_notes` 只挡了「JSON 不是对象」，没挡 `"marks": null` 这类**键在值不在**的
    半截档 —— `setdefault` 修不了 None，`for x in doc["marks"]` 当场 TypeError，
    这本书的笔记从此一打开就 500。
  · `_normalize` 里 `int(m.get("created_at"))`：手改过、或外部 Agent 写进来的
    `"1728-10-14"` / `"昨天"` 会抛 ValueError，被 /api/mynotes 兜成「存不下」——
    用户这一轮改的**其它**笔记跟着一起没落盘。

另外模板库满了（200 条）时，旧代码 `items[:200]` 切的是最旧的 200 条，刚追加的
第 201 条正好被切在门外，可接口回的还是「模板存好了」加它自己的 id —— 磁盘上却
没有。这一条也一并钉住。

全程离线：只调 book_notes 的纯函数 + 临时目录，不 import 服务、不联网。
"""
import json
import os
import pathlib
import sys
import tempfile
import time

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


# ── 1. 读档：半截档不许把这本书的笔记变成 500 ───────────────────
for key in ("marks", "entries"):
    with tempfile.TemporaryDirectory(prefix="gz-bn-null-") as td:
        write_raw(td, {"marks": [], "entries": [], key: None})
        try:
            out = bn.load_notes(td)
            ok = out["marks"] == [] and out["entries"] == []
        except Exception as e:                       # noqa: BLE001
            ok, out = False, "%s: %s" % (type(e).__name__, e)
        chk("L1 %s 是 null 时读得开、当空表" % key, ok, out)

with tempfile.TemporaryDirectory(prefix="gz-bn-shape-") as td:
    write_raw(td, {"marks": {"a": 1}, "entries": "不是列表"})
    try:
        out = bn.load_notes(td)
        chk("L2 marks 给对象 / entries 给字符串时读得开、当空表",
            out["marks"] == [] and out["entries"] == [], out)
    except Exception as e:                           # noqa: BLE001
        chk("L2 marks 给对象 / entries 给字符串时读得开、当空表", False, e)

with tempfile.TemporaryDirectory(prefix="gz-bn-keep-") as td:
    write_raw(td, {"marks": [{"id": "m1", "text": "留着", "created_at": 1756864800},
                             {"text": "没 id 的丢掉"}, "不是字典"],
                   "entries": [{"id": "n1", "title": "条目"}]})
    out = bn.load_notes(td)
    chk("L3 正常条目照读：有 id 的留、没 id 的与非字典丢掉",
        [m["id"] for m in out["marks"]] == ["m1"] and [e["id"] for e in out["entries"]] == ["n1"],
        out)

with tempfile.TemporaryDirectory(prefix="gz-bn-bad-") as td:
    with open(os.path.join(td, bn.NOTES_FILE), "w", encoding="utf-8") as f:
        f.write("{坏掉的 json")
    out = bn.load_notes(td)
    bad = [f for f in os.listdir(td) if f.startswith(bn.NOTES_FILE + ".bad-")]
    chk("L4 连 JSON 都坏了：回空结构，并把坏档先备份走（不直接覆盖）",
        out["marks"] == [] and out["entries"] == [] and len(bad) == 1, os.listdir(td))

# ── 2. 存盘：坏时间戳不许把整份笔记带走 ─────────────────────────
before = int(time.time())
for bad in ("1728-10-14", "昨天", "不是时间", {"x": 1}, [1, 2]):
    try:
        out = bn._normalize({"marks": [{"text": "一句划线", "created_at": bad,
                                        "updated_at": bad}]})
        got = out["marks"][0]["created_at"] if out["marks"] else None
        chk("T1 created_at=%r 不抛、当当前时间补上" % (bad,),
            len(out["marks"]) == 1 and isinstance(got, int) and before <= got <= int(time.time()) + 1,
            got)
    except Exception as e:                           # noqa: BLE001
        chk("T1 created_at=%r 不抛、当当前时间补上" % (bad,), False, "%s: %s" % (type(e).__name__, e))

out = bn._normalize({"marks": [{"text": "x", "created_at": "1756864800",
                                "updated_at": "1756864800.9"}]})
chk("T2 数字串与小数都认得（旧档接得回来）",
    out["marks"][0]["created_at"] == 1756864800 and out["marks"][0]["updated_at"] == 1756864800,
    out["marks"][0])

out = bn._normalize({"marks": [{"text": "x"}]})
now = int(time.time())
chk("T3 完全没给时间戳：补当前时间，且同一次保存里两条一致",
    abs(out["marks"][0]["created_at"] - now) <= 2
    and out["marks"][0]["created_at"] == out["marks"][0]["updated_at"], out["marks"][0])

out = bn._normalize({"marks": [{"text": "x"}], "entries": [{"title": "t"}]})
chk("T4 条目的坏时间戳同样兜住（排序不会拿 None 去比）",
    isinstance(out["entries"][0]["updated_at"], int), out["entries"][0])

chk("T5 _ts 认不出回空串、认得出照排",
    bn._ts("昨天") == "" and bn._ts(None) == ""
    and bn._ts("1756864800").startswith("2025-"), bn._ts("1756864800"))

# ── 3. 存盘：坏形状当空，好条目一件不丢 ─────────────────────────
for doc in ({"marks": {"a": 1}}, {"entries": {"a": 1}}, {"marks": "abc"}, {"entries": None}):
    try:
        out = bn._normalize(doc)
        chk("S1 %r 形状不对当空、不抛" % (doc,), out["marks"] == [] and out["entries"] == [], out)
    except Exception as e:                           # noqa: BLE001
        chk("S1 %r 形状不对当空、不抛" % (doc,), False, "%s: %s" % (type(e).__name__, e))

out = bn._normalize({"marks": [{"text": "好划线"}, {"text": "另一条"}],
                     "entries": [{"title": "好条目", "refs": {"a": 1}}]})
chk("S2 refs 给成对象时当空，条目本身照留",
    len(out["entries"]) == 1 and out["entries"][0]["refs"] == [], out["entries"])
chk("S3 同一份里的好划线一条不少", len(out["marks"]) == 2, out["marks"])
chk("S4 没正文的划线（空 text）照旧不占位",
    bn._normalize({"marks": [{"text": "  "}, {"text": "有效"}]})["marks"][0]["text"] == "有效")

# ── 4. 模板库：存得进、更得动、满了顶最旧而不是切最新 ────────────
with tempfile.TemporaryDirectory(prefix="gz-bn-tpl-") as cache:
    chk("P1 官方模板照旧在、不可删",
        bn.delete_template(cache, "official-quote")["ok"] is False
        and any(t["id"] == "official-quote" for t in bn.templates(cache)))

    r1 = bn.save_template(cache, {"name": "我的模板", "desc": "d", "body": "b"})
    chk("P2 新增成功且磁盘上找得到",
        r1["ok"] and bn.get_template(cache, r1["id"])["name"] == "我的模板", r1)

    r2 = bn.save_template(cache, {"id": r1["id"], "name": "改过名", "body": "b2"})
    chk("P3 带同一个 id 再存是原地更新，条目数不涨",
        r2["ok"] and r2["id"] == r1["id"]
        and bn.get_template(cache, r1["id"])["name"] == "改过名"
        and len(bn._custom_list(cache)) == 1, bn._custom_list(cache))

    chk("P4 自己编 official- 开头的 id 会被换成 user_ 开头，盖不掉官方模板",
        bn.save_template(cache, {"id": "official-quote", "name": "冒名"})["id"].startswith("user_")
        and bn.get_template(cache, "official-quote")["name"] == "划线卡片")

    chk("P5 坏档（自定义模板文件是坏 JSON）读成空表而不是抛",
        (lambda d: (os.makedirs(d, exist_ok=True),
                    open(os.path.join(d, bn.TPL_FILE), "w", encoding="utf-8").write("{坏"),
                    bn._custom_list(d) == []))(os.path.join(cache, "sub")))

with tempfile.TemporaryDirectory(prefix="gz-bn-cap-") as cache:
    for i in range(bn.MAX_CUSTOM_TEMPLATES):
        bn.save_template(cache, {"name": "t%03d" % i, "body": "b"})
    chk("P6 存满后自定义模板数就是上限值", len(bn._custom_list(cache)) == bn.MAX_CUSTOM_TEMPLATES,
        len(bn._custom_list(cache)))

    r = bn.save_template(cache, {"name": "最新这条", "body": "b"})
    names = [t["name"] for t in bn._custom_list(cache)]
    chk("P7 满仓再存：最新的这条真在磁盘上（旧代码这里是假话）",
        r["ok"] and "最新这条" in names and bn.get_template(cache, r["id"]) is not None, names[-3:])
    chk("P8 满仓再存：顶掉的是最旧那条，总数仍在上限",
        "t000" not in names and names[0] == "t001" and len(names) == bn.MAX_CUSTOM_TEMPLATES, names[:3])
    chk("P9 回执把「顶掉了最旧的」说清楚，不闷声", "顶掉" in r["msg"], r)

# ── 5. 一条完整的存读回环 ──────────────────────────────────────
with tempfile.TemporaryDirectory(prefix="gz-bn-round-") as book_dir:
    doc = {"marks": [{"text": "回环里的划线", "ch": "0001.md", "tag": "point",
                      "created_at": "1756864800"}],
           "entries": [{"title": "回环条目", "body": "正文", "refs": [{"text": "q", "ch": "0001.md"}]}]}
    bn.save_notes(book_dir, doc)
    back = bn.load_notes(book_dir)
    chk("R1 存下去再读回来：划线、条目、标签都在",
        back["marks"][0]["text"] == "回环里的划线" and back["marks"][0]["tag"] == "point"
        and back["entries"][0]["title"] == "回环条目", back)
    chk("R2 读回的时间戳已是整数（前端/导出不用再自己转）",
        isinstance(back["marks"][0]["created_at"], int), back["marks"][0])
    bn.save_notes(book_dir, doc)                     # 再存一次才谈得上「覆盖上一版」
    chk("R3 覆盖保存会留一份 .prev，旧内容不凭空消失",
        os.path.isfile(bn.notes_path(book_dir) + ".prev"), os.listdir(book_dir))

print()
print("书本地笔记存盘：通过 %d 项，失败 %d 项" % (PASSED, len(FAIL)))
if FAIL:
    for f in FAIL:
        print("  ✗ " + f)
    sys.exit(1)
print("ok  坏档读得开、坏时间戳/坏形状丢不掉整份、模板满仓不假报成功")
