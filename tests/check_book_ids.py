#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""书城短 id 的尺子：Python 与界面必须认同一批数字。

背景：`book_ids.STORE_BOOK_ID_RE` 原来写的是 `r"\\d{5,20}"`，没带 `re.ASCII`。
Python 的 `\\d` 默认匹配 **Unicode 十进制数字** —— 全角数字（１２３４５）、
阿拉伯-印度数字（١٢٣٤٥）、天城文数字（१२३४५）全都算「数字」；而界面上那几处
是 JS 的 `/^\\d{5,20}$/`，按 ECMAScript 的定义 `\\d` 只认 0–9。

于是同一串全角数字在界面上被拒、从 `/api/shelf/add` 或 `shelf_add.py` 命令行
进来却被当成书城短 id，一路发给加书架接口换一个 `succ:0` 回来 —— 用户看到的
是「点了没反应」，正是这个模块当初抽出来要消灭的那类错觉。

这一条把「只认 ASCII 的 0–9」按成会失败的断言，并留一条「不带 re.ASCII 的同一
个 pattern 确实会收」的对照，免得哪天旗子被顺手删掉而门禁还绿着。纯函数 + 源码
走查，不联网、不开浏览器、不需要 playwright，所以归 --fast 静态段。
"""
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import book_ids as bi  # noqa: E402

FAIL = []
PASSED = 0


def chk(name, cond, extra=""):
    global PASSED
    print(("PASS  " if cond else "FAIL  ") + name + ("" if cond else "   " + str(extra)[:220]))
    if cond:
        PASSED += 1
    else:
        FAIL.append(name)


# ── 1. 尺子的旗子本身 ──────────────────────────────────────────────
chk("正则带 re.ASCII（Python 的 \\d 默认认全角数字，不加旗子就比界面宽一档）",
    bool(bi.STORE_BOOK_ID_RE.flags & re.ASCII),
    bi.STORE_BOOK_ID_RE.flags)


# ── 2. 正常的 0–9 一个都不许误伤 ───────────────────────────────────
ASCII_GOOD = ["12345", "123450001", "123456789012",
              "1" * 20, 12345, " 12345 ", "123450001\n"]
chk("ASCII 数字照旧全认（含首尾空白与 int 输入）",
    all(bi.is_store_book_id(x) for x in ASCII_GOOD),
    [x for x in ASCII_GOOD if not bi.is_store_book_id(x)])


# ── 3. 长得像数字、其实不是 0–9 的一位都不许收 ─────────────────────
# 每一串都是 5 个「数字」，只是不在 ASCII 的 0–9 里 —— 加书架接口只认 ASCII 短 id。
UNICODE_DIGITS = [
    "\uff11\uff12\uff13\uff14\uff15",            # 全角 １２３４５
    "\u0661\u0662\u0663\u0664\u0665",            # 阿拉伯-印度 ١٢٣٤٥
    "\u0967\u0968\u0969\u096a\u096b",            # 天城文 १२३४५
    "1234\uff15",                                # ASCII 里混一个全角
    "\uff11" * 20,                               # 全角也要受 5–20 的管
]
chk("全角 / 阿拉伯-印度 / 天城文数字一律不算书城短 id",
    not any(bi.is_store_book_id(x) for x in UNICODE_DIGITS),
    [x for x in UNICODE_DIGITS if bi.is_store_book_id(x)])


# ── 4. 对照：证明上面那组以前真会漏（防门禁空转） ──────────────────
# 同一个 pattern，故意不带 re.ASCII —— 它必须收下全角那一串，否则这组断言
# 根本没在量东西（比如哪天 UNICODE_DIGITS 被写成 ASCII 数字，这里会当场现形）。
_LOOSE = re.compile(r"\d{%d,%d}" % (bi.STORE_BOOK_ID_MIN, bi.STORE_BOOK_ID_MAX))
chk("对照：不带 re.ASCII 的同一个 pattern 确实会收全角数字（说明这组不是空转）",
    bool(_LOOSE.fullmatch(UNICODE_DIGITS[0])),
    UNICODE_DIGITS[0])


# ── 5. 批量口（真正发给子进程的那一步）同样只留 ASCII ─────────────
MIXED = [" 22222 ", UNICODE_DIGITS[0], "11111", "22222", "\u0967\u0968\u0969\u096a\u096b", None]
chk("clean 只留 ASCII 短 id：脏数字安静丢掉、保序去重",
    bi.clean_store_book_ids(MIXED) == ["22222", "11111"],
    bi.clean_store_book_ids(MIXED))
chk("全是脏数字时给空表（调用方据此回「没有可用的书籍编号」，一个请求也不发）",
    bi.clean_store_book_ids(UNICODE_DIGITS) == [],
    bi.clean_store_book_ids(UNICODE_DIGITS))


# ── 6. 源码走查：这条规则仍只有一份，界面那两处是 JS 的 \d（ASCII） ──
src_bi = (ROOT / "book_ids.py").read_text(encoding="utf-8")
_tail = src_bi[src_bi.index("STORE_BOOK_ID_RE = re.compile"):][:200]
chk("book_ids.py 编译这条正则时确实传了 re.ASCII",
    "re.ASCII" in _tail, _tail[:120])

other_py = ["ui_server.py", "shelf_add.py"]
chk("Python 这一侧仍只剩 book_ids.py 一处定义（别的 .py 不另立一把尺子）",
    all(r"\d{5,20}" not in (ROOT / p).read_text(encoding="utf-8", errors="replace")
        for p in other_py))

# JS 的 \d 按 ECMAScript 就是 0–9；只要界面没改用 Unicode 属性转义 / 全角字符类，
# 两边认的就是同一批数字。这几处是 check_shelf_add 也在比的同一组字面量。
js_ui = (ROOT / "ui.html").read_text(encoding="utf-8", errors="replace")
js_mcp = (ROOT / "mcp" / "guizang-mcp.mjs").read_text(encoding="utf-8", errors="replace")
chk("界面的尺子是 /^\\d{5,20}$/（JS 的 \\d 只认 0–9）",
    "/^\\d{5,20}$/" in js_ui and "/^\\d{5,20}$/" in js_mcp)
chk("界面没有改用 Unicode 属性转义 / 全角字符类（那才会让两边真正分叉）",
    "\\p{Nd}" not in js_ui and "\\p{Nd}" not in js_mcp
    and "０-９" not in js_ui and "０-９" not in js_mcp)


print()
print("书城短 id 的 ASCII 口径：通过 %d 项，失败 %d 项" % (PASSED, len(FAIL)))
if FAIL:
    for f in FAIL:
        print("  ✗ " + f)
    sys.exit(1)
print("ok  全角 / 阿拉伯-印度 / 天城文数字不再是书城短 id，ASCII 0–9 一个字不改")
