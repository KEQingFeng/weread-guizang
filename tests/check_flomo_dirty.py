#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""归藏门禁：flomo 便签账本（cache/flomo/notes.json）的脏字段兜底。

一句话：**账里一个字段写脏了，便签那一屏（列表 / 标签 / 记忆画像 / 收成书 / 再导入）
不许白屏。**

这本账不只本进程在写：用户会手工编辑，旧版本也落过别的形状。`load()` 原先只丢掉
「不是字典 / 没 id」的条目，字段本身直读直用，于是：

  · `"words": "abc"` → summary_stats / build_portrait 在 int() 处抛 ValueError，
    /api/flomo/stats 与「记忆画像」整屏取不到数；
  · `"ts": "oops"` → pick / recent_tags 的排序键抛穿 /api/flomo/list；
  · `"tags": "读书"` → `for t in "读书"` 被逐字拆成「读」「书」两个假标签，药丸数字全错；
  · `"atts": "a.png"` → import_notes 补图时 `list.append` 抛 AttributeError。

这条把坏形状逐样喂进 load，要求列表 / 标签 / 画像 / 收成书 / 再导入都不抛，而且：
能认的数字仍然认得出（"42" 就是 42、数字串 ts 照认）；条目不因为一个字段坏掉就整条丢；
裸字符串收成「一个元素」而不是被逐字拆开。另有一条真机路径：拿共享夹具的导出包再导
一次，已有条目 atts 是脏字符串时补图不许炸。

全程在临时目录里跑，不起服务、不联网、不碰真实 cache/。
"""
import json
import os
import pathlib
import shutil
import sys
import tempfile

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))          # 夹具与套件同目录
sys.path.insert(0, str(HERE.parent))   # 被测模块在仓库根
import flomo_notes as fn  # noqa: E402
import flomo_fixture as fx  # noqa: E402

FAIL = []
PASSED = 0


def chk(name, cond, extra=""):
    global PASSED
    print(("PASS  " if cond else "FAIL  ") + name + ("" if cond else "   " + str(extra)[:220]))
    if cond:
        PASSED += 1
    else:
        FAIL.append(name)


ROOT = tempfile.mkdtemp(prefix="gz-flomo-dirty-")
FN = os.path.join(ROOT, "flomo")          # 相当于 ui_server 里的 cache/flomo/
OUT = os.path.join(ROOT, "books")


def put(memos, **top):
    os.makedirs(FN, exist_ok=True)
    d = {"memos": memos}
    d.update(top)
    with open(fn.path_in(FN), "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False)


# ── A：脏账本身：认得出的认、认不出的回安全值、条目不丢 ──────────
DIRTY = [
    {"id": "fm_dirty", "date": "2026-01-01", "clock": "09:30:00", "ts": "oops",
     "md": "#读书 这一条字段全是脏的", "tags": "读书", "words": "abc",
     "atts": "a.png", "book": 123},
    {"id": "fm_num", "date": 20260102, "clock": 930, "ts": "1700000000",
     "md": "数字串要认得出", "tags": ["灵感"], "words": "42", "atts": ["b.png"],
     "custom": {"x": 1}},
    {"id": "fm_empty", "date": "", "clock": "", "ts": None, "md": "",
     "tags": None, "words": 0, "atts": None},
    "不是字典",
    {"md": "没 id"},
]
put(DIRTY, at="oops", src=7)
d = fn.load(FN)
chk("A1 脏账读得开：坏条目丢掉、能用的留下（3 条）", len(d["memos"]) == 3, len(d["memos"]))
chk("A2 顶层脏值不穿出去（at 收成 0、src 收成字符串）", d["at"] == 0 and d["src"] == "7",
    (d["at"], d["src"]))
chk("A3 tags 裸字符串收成「一个标签」，没被逐字拆开",
    d["memos"][0]["tags"] == ["读书"], d["memos"][0]["tags"])
chk("A4 认不出的字数按正文重算（words_of 那把尺，不是硬塞 0）",
    d["memos"][0]["words"] == fn.words_of(d["memos"][0]["md"]) and d["memos"][0]["words"] > 0,
    (d["memos"][0]["words"], fn.words_of(d["memos"][0]["md"])))
chk("A5 认得出的数字照认（words \"42\" → 42）", d["memos"][1]["words"] == 42,
    d["memos"][1]["words"])
chk("A6 ts 脏值回 0、数字串照认（\"1700000000\" → int）",
    d["memos"][0]["ts"] == 0 and d["memos"][1]["ts"] == 1700000000,
    (d["memos"][0]["ts"], d["memos"][1]["ts"]))
chk("A7 atts 裸字符串收成单元素 list、空形状当空 list（收成书要拿 basename）",
    d["memos"][0]["atts"] == ["a.png"] and d["memos"][2]["atts"] == [],
    (d["memos"][0]["atts"], d["memos"][2]["atts"]))
chk("A8 book 号统一成字符串（界面拿它当目录名）", d["memos"][0]["book"] == "123",
    d["memos"][0]["book"])
chk("A9 date / clock / md 统一成字符串（前端拼接不再 TypeError）",
    all(isinstance(d["memos"][1][k], str) for k in ("date", "clock", "md")),
    {k: d["memos"][1][k] for k in ("date", "clock", "md")})
chk("A10 不认识的字段原样留着（不因为洗字段把扩展位洗掉）",
    d["memos"][1].get("custom") == {"x": 1}, d["memos"][1].get("custom"))

# ── B：读它的那几处都不抛，且数字/标签口径是对的 ─────────────────
picked = fn.pick(d)
chk("B1 列表页不抛（排序键不再 int(\"oops\")）", picked["count"] == 3, picked["count"])
chk("B2 列表按 ts 倒序（数字串那条排最前、ts 相同的按 clock 收尾）",
    [m["id"] for m in picked["memos"]] == ["fm_num", "fm_empty", "fm_dirty"],
    [m["id"] for m in picked["memos"]])
pub = fn.public(d["memos"][0])
chk("B3 交给界面/MCP 的那份字段类型稳定（ts / words 是整数，tags / atts 是 list）",
    isinstance(pub["ts"], int) and isinstance(pub["words"], int)
    and isinstance(pub["tags"], list) and isinstance(pub["atts"], list), pub)
stats = fn.summary_stats(d)
chk("B4 骨架统计不抛；裸字符串标签算一个「读书」而不是「读」「书」",
    ("读书", 1) in stats["top_tags"] and not any(t in ("读", "书")
                                              for t, _n in stats["top_tags"]),
    stats["top_tags"])
chk("B5 图按条目上的 atts 张数算（脏成裸字符串也算一张）", stats["images"] == 2,
    stats["images"])
portrait = fn.build_portrait(d)
chk("B6 记忆画像不抛；高峰时段与字数都是整数",
    all(isinstance(h, int) for h, _n in portrait["hours"])
    and isinstance(portrait["words"], int), (portrait["hours"], portrait["words"]))
chk("B7 画像里的条数与账上一致（一条没被丢掉）", portrait["memos"] == 3, portrait["memos"])
recent = fn.recent_tags(d)
chk("B8 最近标签不抛（同样的排序键）", "读书" in recent and "灵感" in recent, recent)
chk("B9 标签药丸计数与筛选同一把尺（读书这一支数得出 1 条）",
    fn.tag_counts(d) == sorted(fn.tag_counts(d), key=lambda kv: (-kv["n"], kv["tag"]))
    and any(x["tag"] == "读书" and x["n"] == 1 for x in fn.tag_counts(d)),
    fn.tag_counts(d))
chk("B10 筛「读书」能筛出那条脏账（标签没因为逐字拆开而失真）",
    [m["id"] for m in fn.pick(d, tag="读书")["memos"]] == ["fm_dirty"],
    fn.pick(d, tag="读书")["memos"])

# ── C：收成书这条路：脏字段不许在 memo_markdown / meta 上翻车 ────
calls = {}


def fake_import(out_root, name, blob, title=None, author=None, book_id_prefix=None,
                source=None):
    calls["blob"] = blob
    calls["title"] = title
    bdir = os.path.join(out_root, "flomo-dirty")
    os.makedirs(os.path.join(bdir, "images"), exist_ok=True)
    return {"id": "flomo-dirty", "dir": bdir, "title": title}


info = fn.to_shelf(FN, "fm_dirty", OUT, import_fn=fake_import)
chk("C1 脏条目也能收成书（memo_markdown 不炸、书名出得来）",
    info.get("dir") and calls.get("blob"), info)
chk("C2 落进书的 meta 里 words 是整数（脏的 \"abc\" 不许原样带进去）",
    isinstance(info["meta"]["words"], int) and info["meta"]["words"] == fn.words_of(
        fn.one(fn.load(FN), "fm_dirty")["md"]), info["meta"])
chk("C3 meta 里的标签是 list（同一把尺）", info["meta"]["tags"] == ["读书"], info["meta"])

# ── D：再导入这条路：已有条目的 atts 是脏字符串时补图不许炸 ──────
FN2 = os.path.join(ROOT, "flomo2")
os.makedirs(FN2, exist_ok=True)
parsed = fn.parse_html(fx.export_html())
target = parsed[3]                        # 带图的那一条
old = dict(target)
old.update({"ts": "oops", "words": "no", "atts": "旧的.txt"})   # atts 是裸字符串
with open(fn.path_in(FN2), "w", encoding="utf-8") as f:
    json.dump({"memos": [old]}, f, ensure_ascii=False)
res = fn.import_notes(FN2, fx.export_zip(), "z.zip")
after = fn.one(fn.load(FN2), target["id"])
chk("D1 已有条目 atts 是脏字符串时，再导入补图不抛且只补不重", res["patched"] == 1, res)
chk("D2 补上的图是真落盘的哈希名，脏字符串还留着（只增不改）",
    "旧的.txt" in after["atts"] and len(after["atts"]) == 2, after["atts"])
chk("D3 再导入后条目形状是干净的（ts / words 收正、tags 是 list）",
    isinstance(after["ts"], int) and isinstance(after["words"], int)
    and isinstance(after["tags"], list), after)
chk("D4 导一遍不丢别的条目（added + existed 对得上解析条数）",
    res["added"] + res["existed"] == len(parsed), res)

# ── E：洗一遍再存再读是幂等的（脏账不会越洗越变） ───────────────
before = json.loads(json.dumps(d))
fn.save(FN, d)
d2 = fn.load(FN)
chk("E1 规范化后的账再存再读一字不差（幂等）", d2["memos"] == before["memos"],
    (d2["memos"], before["memos"]))

shutil.rmtree(ROOT, ignore_errors=True)
print()
print("flomo 脏账本兜底：通过 %d 项，失败 %d 项" % (PASSED, len(FAIL)))
if FAIL:
    for f in FAIL:
        print("  ✗ " + f)
    sys.exit(1)
print("ok  脏字段不再让列表 / 标签 / 画像 / 收成书 / 再导入翻车")
