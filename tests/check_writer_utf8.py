#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""写作那一格的坏字符（writer.py）：半个代理码点不许把「保存」打成失败。

从浏览器来的稿子正文 / 标题是 JSON 字符串：JS 把字符串拦腰截断时会留下一个落单的
代理码点（U+D800–U+DFFF），`json.loads` 收得下（形如 "\\ud83d" 那个转义），于是它会
一路走到落盘那一步 —— draft.md 按 utf-8 写、meta.json 与 _books.json 按
`ensure_ascii=False` 写，三处都当场 UnicodeEncodeError。用户看到的是**写了一大段、
一按保存就没了**（书目录里还留一个半截 .tmp）；而回执本身也是 ensure_ascii=False
编码的，所以坏字符留在内存里那一份，连响应都发不出去。

`check_writer_dirty.py` 管的是「脏字段折不成数」，这一份专补「脏字符写不进 utf-8」
这一类 —— 与 mindmap / board / person / anna 的存盘收尾同一个口径。

只碰 writer.py 的纯函数与系统临时目录里的沙盒书库，不起服务、不联网、不碰真实书库，
所以归静态段，--fast 也要带。四条硬要求：
  · 落单的代理码点被摘掉（高位 / 低位 / 只在中间 / 只剩它）；
  · 真 emoji（含肤色修饰符）与 ZWJ 连字（🧑‍💻）一个字不动；
  · 洗完的正文与账本真能按各自那条路落盘，且回执能按回包那条路编码；
  · 手改成 `"\\ud83d"` 转义的 meta.json / _books.json 读出来也是干净的（旧文件自愈）。
"""
import json
import os
import pathlib
import shutil
import sys
import tempfile

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

# 数据目录指进临时沙盒再 import writer：免得书库那几条路真去动用户真实的书库。
SANDBOX = pathlib.Path(tempfile.mkdtemp(prefix="guizang-writer-utf8-"))
os.environ["GUIZANG_DATA"] = str(SANDBOX / "cache")
os.environ["GUIZANG_BOOKS"] = str(SANDBOX / "books")

import writer  # noqa: E402

FAIL = []
PASSED = 0


def chk(name, cond, extra=""):
    global PASSED
    print(("PASS  " if cond else "FAIL  ") + name + ("" if cond else "   " + repr(extra)[:220]))
    if cond:
        PASSED += 1
    else:
        FAIL.append(name)


LIB = str(SANDBOX / "library")
os.makedirs(LIB, exist_ok=True)


def no_raise(fn, *a, **kw):
    """跑一次，回 (有没有抛, 结果或异常名)。门禁就是要抓所有抛。"""
    try:
        return False, fn(*a, **kw)
    except Exception as e:                      # noqa: BLE001
        return True, "%s: %s" % (type(e).__name__, str(e)[:120])


def json_encodable(obj):
    """照 ui_server._json 那条路真编码一遍：ensure_ascii=False + utf-8。

    坏字符留在回执里就是这个表现 —— 落盘那一刀修好了，响应照样发不出去。
    """
    try:
        json.dumps(obj, ensure_ascii=False).encode("utf-8")
        return True
    except UnicodeEncodeError:
        return False


def raw_utf8_encodable(text):
    """照 draft.md 那条路真写一遍 utf-8（真建文件，不只 encode）。"""
    p = SANDBOX / "raw.md"
    try:
        with open(p, "w", encoding="utf-8") as f:
            f.write(text)
        return True
    except UnicodeEncodeError:
        return False


def on_disk_clean(path):
    """盘上这份文件里不该有落单的半个代理码点。"""
    try:
        with open(path, encoding="utf-8") as f:
            return not writer._SURROGATE.search(f.read())
    except OSError:
        return False


def seed_dirty_meta(bid, **fields):
    """把 meta.json 手改成含 `"\\ud83d"` 转义的样子（ensure_ascii=True 才写得进去）。"""
    d = writer.dir_of(LIB, bid)
    m = json.load(open(writer.meta_path(d), encoding="utf-8"))
    m.update(fields)
    pathlib.Path(writer.meta_path(d)).write_text(
        json.dumps(m, ensure_ascii=True), encoding="ascii")
    return d


HI, LO = "\ud83d", "\ude00"          # 落单的高位 / 低位代理码点


# ── 一、_drop_surrogates：摘掉落单的那半个 ─────────────────────────
chk("落单的高位代理码点被摘掉", writer._drop_surrogates("a" + HI + "b") == "ab")
chk("落单的低位代理码点被摘掉", writer._drop_surrogates("a" + LO + "b") == "ab")
chk("只剩一个落单代理码点时给空串", writer._drop_surrogates(HI) == ""
    and writer._drop_surrogates(LO) == "")
chk("dict / list 逐层走（meta 那种嵌套结构）",
    writer._drop_surrogates({"t": "标" + HI + "题",
                             "sparks": ["灵" + LO + "感", 3, None]})
    == {"t": "标题", "sparks": ["灵感", 3, None]})
chk("不是字符串的原样留着", writer._drop_surrogates(7) == 7
    and writer._drop_surrogates(None) is None)
chk("清洗是幂等的（洗两遍与一遍相同）",
    writer._drop_surrogates(writer._drop_surrogates("x" + HI + "y"))
    == writer._drop_surrogates("x" + HI + "y"))
src = {"t": "a" + HI + "b"}
writer._drop_surrogates(src)
chk("返回的是新对象，不就地改调用方那份", src["t"] == "a" + HI + "b")

# 反向保护：别洗过头。emoji 本身是一个真字符（不是代理对），ZWJ 是连字笔画。
chk("真 emoji（含肤色修饰符）一个字不动",
    writer._drop_surrogates("👋\U0001f3fd 归藏") == "👋\U0001f3fd 归藏")
chk("ZWJ 连字（🧑‍💻）原样保留",
    writer._drop_surrogates("🧑\u200d💻") == "🧑\u200d💻")

# 对照：不清洗的坏字符确实过不了这两条落盘路 —— 证明上面不是空转。
chk("对照：未清洗的落单代理码点确实写不进 utf-8",
    not raw_utf8_encodable("正文" + HI)
    and not json_encodable({"title": "名" + LO}))


# ── 二、create / save：脏正文与脏标题照样存得下 ───────────────────
threw, made = no_raise(writer.create, LIB, "标" + HI + "题", "正文" + HI + "后")
chk("create 不抛（脏正文 + 脏标题）", not threw, made)
if not threw:
    bid, meta = made
    d = writer.dir_of(LIB, bid)
    chk("create 回的标题是干净的", meta["title"] == "标题", repr(meta["title"]))
    chk("create 回的 meta 能按回包那条路编码", json_encodable(meta), repr(meta))
    chk("draft.md 落盘干净", on_disk_clean(writer.draft_path(d)))
    chk("meta.json 落盘干净", on_disk_clean(writer.meta_path(d)))

    # ── 三、save：正文改动与标题都要洗 ────────────────────────────
    threw, saved = no_raise(writer.save, LIB, bid, "改过" + LO + "的正文",
                            title="新" + HI + "题", rev=meta.get("rev"))
    chk("save 不抛（脏正文 + 脏标题）", not threw, saved)
    if not threw:
        ok, m2 = saved
        chk("save 回 ok 且回执干净可编码",
            ok and m2["title"] == "新题" and json_encodable(m2), repr(m2)[:160])
        chk("save 后 draft.md 落盘干净", on_disk_clean(writer.draft_path(d)))
        chk("save 后 meta.json 落盘干净", on_disk_clean(writer.meta_path(d)))

    # 正常内容一个字不改：emoji 与 ZWJ 照留。
    threw, saved2 = no_raise(writer.save, LIB, bid, "真👋\U0001f3fd与🧑\u200d💻",
                             title="👋真标题", rev=None)
    chk("save 不把真 emoji / ZWJ 洗坏",
        not threw and saved2[1]["title"] == "👋真标题"
        and writer.read(LIB, bid)["text"] == "真👋\U0001f3fd与🧑\u200d💻", saved2)

    # ── 四、手改脏的 meta.json：读出来自愈，再存一次盘洗回盘上 ─────
    seed_dirty_meta(bid, title="旧" + HI + "名", sparks=["角" + LO + "度"])
    threw, got = no_raise(writer.read, LIB, bid)
    chk("read 手改脏的 meta.json 不抛", not threw, got)
    if not threw:
        chk("读出来的标题与方向卡都干净且可编码",
            got["title"] == "旧名" and got["sparks"] == ["角度"] and json_encodable(got),
            repr(got)[:160])
    threw, _ = no_raise(writer.save, LIB, bid, "再存一次", rev=None)
    chk("存一次盘把盘上那份也洗回来了",
        not threw and on_disk_clean(writer.meta_path(d)))


# ── 五、册账（_books.json）：书名列表也不许被坏字符带走 ───────────
threw, made_book = no_raise(writer.create_book, LIB, "书" + HI + "名")
chk("create_book 不抛且回的书名干净可编码",
    not threw and made_book["title"] == "书名" and json_encodable(made_book), made_book)
if not threw:
    threw, renamed = no_raise(writer.rename_book, LIB, made_book["id"], "改" + LO + "名")
    chk("rename_book 不抛且回的书名干净可编码",
        not threw and renamed[1]["title"] == "改名" and json_encodable(renamed[1]), renamed)
    chk("_books.json 落盘干净", on_disk_clean(writer.books_path(LIB)))
    # 手改脏的册账：读出来也不带坏字符。
    bp = writer.books_path(LIB)
    raw = json.load(open(bp, encoding="utf-8"))
    raw["books"][0]["title"] = "册" + HI + "名"
    pathlib.Path(bp).write_text(json.dumps(raw, ensure_ascii=True), encoding="ascii")
    threw, books = no_raise(writer.load_books, LIB)
    chk("load_books 读手改脏的册账也不带坏字符",
        not threw and books and books[0]["title"] == "册名" and json_encodable(books),
        books)

shutil.rmtree(SANDBOX, ignore_errors=True)

print("\n写作坏字符体检：通过 %d 项，失败 %d 项" % (PASSED, len(FAIL)))
for name in FAIL:
    print("  x " + name)
sys.exit(1 if FAIL else 0)
