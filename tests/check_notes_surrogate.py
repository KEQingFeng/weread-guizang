#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""书本地笔记的「写盘口」离线自测：半个代理码点不再把整份笔记打成「存不下」。

坏的是哪一种输入
    JS 把一条含 emoji 的字符串**拦腰截断**（界面按字数截显示、或用户删到一半的
    那条划线）会剩下一个落单的代理码点，形如 `\\ud83d`。`JSON.stringify` 按
    well-formed 口径把它原样写成 `"\\ud83d"` 转义发出来，Python 的 `json.load`
    也照收 —— 于是这个码点一路走到 `book_notes._write_json`。

为什么它是一次全损
    `json.dump(..., ensure_ascii=False)` 把那个码点原样交给 utf-8 编码器，当场
    `UnicodeEncodeError: 'utf-8' codec can't encode character '\\ud83d'`。`/api/mynotes`
    把它兜成「存不下」——用户这一轮改的**整份**笔记（不止带 emoji 的那条）都没落盘，
    盘上还留一个写了一半的 `notes.json.tmp`。读写两侧的其它字段收口都做过，唯独
    字符串字段的这把尺子（`_clip`）没洗代理码点。

这一条钉住
    A 对照：不洗直接 `json.dump(..., ensure_ascii=False)` 真会抛（不是空想）。
    B 存盘：划线 / 批注 / 条目标题与正文 / 引用 / 章节号里带半个码点，整份存得下，
      盘上恰好一份 notes.json、没有 .tmp 残骸。
    C 读回：半个码点被洗掉，同一字段里的正常文字一个不少。
    D 成对的不误伤：真 emoji（🌱 / 👍 / 代理对）原样留着。
    E 手改档：notes.json 里手写的 `\\ud800` 读得开、洗得掉；导出 notes.md 与
      思维导图 SVG 都写得出（SVG 还得是合法 XML）。
    F 模板：自定义模板的名字 / 正文带半个码点，存得下也读得回。
    G 口径不变：洗在截断之前，字段长度上限还是原来那个数。
    H 章节目录（`_catalog.json`）：标题里的半个码点不再让整篇笔记导出打回。

全程离线：只调 book_notes 的纯函数 + 临时目录，不 import 服务、不联网、不开浏览器。

跑法：.venv/bin/python tests/check_notes_surrogate.py
"""
import json
import os
import pathlib
import sys
import tempfile
import xml.etree.ElementTree as ET

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import book_notes as bn  # noqa: E402

HALF_HI = "\ud83d"          # 半个高位代理（截断的 emoji 前半）
HALF_LO = "\udfff"          # 半个低位代理
EMOJI = "🌱👍"              # 成对的、正经的 emoji

FAIL = []
PASSED = 0


def chk(name, cond, extra=""):
    global PASSED
    print(("PASS  " if cond else "FAIL  ") + name + ("" if cond else "   " + str(extra)[:220]))
    if cond:
        PASSED += 1
    else:
        FAIL.append(name)


def _tmp():
    return tempfile.mkdtemp(prefix="guizang-notes-surrogate-")


def _raw(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


# ── A 对照：这个输入真的写不进 utf-8 文件 ──────────────────────────
print("── A 对照：半个代理码点直接落盘会抛 ──")
_ctrl = _tmp()
try:
    with open(os.path.join(_ctrl, "raw.json"), "w", encoding="utf-8") as f:
        json.dump({"text": "前半" + HALF_HI}, f, ensure_ascii=False)
    chk("A1 不洗直接 json.dump 会 UnicodeEncodeError（证明这不是空想）", False,
        "居然写成功了 —— 这条对照本身需要重看")
except UnicodeEncodeError:
    chk("A1 不洗直接 json.dump 会 UnicodeEncodeError（证明这不是空想）", True)
chk("A2 json.load 收得下 \"\\ud800\" 这种转义（所以它走得进来）",
    json.loads('{"t": "\\ud800"}')["t"] == "\ud800")


# ── B / C 存盘：整份存得下，读回来半个码点没了、真文字还在 ──────────
print("\n── B/C 存盘与读回 ──")
book = _tmp()
doc = {
    "marks": [
        {"id": "m1" + HALF_HI, "text": "划线的前半" + HALF_HI, "memo": "批注" + HALF_LO,
         "prefix": "前" + HALF_HI, "suffix": "后" + HALF_LO, "ch": "0001.md" + HALF_HI,
         "created_at": 1756864800, "tag": "point"},
        {"text": "后半截才断的 emoji" + HALF_HI, "created_at": "1756864801"},
        {"text": HALF_HI, "created_at": "1756864803"},      # 只有半个码点 → 洗完就是空
    ],
    "entries": [
        {"id": "n1" + HALF_LO, "title": "标题" + HALF_HI, "body": "正文里断了" + HALF_LO,
         "refs": [{"text": "引用" + HALF_HI, "ch": "0002.md" + HALF_LO}],
         "created_at": 1756864802},
    ],
}
_raised = None
try:
    bn.save_notes(book, doc)
except Exception as e:                                  # 修好之前这里就是 UnicodeEncodeError
    _raised = e
chk("B1 带半个代理码点的整份笔记存得下（不再「存不下」）", _raised is None, repr(_raised))
chk("B2 盘上确实写出了 notes.json", os.path.isfile(bn.notes_path(book)))
chk("B3 没留下 .tmp 残骸（写好再原子改名）",
    not os.path.exists(bn.notes_path(book) + ".tmp"), sorted(os.listdir(book)))

back = bn.load_notes(book)
by_id = {m.get("id"): m for m in back["marks"]}
m0 = by_id.get("m1")
chk("C1 划线的正文洗掉半个码点、正常文字一个不少",
    bool(m0) and m0["text"] == "划线的前半", repr(m0))
chk("C2 批注 / 前缀 / 后缀 / 章节号同样洗掉",
    bool(m0) and m0["memo"] == "批注" and m0["prefix"] == "前" and m0["suffix"] == "后"
    and m0["ch"] == "0001.md", m0)
chk("C3 id 里的半个码点也洗掉（不写成坏字典键）",
    "m1" in by_id and all(bn.SURROGATE.search(str(k)) is None for k in by_id), list(by_id))
chk("C4 条目标题与正文、引用都干净",
    back["entries"][0]["title"] == "标题" and back["entries"][0]["body"] == "正文里断了"
    and back["entries"][0]["refs"][0]["text"] == "引用"
    and back["entries"][0]["refs"][0]["ch"] == "0002.md", back["entries"][0])
chk("C5 落盘文本里一个代理码点都不剩", bn.SURROGATE.search(_raw(bn.notes_path(book))) is None)
chk("C6 「只剩半个码点」的那条洗完成空正文，照样按无正文筛掉；带真字的都留着",
    len(back["marks"]) == 2, [m["text"] for m in back["marks"]])


# ── D 成对的 emoji 不误伤 ────────────────────────────────────────
print("\n── D 真 emoji 原样保留 ──")
book2 = _tmp()
bn.save_notes(book2, {"marks": [{"id": "m9", "text": "好" + EMOJI + "的划线",
                                 "memo": "标" + EMOJI, "created_at": 1756864800}]})
b2 = bn.load_notes(book2)["marks"][0]
chk("D1 划线正文里的 emoji 原样留着", b2["text"] == "好" + EMOJI + "的划线", repr(b2["text"]))
chk("D2 批注里的 emoji 原样留着", b2["memo"] == "标" + EMOJI, repr(b2["memo"]))


# ── E 手改过的档 / 导出 / 导图 ───────────────────────────────────
print("\n── E 手改档读得开、导出与导图写得出 ──")
book3 = _tmp()
with open(bn.notes_path(book3), "w", encoding="utf-8") as f:
    f.write('{"marks": [{"id": "h1", "text": "手改\\ud800的", "memo": "m\\udfff",'
            ' "created_at": 1756864800}], "entries": []}')
b3 = bn.load_notes(book3)
chk("E1 手写 \\ud800 的档读得开（不被当坏档备份走）",
    len(b3["marks"]) == 1, sorted(os.listdir(book3)))
chk("E2 读进来就洗掉了", b3["marks"][0]["text"] == "手改的" and b3["marks"][0]["memo"] == "m",
    b3["marks"][0])

_ex = None
try:
    bn.export_notes(book3, b3, book_label="手改档")
except Exception as e:
    _ex = e
chk("E3 导出 notes.md 写得出", _ex is None and
    os.path.isfile(os.path.join(book3, bn.EXPORT_MD)), repr(_ex))
if os.path.isfile(os.path.join(book3, bn.EXPORT_MD)):
    chk("E4 notes.md 里没有代理码点", bn.SURROGATE.search(_raw(os.path.join(book3, bn.EXPORT_MD))) is None)

_mp = None
try:
    bn.mindmap(book3, b3, book_label="手改档")
except Exception as e:
    _mp = e
svg_path = os.path.join(book3, bn.EXPORT_MAP)
chk("E5 思维导图 SVG 写得出", _mp is None and os.path.isfile(svg_path), repr(_mp))
if os.path.isfile(svg_path):
    svg = _raw(svg_path)
    chk("E6 SVG 里没有代理码点", bn.SURROGATE.search(svg) is None)
    try:
        ET.fromstring(svg)
        chk("E7 SVG 仍是合法 XML（浏览器里不是一张破图）", True)
    except ET.ParseError as e:
        chk("E7 SVG 仍是合法 XML（浏览器里不是一张破图）", False, e)


# ── F 模板 ──────────────────────────────────────────────────────
print("\n── F 自定义模板 ──")
cache = _tmp()
res = bn.save_template(cache, {"name": "模板" + HALF_HI, "desc": "说明" + HALF_LO,
                               "body": "正文" + HALF_HI})
chk("F1 模板带半个码点也存得下（回 ok）", bool(res.get("ok")), res)
tpl = [t for t in bn.templates(cache) if not t.get("official") and t["id"] == res["id"]]
chk("F2 读回来名字与正文都干净",
    tpl and tpl[0]["name"] == "模板" and tpl[0]["body"] == "正文", tpl)


# ── G 截断口径不变 ──────────────────────────────────────────────
print("\n── G 字段上限口径 ──")
clip_long = bn._clip("a" * 3999 + HALF_HI + "b" * 10, 4000)
chk("G1 洗在截断之前：长度仍是 4000，且不含代理码点",
    len(clip_long) == 4000 and bn.SURROGATE.search(clip_long) is None, len(clip_long))
chk("G2 纯 ASCII 的裁剪结果与原来逐字相同",
    bn._clip("x" * 5000, 4000) == "x" * 4000)


# ── H 章节目录（_catalog.json）的坏字符 ─────────────────────────
# 章标题也吃盘上那一份 `_catalog.json`（手改 / 旧版本 / 云同步都会落回别的形状），
# 导出与导图把它拼进正文（`### %s`）再落盘。修前 `str(t)[:120]` 只裁长度，落单的
# 半个代理码点一路带到 `f.write(..., encoding="utf-8")`，整篇 notes.md 打回，
# 盘上还留一个 `notes.md.tmp`。
print("\n── H 章节目录坏字符 ──")
book4 = _tmp()
with open(os.path.join(book4, "_catalog.json"), "w", encoding="utf-8") as f:
    json.dump(["第一" + HALF_HI + "章", "第二章" + EMOJI], f)
_ctl_h = None
try:
    with open(os.path.join(book4, "ctl.md"), "w", encoding="utf-8") as f:
        f.write("### " + "第一" + HALF_HI + "章")          # 对照：不洗就是这么抛的
except UnicodeEncodeError as e:
    _ctl_h = e
chk("H0 对照：目录标题里的半个码点直接拼进正文落盘确实会 UnicodeEncodeError",
    _ctl_h is not None, "居然写成功了 —— 这条对照本身需要重看")
titles4 = bn.chapter_titles(book4)
chk("H1 chapter_titles 把目录标题洗成干净字符串（真 emoji 不误伤）",
    titles4.get("0000.md") == "第一章" and titles4.get("0001.md") == "第二章" + EMOJI,
    titles4)
bn.save_notes(book4, {"marks": [{"id": "c1", "text": "划一句", "ch": "0000.md",
                                 "created_at": 1756864800}]})
_ce = None
try:
    bn.export_notes(book4, bn.load_notes(book4), titles=titles4)
except Exception as e:                                     # 修前这里就是 UnicodeEncodeError
    _ce = e
chk("H2 导出 notes.md 写得出（坏章标题不再带走整篇）", _ce is None and
    os.path.isfile(os.path.join(book4, bn.EXPORT_MD)), repr(_ce))
chk("H3 导出后没留下 notes.md.tmp 残骸",
    not os.path.exists(os.path.join(book4, bn.EXPORT_MD + ".tmp")),
    sorted(os.listdir(book4)))
if os.path.isfile(os.path.join(book4, bn.EXPORT_MD)):
    _h = _raw(os.path.join(book4, bn.EXPORT_MD))
    chk("H4 导出稿里没有代理码点，且用上的是洗干净的那个章标题",
        bn.SURROGATE.search(_h) is None and "### 第一章" in _h, _h[:200])


print()
print("书本地笔记坏字符存盘：通过 %d 项，失败 %d 项" % (PASSED, len(FAIL)))
if FAIL:
    for f in FAIL:
        print("  ✗ " + f)
    sys.exit(1)
print("ok  半个代理码点不再把整份笔记打成「存不下」，真 emoji 与长度口径都没动")
