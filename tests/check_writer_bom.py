#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""写作那一格的册账（writer.py 的 `_books.json`）：带 BOM 的手改账本不许被当成空账。

`_books.json` 是「写作」这一格唯一的书目账 —— 一本书 = 哪几章、按什么顺序。
它是一份用户能直接编辑的 JSON，记事本「另存为」、部分云同步客户端回写时会在开头
多出三个字节 `EF BB BF`。`writer.load_books` 原来按 utf-8 读：`json.load` 碰到 BOM
当场 JSONDecodeError，被那句「文件不在 / 读坏了都给空表」的宽 `except` 收成 `[]`，
写作屏看起来只是「书都没了、章节回到未归册」。

真正要命的是下一步：紧接着任何一次建书 / 改名 / 加章都会 `save_books(空表)` ——
把这份空账原样写回盘上。稿件文件都还在，但用户搭好的书目**永久没了**。这不是崩溃，
是静默清空，比崩溃难查得多。仓库里其余账本（book_layout.read_meta / flomo_notes /
ui_server 那几本）早改成 `utf-8-sig` 读，写作这一本漏了，`check_ledger_bom.py` 也没点到。

这一条只钉这一本账，三面都要：
  · 干净账读得出（夹具有效，不许靠「两边都是空」蒙过去）；
  · 加 BOM 后读回来与干净那份**逐字段相同**；
  · 一份旧读法的对照（裸 utf-8 的 `json.load` 确实会抛），证明这条门禁不是空转；
  · 后果那一面：带 BOM 的账上再建一本书，原来那本还在盘上，且写回的文件不带 BOM。

只碰 writer.py 的纯函数与系统临时目录里的沙盒书库，不起服务、不联网、不碰真实 cache/
与书库，所以归静态段，--fast 也要带。
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
SANDBOX = pathlib.Path(tempfile.mkdtemp(prefix="guizang-writer-bom-"))
os.environ["GUIZANG_DATA"] = str(SANDBOX / "cache")
os.environ["GUIZANG_BOOKS"] = str(SANDBOX / "books")

import writer  # noqa: E402

BOM = b"\xef\xbb\xbf"
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
    try:
        return False, fn(*a, **kw)
    except Exception as e:                      # noqa: BLE001
        return True, "%s: %s" % (type(e).__name__, str(e)[:120])


# ── 造一份合法册账：一本书 + 一章（夹具有效这关先过，后面才谈得上 BOM）──────
book = writer.create_book(LIB, "我的书")
threw, _made = no_raise(writer.add_chapter, LIB, book["id"], title="第一章")
chk("夹具：建书 / 加章不抛", not threw, _made)

clean = writer.load_books(LIB)
chk("夹具：干净账读得出这本书", any(b.get("id") == book["id"] for b in clean), clean)

BP = writer.books_path(LIB)
raw = pathlib.Path(BP).read_bytes()
chk("夹具本身没带 BOM", not raw.startswith(BOM), raw[:6])

# ── 加 BOM：读口必须与干净那份逐字段相同；旧读法对照要真抛（门禁不空转）──
pathlib.Path(BP).write_bytes(BOM + raw)
try:
    with open(BP, encoding="utf-8") as f:
        json.load(f)
    old_threw = False
except ValueError:
    old_threw = True
chk("对照：旧写法（utf-8）读带 BOM 的账确实抛", old_threw)
bom = writer.load_books(LIB)
chk("带 BOM 读回来与干净那份逐字段相同", bom == clean, (bom, clean))

# ── 后果那一面：带 BOM 的账上再建书，原来那本不许被空账冲掉 ──────────────
threw, _new = no_raise(writer.create_book, LIB, "新建的书")
chk("带 BOM 的账上建书不抛", not threw, _new)
on_disk = writer.load_books(LIB) if not threw else []
titles = [b.get("title") for b in on_disk]
chk("原来那本《我的书》还在盘上（没被空账冲掉）", "我的书" in titles, titles)
chk("新书也在盘上", "新建的书" in titles, titles)

# ── 写盘口径不变：修好的读口不会给文件加 BOM ─────────────────────────
after = pathlib.Path(BP).read_bytes()
chk("写回的文件不带 BOM（写盘口径没被改）", not after.startswith(BOM), after[:6])

# ── 再验一次旧读法对照：现在盘上这份（无 BOM）用 utf-8 也读得动，
#    所以上面的抛一定来自 BOM 那一下，不是别的 ────────────────────────
try:
    plain = json.load(open(BP, encoding="utf-8"))
    chk("无 BOM 时裸 utf-8 读得动（对照归因正确）", isinstance(plain, dict), type(plain))
except ValueError as e:                          # noqa: BLE001
    chk("无 BOM 时裸 utf-8 读得动（对照归因正确）", False, e)

shutil.rmtree(SANDBOX, ignore_errors=True)

print("\n写作册账 BOM 体检：通过 %d 项，失败 %d 项" % (PASSED, len(FAIL)))
for name in FAIL:
    print("  x " + name)
sys.exit(1 if FAIL else 0)
