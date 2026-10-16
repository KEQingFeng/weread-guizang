#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""章节 / 写作稿正文开头的 BOM（book_export.read_chapter_file）：记事本另存的三字节不许认成正文。

为什么单开这一条
    `book_export.read_chapter_file` 是章节正文、写作稿（`writer.read_text`）、
    导出 EPUB / PDF 共用的唯一读口。它按字节读、再按 UTF-8 解、认不出的字节换
    U+FFFD —— 这一半收的是「坏字节」。可它漏了另一半：记事本在 Windows 上
    「另存为 UTF-8」会在最前面多塞三字节 BOM（`\\xef\\xbb\\xbf`），那是**合法
    UTF-8**，`errors="replace"` 一个字都不换，于是解出来的正文以 U+FEFF 开头：

        read_chapter_file(p)  -> "\\ufeff# 标题\\n正文"     （看不见的字符混进正文）
        writer.outline(...)   -> []                        （`^#` 认不出顶着 BOM 的头一行）
        writer.title_from(..) -> 退回「未题名」/ 取到 BOM 本身

    症状是「大纲空了、标题没了」，而稿子明明在、也一个字没错；导出 EPUB / PDF 时
    那个不可见字符同样留在正文里。判据与 `book_layout.read_meta` / 各本账的读口
    是同一条：读口 `utf-8-sig`（只在开头出现 BOM 时摘掉），写口照旧 `utf-8`
    （不往里种 BOM）。这一条把共用那把尺子钉在 `utf-8-sig` 上。

这一条钉住
  · 对照：裸 `utf-8` 解同一个带 BOM 的文件，开头确实是 U+FEFF（证明这道门不是空转）；
  · read_chapter_file 对带 BOM 的章节 / 写作稿摘掉开头 BOM，正文其余一字不差；
  · writer.read_text / outline / title_from 三个入口在带 BOM 的稿子上都恢复正常
    （大纲认得出 `# 标题`、标题就是「标题」，不再退回兜底）；
  · read_chapters 逐章读回时同样摘掉 BOM，别的干净章节一个字不动；
  · 开头 BOM 与坏字节（GBK / 半截多字节）叠在一起时，BOM 摘掉、坏字节换 U+FFFD，
    两半互不影响；
  · BOM 只处理**开头**那一个：正文中间出现的同名码点一个都不动；
  · 干净 UTF-8（不含 BOM）读出来与原来逐字节相同，不多不少；
  · 读不到（文件不在 / 同名目录）照旧回 None / 跳过，调用方那条话术不变；
  · 接线：读口用的是 `utf-8-sig` 解码，不是裸 `utf-8`。

只碰 book_export / writer 的纯函数与系统临时目录里的沙盒书库，不起服务、不联网、
不碰真实书库，故归 --fast 静态段。
"""
import os
import pathlib
import shutil
import sys
import tempfile

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

# 数据目录指进临时沙盒再 import writer：免得书库那几条路真去动用户真实的书库。
SANDBOX = pathlib.Path(tempfile.mkdtemp(prefix="guizang-chapter-bom-"))
TMP = SANDBOX / "work"
os.environ["GUIZANG_DATA"] = str(SANDBOX / "cache")
os.environ["GUIZANG_BOOKS"] = str(SANDBOX / "books")

import book_export  # noqa: E402
import writer  # noqa: E402

FAIL = []
PASSED = 0


def chk(name, cond, extra=""):
    global PASSED
    if cond:
        PASSED += 1
        print("PASS  " + name)
    else:
        FAIL.append(name)
        print("FAIL  " + name + ("" if not extra else "   <- " + repr(extra)[:220]))


LIB = str(TMP / "library")
os.makedirs(LIB, exist_ok=True)

BOM = b"\xef\xbb\xbf"
# 带 BOM 的稿子：头一行就是 `# 标题`，正好是 `outline` / `title_from` 依赖的形状。
BOM_DRAFT = BOM + "# 我的标题\n\n正文第一段，带 emoji \U0001f600。\n".encode("utf-8")
CLEAN_DRAFT = "# 干净标题\n\n这一段没有 BOM。\n"


def write_bytes(path, raw):
    with open(path, "wb") as f:
        f.write(raw)


# ── 1. 对照：裸 utf-8 解带 BOM 的文件，开头就是 U+FEFF（洞是真的） ────
probe = TMP / "probe.md"
write_bytes(str(probe), BOM_DRAFT)
raw_text = probe.read_bytes().decode("utf-8", errors="replace")
chk("对照：裸 utf-8 解带 BOM 的文件，开头确实是 U+FEFF",
    raw_text.startswith("\ufeff") and not raw_text.startswith("#"), repr(raw_text[:12]))

# ── 2. 共用那把尺子：摘掉开头 BOM，正文其余一字不差 ─────────────────
got = book_export.read_chapter_file(str(probe))
want = BOM_DRAFT.decode("utf-8-sig")
chk("read_chapter_file：开头 BOM 摘掉了", isinstance(got, str) and not got.startswith("\ufeff"),
    repr((got or "")[:12]))
chk("read_chapter_file：正文其余一字不差", got == want, got)
chk("read_chapter_file：摘完就是 `# 我的标题` 打头",
    isinstance(got, str) and got.startswith("# 我的标题"), repr((got or "")[:12]))

# ── 3. 写作那一格三个入口：大纲认得出、标题就是标题 ─────────────────
bid, _meta = writer.create(LIB, title="带 BOM 的稿子", text="先随便一段")
d = writer.dir_of(LIB, bid)
write_bytes(writer.draft_path(d), BOM_DRAFT)

text = writer.read_text(d)
chk("writer.read_text：开头 BOM 不再混进正文", not text.startswith("\ufeff"), repr(text[:12]))
chk("writer.read_text：读回来与 utf-8-sig 逐字一致", text == want, text)

out = writer.outline(text)
chk("writer.outline：认得出头一行 `# 我的标题`（原来顶着 BOM 是空大纲）",
    out == [{"level": 1, "title": "我的标题", "line": 1}], out)
chk("writer.title_from：标题是「我的标题」，不退回兜底、也不带 BOM",
    writer.title_from(text) == "我的标题", writer.title_from(text))

# ── 4. read_chapters：逐章读回同样摘 BOM，干净章一个字不动 ─────────
bdir = TMP / "books" / "bom-book"
ch_dir = bdir / "chapters"
os.makedirs(str(ch_dir), exist_ok=True)
write_bytes(str(ch_dir / "0001.md"), BOM_DRAFT)
write_bytes(str(ch_dir / "0002.md"), CLEAN_DRAFT.encode("utf-8"))
rows = dict(book_export.read_chapters(str(bdir)))
chk("read_chapters：带 BOM 那一章摘掉开头 BOM",
    "0001.md" in rows and not rows["0001.md"].startswith("\ufeff"), repr(rows.get("0001.md", "")[:12]))
chk("read_chapters：带 BOM 那一章正文其余一字不差",
    rows.get("0001.md") == want, rows.get("0001.md"))
chk("read_chapters：干净那一章与原来逐字节相同",
    rows.get("0002.md") == CLEAN_DRAFT, rows.get("0002.md"))

# ── 5. 开头 BOM 与坏字节叠在一起：两半互不影响 ──────────────────────
mixed = BOM + b"# title\n" + "这一段是后来另存的".encode("gbk")
write_bytes(str(probe), mixed)
mixed_text = book_export.read_chapter_file(str(probe))
chk("BOM + GBK：开头 BOM 摘掉、坏字节换 U+FFFD",
    isinstance(mixed_text, str) and not mixed_text.startswith("\ufeff")
    and "\ufffd" in mixed_text, repr((mixed_text or "")[:24]))
half = BOM + "你好世界".encode("utf-8")[:5]
write_bytes(str(probe), half)
half_text = book_export.read_chapter_file(str(probe))
chk("BOM + 半截多字节：开头 BOM 摘掉、半截序列换 U+FFFD",
    isinstance(half_text, str) and not half_text.startswith("\ufeff")
    and "\ufffd" in half_text, repr((half_text or "")[:24]))

# ── 6. BOM 只处理开头那一个：正文中间的同名码点一个都不动 ─────────────
mid = ("# 标题\n\n前一段\ufeff后一段\n").encode("utf-8")
write_bytes(str(probe), mid)
mid_text = book_export.read_chapter_file(str(probe))
chk("正文中间那个 U+FEFF 原样留着（utf-8-sig 只摘开头那一颗）",
    mid_text == "# 标题\n\n前一段\ufeff后一段\n", repr(mid_text))

# ── 7. 干净 UTF-8 不多不少（回归：改动只影响开头 BOM 这一档） ────────
write_bytes(str(probe), CLEAN_DRAFT.encode("utf-8"))
chk("干净稿子逐字节相同（改编码没有夹带别的）",
    book_export.read_chapter_file(str(probe)) == CLEAN_DRAFT,
    book_export.read_chapter_file(str(probe)))
write_bytes(str(probe), b"")
chk("空文件回空串（不是 None）", book_export.read_chapter_file(str(probe)) == "")
chk("文件不在回 None", book_export.read_chapter_file(str(TMP / "nope.md")) is None)
os.makedirs(str(TMP / "adir"), exist_ok=True)
chk("同名目录回 None（OSError 那一档照旧）",
    book_export.read_chapter_file(str(TMP / "adir")) is None)

# ── 8. 接线：读口用 utf-8-sig 解码，不是裸 utf-8 ────────────────────
src = (REPO / "book_export.py").read_text(encoding="utf-8")
chk("接线：read_chapter_file 用 utf-8-sig 解码",
    'decode("utf-8-sig", errors="replace")' in src)
chk("接线：不再留裸 utf-8 解码那一句（同一处）",
    'decode("utf-8", errors="replace")' not in src)

shutil.rmtree(SANDBOX, ignore_errors=True)
print()
print("章节 / 写作稿 BOM 门禁：通过 %d 项，失败 %d 项" % (PASSED, len(FAIL)))
if FAIL:
    for f in FAIL:
        print("  x " + f)
    sys.exit(1)
print("ok  记事本另存的三字节不再混进正文：大纲认得出、标题还在、导出照旧")
