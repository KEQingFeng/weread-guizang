#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""OPML 导出的那半边闸门：导出的清单必须自己解得开、也导得回去 (feed.py)。

为什么单开这一条
    `tests/check_opml_safety.py` 管的是「从别处整包拿来的 OPML 进门」这道闸；
    这一条管对面那一步 —— **归藏自己导出的 `订阅.opml`**。`_esc` 原先只有一句
    `html.escape(..., quote=True)`，而 `html.escape` 只换 `& < > " '`：XML 1.0 的
    Char 产生式不允许的字符它一个都不管（C0 里除 `\\t \\n \\r` 之外的、落单的代理
    码点、`U+FFFE` / `U+FFFF`）。标题、站点名、组名全是外部数据：

      · JSON Feed（`application/feed+json`）的字符串里 `"\\u000b"` 是合法转义，
        `json.loads` 收得下，`_parse_jsonfeed` 原样当标题；
      · `feed.json` 是用户能手改的文本，云同步也可能写回旧版本；
      · 组名是用户自己敲的。

    于是标题里混进一个这样的字符，导出的 `.opml` 就是**不合式** XML：任何阅读器
    （以及本模块自己的 `import_opml`）解它都当场 `ParseError: not well-formed`。
    用户看到的是「导出的文件导入别的软件报错」，而界面上导出那一下回的是「已导出」。

    修法在 `_esc`：先按 XML 1.0 的 Char 产生式把落在允许区间之外的字符洗掉（口径与
    `_drop_surrogates` 一致 —— 认不出的坏输入洗掉），再 `html.escape`。允许的字符
    （含 `\\t \\n \\r`、CJK、emoji）一个字不动。

这一条钉住
  · **对照**：把 `_esc` 换回只有 `html.escape` 的老样子，同一份账本导出的 OPML
    确实解不开 —— 证明这道闸不是空转；
  · 修好之后 `export_opml()` 的输出 `ET.fromstring` 解得开，且正文里不再有
    C0 控制符 / 落单代理码点 / `U+FFFE` / `U+FFFF`；
  · `<title>`、组 outline、源 outline 三处的脏字符都被洗掉（不是只顾一处）；
  · 干净的内容（CJK、`&`、emoji、`\\t \\n \\r`）一个字不改，`&` 照常转义成 `&amp;`；
  · 导出的这份用 `import_opml(..., dry_run=True)` 读得回来，`xmlUrl` / 标题都对得上；
  · `write_opml(path)` 落到盘上的那个文件同样解得开。

沙盒：`_load` 被替换成一份内存账本，`write_opml` 写到系统临时目录，不碰真实
`cache/` 与书库、不联网、不开浏览器，故归 --fast 静态段。
"""
import html
import os
import pathlib
import shutil
import sys
import tempfile
import xml.etree.ElementTree as ET

HERE = pathlib.Path(__file__).resolve().parent
REPO = HERE.parent
sys.path.insert(0, str(REPO))

# GUIZANG_DATA 必须在 import feed 之前设好：feed.py 在导入时就把 DATA_DIR / STORE 定下来。
TMP = tempfile.mkdtemp(prefix="gz-opml-export-")
os.environ["GUIZANG_DATA"] = os.path.join(TMP, "data")
os.environ["GUIZANG_BOOKS"] = os.path.join(TMP, "books")
os.makedirs(os.environ["GUIZANG_DATA"], exist_ok=True)
os.makedirs(os.environ["GUIZANG_BOOKS"], exist_ok=True)

import feed  # noqa: E402

FAIL = []
PASSED = [0]


def chk(name, ok, extra=""):
    if ok:
        PASSED[0] += 1
        print("  ok  " + name)
    else:
        FAIL.append(name)
        print(" FAIL " + name + (("  <- " + str(extra)[:220]) if extra else ""))


def parses(text):
    """这段文本解得开吗（返回 (bool, 出错时那句话)）。"""
    try:
        ET.fromstring(text)
        return True, ""
    except Exception as e:                     # noqa: BLE001
        return False, "%s: %s" % (type(e).__name__, e)


# XML 1.0 不允许、但 Python 字符串 / JSON 转义里都装得下的那几类各来一个。
BANNED = ("\x00", "\x08", "\x0b", "\x0c", "\x0e", "\x1f", "\ud800", "\ufffe", "\uffff")
# 允许的：制表 / 换行 / 回车、普通 CJK、emoji（含 ZWJ 组合）、`&`（要被转义）。
ALLOWED = "\t\n\r中文🧑‍💻 & < > \" '"

TITLE = "标题" + "".join(BANNED) + "尾" + ALLOWED
GROUP = "组" + "".join(BANNED) + "名"
SITE = "站点" + "\ufffe" + "&amp;"

DATA = {
    "feeds": [{"id": "f1", "url": "http://example.com/a.xml?x=1&y=2",
               "title": TITLE, "site": SITE, "group": GROUP, "order": 0}],
    "entries": {},
    "seen": {},
    "settings": dict(feed.DEFAULT_SETTINGS, groups=[GROUP]),
}

_real_load = feed._load
_real_esc = feed._esc
feed._load = lambda: DATA

try:
    # ── 1 对照：老 `_esc`（只有 html.escape）导出的这份确实解不开 ─────
    feed._esc = lambda v: html.escape(str(v or ""), quote=True)
    old_text = feed.export_opml()
    ok_old, why_old = parses(old_text)
    chk("对照：老写法（只有 html.escape）导出的 OPML 解不开", not ok_old, why_old)
    chk("对照：老写法确实把非法字符原样写进了文件",
        any(b in old_text for b in BANNED))
    feed._esc = _real_esc

    # ── 2 修好之后：解得开，且非法字符一个都不剩 ─────────────────────
    text = feed.export_opml()
    ok_new, why_new = parses(text)
    chk("现在导出的 OPML 解得开（well-formed）", ok_new, why_new)
    left = [repr(b) for b in BANNED if b in text]
    chk("导出的正文里不再有 C0 / 代理 / U+FFFE / U+FFFF", left == [], left)

    root = ET.fromstring(text)
    head = root.find("head")
    chk("head / body 都在", head is not None and root.find("body") is not None)
    body = root.find("body")

    # ── 3 三处脏字符都被洗（head 标题 / 组 outline / 源 outline） ─────
    # 顶层 <title> 是 export_opml(title=…) 那个参数，给一份同样脏的。
    head_text = feed.export_opml(title=TITLE)
    ok_head, why_head = parses(head_text)
    head_title = (ET.fromstring(head_text).findtext("head/title") or "") if ok_head else ""
    chk("head 的 <title>（导出时那个标题参数）也解得开", ok_head, why_head)
    chk("head 的 <title> 洗过了（还有「标题」「尾」这些好字符）",
        "标题" in head_title and "尾" in head_title, repr(head_title))
    group_out = body.find("outline") if body is not None else None
    chk("组 outline 在", group_out is not None)
    feed_out = None
    if group_out is not None:
        g_attr = group_out.get("text") or ""
        chk("组名的脏字符洗掉了、「组」「名」还在",
            g_attr.startswith("组") and g_attr.endswith("名"), repr(g_attr))
        feed_out = group_out.find("outline")
        chk("组里那个源 outline 在", feed_out is not None)
    if feed_out is not None:
        f_text = feed_out.get("text") or ""
        chk("源标题洗过了、好内容还在",
            "标题" in f_text and "中文🧑‍💻" in f_text, repr(f_text))
        chk("xmlUrl 原样且 & 照常可以解析回来",
            feed_out.get("xmlUrl") == "http://example.com/a.xml?x=1&y=2",
            feed_out.get("xmlUrl"))
        chk("site 里的 U+FFFE 洗掉了、&amp; 解回 &",
            feed_out.get("htmlUrl") == "站点&amp;", repr(feed_out.get("htmlUrl")))

    # ── 4 允许的字符一个字不改 ───────────────────────────────────────
    chk("CJK 与 emoji（含 ZWJ 组合）完整保留",
        "中文🧑‍💻" in head_title, repr(head_title))
    # 制表 / 换行 / 回车是 XML 允许的字符，写出去时不该被这道闸洗掉。
    # 注意要在**原文**上验：XML 解析器会按规范把 `\r`（与 `\r\n`）归一成 `\n`，
    # 解析回来的字符串里看不到 `\r` 是解析器干的，不是我们把字洗了。
    chk("制表 / 换行 / 回车这三个白名单字符写出去时没被误伤",
        all(c in head_text for c in ("\t", "\n", "\r")), repr(head_text))
    chk("`&` 照常转义（文件里是 &amp;，解析回来是 &）",
        "&amp;" in text and feed_out is not None
        and "& < >" in (feed_out.get("text") or ""), repr(head_title))

    # ── 5 导得出去也导得回来 ─────────────────────────────────────────
    back = feed.import_opml(text, dry_run=True)
    row = (back.get("results") or [{}])[0]
    chk("导出的这份能原样 import 回来（dry_run）",
        back.get("total") == 1 and row.get("status") == "pending", back)
    chk("读回来的 xmlUrl 与写出去的一致",
        row.get("url") == "http://example.com/a.xml?x=1&y=2", row)
    chk("读回来的标题是洗过的那一份",
        "标题" in (row.get("title") or "") and "尾" in (row.get("title") or ""),
        repr(row.get("title")))

    # ── 6 落盘那一步同样解得开 ───────────────────────────────────────
    out_path = os.path.join(TMP, "订阅.opml")
    feed.write_opml(out_path)
    with open(out_path, "rb") as fh:
        on_disk = fh.read().decode("utf-8")
    ok_disk, why_disk = parses(on_disk)
    chk("write_opml 落到盘上的文件也解得开", ok_disk, why_disk)
finally:
    feed._load = _real_load
    feed._esc = _real_esc
    shutil.rmtree(TMP, ignore_errors=True)

print("\nOPML 导出合法性：通过 %d 项，失败 %d 项" % (PASSED[0], len(FAIL)))
for f in FAIL:
    print("  × " + f)
sys.exit(1 if FAIL else 0)
