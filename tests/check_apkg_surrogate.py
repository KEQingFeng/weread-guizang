#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""导出 Anki 卡包（.apkg）的坏字符离线门禁：一个落单的半个代理码点不再把整包打没。

坏的是哪一种输入
    划线与想法是从本机索引 / 官方网关收回来的正文，里面完全可能夹着一个落单的
    代理码点（`\\ud800` 这种）：界面把一条含 emoji 的划线拦腰截断、或账本本身
    就是别的版本写下来的，JSON 里都存得下、`json.load` 也照收。书名同样可能来自
    手改过的 meta。

为什么它是一次全损
    `genanki.Package(...).write_to_file()` 落的是一个真 SQLite：卡面正文、背面
    出处、卡组名都要写进去。只要有一处编不出 utf-8，整包在这一步
    `UnicodeEncodeError` 收场，`/api/apkg` 兜成 400 + 一句英文 Python 报错 ——
    用户点「导出卡包」拿到的是「没反应」，一整本书的卡片一张都拿不到。
    文件名那条边界（content_disp）早就洗过半个码点，唯独进 SQLite 的正文没洗。

这一条钉住
    A 对照：不洗直接交给 genanki 真会 UnicodeEncodeError（不是空想）。
    B 导出：书名 / 划线正文 / 想法正文 / kind 里带半个码点，整包建得出来，盘上
      确实有一份 .apkg，里面的 SQLite 读得开、且一个代理码点都不剩。
    C 不误伤：真 emoji（代理对）与中文标点原样留在卡面上。
    D 幂等：同一本书再导一次不报错、条数一致（文件名同名覆盖）。
    E 形状：书名是 None / 数字 / 空串时照样出得来（用书号兜底），不把 TypeError
      递出去。
    F 口径：`at` 认不出时背面只少那一段日期，卡片本身仍在。

全程离线：只调 `ui_server.build_apkg` 的纯函数 + 临时沙盒目录，把 `notes_for_book`
换成一份固定条目，不联网、不起服务、不 import 浏览器那一路。

跑法：.venv/bin/python tests/check_apkg_surrogate.py
"""
import json
import os
import pathlib
import sqlite3
import sys
import tempfile
import time
import zipfile

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import selftest  # noqa: E402

selftest.export_sandbox_env()               # 必须在 import ui_server 之前设好环境变量
sys.path.insert(0, str(selftest.REPO))

import ui_server  # noqa: E402

HALF_HI = "\ud83d"          # 半个高位代理（被截断的 emoji 前半）
HALF_LO = "\udfff"          # 半个低位代理
EMOJI = "🌱👍"              # 成对的、正经的 emoji

FAIL = []
PASSED = [0]


def chk(name, cond, extra=""):
    if cond:
        PASSED[0] += 1
    print(("PASS  " if cond else "FAIL  ") + name + ("" if cond else "   " + str(extra)[:300]))
    if not cond:
        FAIL.append(name)


def _fake_notes(items, resolved="b1"):
    """把 notes_for_book 换成固定条目：卡包那一段就不再依赖索引 / 网关。"""
    def _f(any_id, title="", author=""):
        return items, resolved, "index"
    return _f


def anki_fields(path):
    """从 .apkg 里把收藏库拆出来，回每张卡的 (正面, 背面) 与卡组名。

    .apkg 是个 zip，里面那个 collection.anki2 / .anki21 才是 SQLite。落到临时
    文件再连（sqlite3 不吃内存流），读 `notes.flds`（两栏用 \\x1f 分）与
    `col.decks`（一段 JSON）。
    """
    z = zipfile.ZipFile(path)
    db = next(n for n in z.namelist() if n.endswith((".anki21", ".anki2")))
    fd, tmp = tempfile.mkstemp(prefix="guizang-apkg-", suffix=".db")
    os.close(fd)
    try:
        with open(tmp, "wb") as f:
            f.write(z.read(db))
        con = sqlite3.connect(tmp)
        try:
            rows = con.execute("select flds from notes").fetchall()
            decks = json.loads(con.execute("select decks from col").fetchone()[0])
        finally:
            con.close()
    finally:
        os.unlink(tmp)
    cards = []
    for (flds,) in rows:
        parts = flds.split("\x1f")
        cards.append((parts[0], parts[1] if len(parts) > 1 else ""))
    return cards, [d.get("name", "") for d in decks.values()]


def safe_fields(path):
    """拆包读卡；路径是 None 或包是半截时不抛 —— 坏的那一条由别的断言点名。"""
    if not path or not os.path.isfile(path):
        return [], []
    try:
        return anki_fields(path)
    except Exception:
        return [], []


def has_half_surrogate(s):
    return any(0xD800 <= ord(c) <= 0xDFFF for c in s)


# ── A 对照：这个输入真写不进 genanki 的 SQLite ────────────────────
print("── A 对照：半个代理码点直接进 genanki 会抛 ──")
import genanki  # noqa: E402

_ctl = tempfile.mkdtemp(prefix="guizang-apkg-ctl-")
_m = genanki.Model(1, "对照", fields=[{"name": "正面"}, {"name": "背面"}],
                   templates=[{"name": "t", "qfmt": "{{正面}}", "afmt": "{{背面}}"}])
_d = genanki.Deck(1, "对照" + HALF_HI)
_d.add_note(genanki.Note(model=_m, fields=["前半" + HALF_HI, "背"]))
try:
    genanki.Package(_d).write_to_file(os.path.join(_ctl, "raw.apkg"))
    chk("A1 不洗直接写 .apkg 会 UnicodeEncodeError（证明这不是空想）", False,
        "居然写成功了 —— 这条对照本身需要重看")
except UnicodeEncodeError:
    chk("A1 不洗直接写 .apkg 会 UnicodeEncodeError（证明这不是空想）", True)


# ── B 导出：坏字符只是从卡面上消失，整包照样建得出来 ────────────────
print("\n── B 导出：脏正文不再把整包打没 ──")
items = [
    {"bookId": "b1", "text": "划线的前半" + HALF_HI, "kind": "划线", "at": 1756864800},
    {"bookId": "b1", "text": "想法里断了" + HALF_LO, "kind": "想法" + HALF_HI, "at": 1756864801},
    {"bookId": "b1", "text": "   ", "kind": "划线", "at": 0},          # 洗完是空 → 不成卡
]
ui_server.notes_for_book = _fake_notes(items)

_raised = None
dest, n = None, 0
try:
    dest, n = ui_server.build_apkg("b1", "书名" + HALF_HI)
except Exception as e:                                    # 修好之前这里就是 UnicodeEncodeError
    _raised = e
chk("B1 带半个代理码点的书名与正文：整包建得出来（不再 400）",
    _raised is None and dest is not None, repr(_raised))
chk("B2 盘上确实写出了 .apkg", bool(dest) and os.path.isfile(dest), dest)
chk("B3 空正文的那条不成卡（口径没变），另两条都留着", n == 2, n)

if dest and os.path.isfile(dest):
    try:
        cards, deck_names = anki_fields(dest)
        chk("B4 卡包里的 SQLite 读得开（不是半截文件）", True)
    except Exception as e:
        cards, deck_names = [], []
        chk("B4 卡包里的 SQLite 读得开（不是半截文件）", False, repr(e))
    joined = "".join(a + b for a, b in cards)
    chk("B5 卡面与背面一个代理码点都不剩", not has_half_surrogate(joined), repr(joined[:120]))
    chk("B6 卡组名里的半个码点也洗掉了",
        deck_names and not any(has_half_surrogate(x) for x in deck_names), deck_names)
    chk("B7 正常文字一个不少（洗的只是那半个码点）",
        sorted(a for a, _ in cards) == ["划线的前半", "想法里断了"], cards)


# ── C 成对的 emoji 与中文标点不误伤 ─────────────────────────────
print("\n── C 真 emoji 原样保留 ──")
ui_server.notes_for_book = _fake_notes(
    [{"bookId": "b2", "text": "好" + EMOJI + "的“划线”", "kind": "划线", "at": 1756864800}])
dest2, _ = ui_server.build_apkg("b2", "书" + EMOJI)
cards2, _ = safe_fields(dest2)
chk("C1 卡面上的 emoji 与中文标点一字不改",
    cards2 and cards2[0][0] == "好" + EMOJI + "的“划线”", cards2)


# ── D 幂等：同名再导一次不报错、条数一致 ─────────────────────────
print("\n── D 重复导出 ──")
dest3, n3 = ui_server.build_apkg("b2", "书" + EMOJI)
chk("D1 再导一次仍是同一份文件、条数不变",
    dest3 == dest2 and n3 == 1 and bool(dest3) and os.path.isfile(dest3),
    (dest2, dest3, n3))


# ── E 书名形状：None / 数字 / 空串都出得来 ───────────────────────
print("\n── E 书名形状兜底 ──")
ui_server.notes_for_book = _fake_notes(
    [{"bookId": "b3", "text": "一张卡", "kind": "划线", "at": 0}], resolved="")
for label, title in [("None", None), ("数字", 123), ("空串", "")]:
    try:
        d, _n = ui_server.build_apkg("b3", title)
        chk("E 书名是 %s 时照样出得来（用书号兜底）" % label,
            bool(d) and os.path.isfile(d) and os.path.basename(d) == "b3.apkg", d)
    except Exception as e:
        chk("E 书名是 %s 时照样出得来（用书号兜底）" % label, False, repr(e))

ui_server.notes_for_book = _fake_notes(
    [{"bookId": "b9", "text": "只有正文", "kind": "划线", "at": "abc"}], resolved="")
try:
    d9, _n9 = ui_server.build_apkg("书号" + HALF_HI, "粗")
    chk("E 书号里带半个码点也不抛（sha1 拿得到文本）", bool(d9) and os.path.isfile(d9), d9)
except Exception as e:
    chk("E 书号里带半个码点也不抛（sha1 拿得到文本）", False, repr(e))


# ── F 认不出的时间戳不影响卡片 ──────────────────────────────────
print("\n── F 坏时间戳 ──")
ui_server.notes_for_book = _fake_notes(
    [{"bookId": "b4", "text": "有日期的", "kind": "划线", "at": 1756864800},
     {"bookId": "b4", "text": "没日期的", "kind": "想法", "at": "昨天"}])
dest4, n4 = ui_server.build_apkg("b4", "时间书")
cards4, _ = safe_fields(dest4)
dated = {a: b for a, b in cards4}
_want = time.strftime("%Y-%m-%d", time.localtime(1756864800))
chk("F1 坏时间戳不带走卡片（两张都在）", n4 == 2 and len(cards4) == 2, cards4)
chk("F2 认得出的日期照旧印在背面，认不出的只是少那一段",
    _want in dated.get("有日期的", "") and _want not in dated.get("没日期的", ""), dated)


print()
print("Anki 卡包坏字符：通过 %d 项，失败 %d 项" % (PASSED[0], len(FAIL)))
if FAIL:
    for f in FAIL:
        print("  ✗ " + f)
    sys.exit(1)
print("ok  半个代理码点不再把整包卡打没，真 emoji 与导出条数口径都没动")
