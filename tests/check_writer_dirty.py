#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""归藏门禁：写作那一格的账本（meta.json / _books.json）脏字段兜底。

一句话：**账里一个字段写脏了，写作那一屏（稿列 / 左栏书树 / 存盘 / 方向卡）
不许白屏，更不许存不进去。**

这两本账都是用户能直接编辑的文本文件，旧版本也落过别的形状，而读它们的地方
原来一律直读直用 `int(meta.get("words") or 0)`、`list(meta.get("sparks") or [])`：

  · `"words": "很多"`（或 `"created": "昨天"` / `"rev": "abc"`）→ `/api/writer`
    的 `list_drafts` / `brief` / `note` 在 int() 处抛 ValueError，写作屏左栏整栏
    加载不出来，`book_detail` 里的书树也跟着空；
  · `"rev": "abc"` 还会让 `save()` 第一行的 `int(meta["rev"])` 抛出去 ——
    用户敲完点保存，回的是「存不下」，一个字没落。这是最要命的一档：
    坏账本把「写好的稿子存不进去」变成了现实；
  · `"sparks": "灵感角度"` → `list("灵感角度")` 把它逐字拆成「灵」「感」「角」「度」，
    下一次 `remember_sparks` 再写回盘上就再也拼不回来，喂模型的「别重复」点名
    成了一串单字；`"chapters": "write_…"` 这种裸字符串同理，一段章号会被拆成
    一个个字、一个都认不出来，册账里那本书看着就「章没了」。

这条把坏形状逐样喂进去，要求列表 / 单篇 / 书树 / 存盘 / 快照 / 方向卡都不抛，
而且：认得出的数字仍认得出（"42" 就是 42、42.0 就是 42）；裸字符串收成
「一个元素」而不是被逐字拆开；条目不因为一个字段坏掉就整篇丢；存一次盘后
账本自己修成干净形状（幂等）；rev 冲突那套协议一个字没被改坏。

全程在系统临时目录里造的那座沙盒书库里跑，不起服务、不联网、不碰真实书库。
"""
import json
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import writer               # noqa: E402

FAIL = []
PASSED = 0


def chk(name, cond, extra=""):
    global PASSED
    print(("PASS  " if cond else "FAIL  ") + name + ("" if cond else "   " + repr(extra)[:220]))
    if cond:
        PASSED += 1
    else:
        FAIL.append(name)


TMP = tempfile.mkdtemp(prefix="guizang-writer-dirty-")
LIB = os.path.join(TMP, "library")
os.makedirs(LIB)


def make_draft(title="手改过的稿子", text="# 标题\n正文一段\n"):
    bid, _meta = writer.create(LIB, title, text)
    return bid, writer.dir_of(LIB, bid)


def dirty_meta(bid, **fields):
    """把这篇稿子的 meta.json 改脏（只动点名的字段，其余保持干净）。"""
    d = writer.dir_of(LIB, bid)
    m = json.load(open(writer.meta_path(d), encoding="utf-8"))
    m.update(fields)
    with open(writer.meta_path(d), "w", encoding="utf-8") as f:
        json.dump(m, f, ensure_ascii=False)
    return d


def no_raise(fn, *a, **kw):
    """跑一次，回 (有没有抛, 结果或异常名)。"""
    try:
        return False, fn(*a, **kw)
    except Exception as e:                      # noqa: BLE001 —— 门禁就是要抓所有抛
        return True, "%s: %s" % (type(e).__name__, e)


def _ls(v):
    """no_raise 的结果可能是异常字符串 —— 只把真清单取出来用，免得断言自己又抛。"""
    return v if isinstance(v, list) else []


def _dt(v):
    return v if isinstance(v, dict) else {}


# ── A：单篇 meta 的数字字段写脏 ────────────────────────────────
# 这一档是「写作屏整栏 + 存盘」的生死线。
bid, d = make_draft()
dirty_meta(bid, rev="abc", words="很多", created="昨天", updated=None)

threw, b = no_raise(writer.brief, d)
chk("A1 brief：数字字段写脏不抛", not threw, b)
chk("A1 brief：认不出的数字回 0（不是 None / 不是 '很多'）",
    isinstance(b, dict) and b.get("words") == 0 and b.get("rev") == 0
    and isinstance(b.get("created"), int) and isinstance(b.get("updated"), int), b)

threw, n = no_raise(writer.note, LIB, bid)
chk("A2 note（书架卡片）：数字字段写脏不抛、回的全是 int",
    not threw and isinstance(n, dict) and all(isinstance(n.get(k), int)
                                              for k in ("words", "rev", "updated")), n)

threw, rows = no_raise(writer.list_drafts, LIB)
chk("A3 list_drafts：脏稿子不把整栏加载带崩", not threw, rows)
chk("A3 list_drafts：这一篇没因为字段坏掉就消失",
    [r["id"] for r in _ls(rows)] == [bid], rows)

threw, one = no_raise(writer.read, LIB, bid)
chk("A4 read（编辑器进来取一次）：不抛且 rev 是 int",
    not threw and isinstance(one, dict) and isinstance(one["rev"], int), one)

# 这一条是关键：坏账本不再让「保存」这一刀被顶回。
threw, saved = no_raise(writer.save, LIB, bid, "改过的正文\n")
chk("A5 save：rev 写脏也存得进去（不再把用户的字挡在门外）", not threw, saved)
chk("A5 save：回的是成功 + 新 rev", not threw and saved[0] is True and saved[1]["rev"] == 1,
    saved)
chk("A5 save：正文真的落盘了", writer.read_text(d) == "改过的正文\n", writer.read_text(d))

disk = json.load(open(writer.meta_path(d), encoding="utf-8"))
chk("A6 存一次盘后账本自己修好：words / rev / created / updated 都是 int",
    all(isinstance(disk.get(k), int) for k in ("words", "rev", "created", "updated")), disk)

# 认得出的形状照认：这不是「一律归零」。
bid2, d2 = make_draft("数字串的稿子")
dirty_meta(bid2, rev="42", words="42.0", created="1700000000", updated=1699999999)
threw, b2 = no_raise(writer.brief, d2)
chk("A7 认得出的数字照认：rev \"42\" → 42、words \"42.0\" → 42、created 数字串 → 数字",
    not threw and b2["rev"] == 42 and b2["words"] == 42 and b2["created"] == 1700000000, b2)

# 一篇坏账本不许连累旁边那篇干净的。
bid3, d3 = make_draft("干净的稿子")
threw, rows = no_raise(writer.list_drafts, LIB)
chk("A8 一篇脏不连累别的：三篇都还在",
    not threw and sorted(r["id"] for r in _ls(rows)) == sorted([bid, bid2, bid3]), rows)
chk("A8 干净的稿子字数照算（没有被『一刀切归零』牵连）",
    next((r for r in _ls(rows) if r["id"] == bid3), {}).get("words", 0) > 0, rows)
chk("A9 list_drafts 按 updated 倒序的排序键全是 int（不会拿字符串比大小抛）",
    [r["updated"] for r in _ls(rows)] == sorted((r["updated"] for r in _ls(rows)),
                                                reverse=True), rows)


# ── B：sparks（灵感角度）被逐字拆开 ─────────────────────────────
bid4, d4 = make_draft("方向卡的稿子")
dirty_meta(bid4, sparks="灵感角度")

threw, seen = no_raise(writer.seen_sparks, LIB, bid4)
chk("B1 裸字符串 sparks 收成「一个角度」，不是逐字拆开",
    not threw and seen == ["灵感角度"], seen)

threw, ok = no_raise(writer.remember_sparks, LIB, bid4, ["第三个角度"])
chk("B2 记新角度不抛", not threw and ok is True, ok)
threw, seen2 = no_raise(writer.seen_sparks, LIB, bid4)
chk("B2 盘上还是完整角度（没被拆成单字、新角度也进去了）",
    not threw and seen2 == ["灵感角度", "第三个角度"], seen2)

# 形状不对的 sparks：不是容器就当空，别拿它当清单遍历。
bid5, d5 = make_draft("脏 sparks")
dirty_meta(bid5, sparks={"a": 1})
threw, got = no_raise(writer.seen_sparks, LIB, bid5)
chk("B3 sparks 是 dict → 空清单（不拿它的键当角度）", not threw and got == [], got)
dirty_meta(bid5, sparks=123)
threw, got = no_raise(writer.seen_sparks, LIB, bid5)
chk("B3 sparks 是数字 → 空清单（不抛）", not threw and got == [], got)
dirty_meta(bid5, sparks=["   ", "好角度", "好角度", "x" * 80])
threw, got = no_raise(writer.seen_sparks, LIB, bid5)
chk("B3 清单里的空白去掉、重复去掉、超 40 字截断",
    not threw and got == ["好角度", "x" * 40], got)
dirty_meta(bid5, sparks=["好角度"])
threw, g1 = no_raise(writer.seen_sparks, LIB, bid5)
threw2, g2 = no_raise(writer.seen_sparks, LIB, bid5)
chk("B4 读两次一字不差（洗一遍是幂等的）",
    not threw and not threw2 and g1 == g2 == ["好角度"], (g1, g2))


# ── C：册账 _books.json 的数字与 chapters ──────────────────────
bid6, _d6 = make_draft("归了册的章")
writer.create_book(LIB, "册账那本书")
raw = json.load(open(writer.books_path(LIB), encoding="utf-8"))
raw["books"][0]["created"] = "上个月"
raw["books"][0]["updated"] = {"t": 1}
raw["books"][0]["chapters"] = bid6              # 裸字符串，不是清单
with open(writer.books_path(LIB), "w", encoding="utf-8") as f:
    json.dump(raw, f, ensure_ascii=False)

threw, books = no_raise(writer.load_books, LIB)
chk("C1 册账数字写脏不抛", not threw, books)
chk("C1 册账数字回 int",
    not threw and _ls(books) and all(isinstance(b.get(k), int)
                                     for b in _ls(books) for k in ("created", "updated")), books)
chk("C2 chapters 裸字符串收成「一个章号」，不逐字拆开（书树里章还在）",
    not threw and _ls(books) and _ls(books)[0]["chapters"] == [bid6], books)

threw, det = no_raise(writer.book_detail, LIB, _ls(books)[0] if _ls(books) else {})
chk("C3 book_detail（左栏书树）：脏册账也摊得开", not threw, det)
chk("C3 book_detail：章清单里就是那一段、字数都是 int",
    not threw and _ls(_dt(det).get("chapters"))
    and _ls(_dt(det).get("chapters"))[0]["id"] == bid6
    and isinstance(_dt(det).get("words"), int), det)

# chapters 完全不是容器时当空，不拿数字 / null 去遍历。
raw["books"][0]["chapters"] = 7
with open(writer.books_path(LIB), "w", encoding="utf-8") as f:
    json.dump(raw, f, ensure_ascii=False)
threw, books2 = no_raise(writer.load_books, LIB)
chk("C4 chapters 是数字 → 空清单（不抛）",
    not threw and _ls(books2) and _ls(books2)[0]["chapters"] == [], books2)

raw["books"][0]["chapters"] = [bid6, bid6, "write_000000000000", 42]
with open(writer.books_path(LIB), "w", encoding="utf-8") as f:
    json.dump(raw, f, ensure_ascii=False)
threw, books3 = no_raise(writer.load_books, LIB)
chk("C4 章号照旧只留真在盘上的那一个（重复 / 空气 / 非字符串都剔掉）",
    not threw and _ls(books3) and _ls(books3)[0]["chapters"] == [bid6], books3)


# ── D：挪章时 delta 是脏值 ─────────────────────────────────────
raw2 = json.load(open(writer.books_path(LIB), encoding="utf-8"))
raw2["books"][0]["chapters"] = [bid]
with open(writer.books_path(LIB), "w", encoding="utf-8") as f:
    json.dump(raw2, f, ensure_ascii=False)
threw, moved = no_raise(writer.order_chapter, LIB, raw2["books"][0]["id"], bid, "上")
chk("D1 delta 是「上」：不抛、不挪、书还是那本书",
    not threw and moved[0] is True and moved[1]["moved"] is False
    and moved[1]["book"]["chapters"] == [bid], moved)
threw, moved2 = no_raise(writer.order_chapter, LIB, raw2["books"][0]["id"], bid, None)
chk("D1 delta 是 None：不抛、不挪", not threw and moved2[1]["moved"] is False, moved2)


# ── E：rev 冲突那套协议一个字没改坏 ────────────────────────────
bid7, d7 = make_draft("按 rev 存")
m7 = writer.load_meta(d7)
chk("E1 新稿子从 rev=1 开始", m7["rev"] == 1, m7["rev"])
ok7, out7 = writer.save(LIB, bid7, "第一版改", rev=0)      # 拿旧 rev 存
chk("E2 rev 对不上就不写、把服务端那份带回来（冲突当场可见）",
    ok7 is False and "conflict" in out7 and writer.read_text(d7) == "# 标题\n正文一段\n", out7)
ok8, out8 = writer.save(LIB, bid7, "第一版改", rev=1)      # 拿当前 rev 存
chk("E3 rev 对上就写、rev +1", ok8 is True and out8["rev"] == 2, out8)

# 脏 rev 直接喂 snapshot：读出来已归一，不该在「拍快照」这一步抛。
dirty_meta(bid7, rev="不是数字")
threw, snap = no_raise(writer.snapshot, d7, writer.load_meta(d7))
chk("E4 rev 脏时拍快照不抛（rev 当 0，正好留一份）", not threw, snap)
threw, saved7 = no_raise(writer.save, LIB, bid7, "第二版", rev="wot")
chk("E5 客户端 rev 写脏也不 500：按覆盖处理并照旧留快照",
    not threw and saved7[0] is True, saved7)
chk("E5 快照目录里确实留了旧正文",
    writer.versions(d7)
    and writer.version_text(d7, writer.versions(d7)[0]["name"]) is not None,
    writer.versions(d7))


shutil.rmtree(TMP, ignore_errors=True)
print()
print("写作脏账本兜底：通过 %d 项，失败 %d 项" % (PASSED, len(FAIL)))
if FAIL:
    for f in FAIL:
        print("  ✗ " + f)
    sys.exit(1)
print("ok  脏字段不再让写作那一屏加载不出、存不进；裸字符串不被逐字拆开")
